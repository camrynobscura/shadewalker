"""Tests for server/geocode.py + the /geocode endpoints -- the Photon
proxy that replaced browser-direct Nominatim on 2026-08-30
(history/geocoding-photon.md).

The behaviors pinned here, and where they came from:
- the address-not-POI reverse rule (the Lucali story, 2026-08-24: reverse
  right outside a restaurant must label the field "575 Henry Street",
  never "Lucali") -- it moved here from web/src/api.ts with the proxy;
- upstream failures RAISE so the LRU cache can never memoize an outage;
- the cache actually absorbs repeat queries -- politeness to a fair-use
  public upstream is the proxy's reason to exist;
- the 502 mapping never echoes query text back (location data).

No test here touches the network: _session.get is monkeypatched with
canned Photon GeoJSON. TestClient is used WITHOUT its context manager on
purpose -- lifespan (and with it the graph load) only runs inside
`with`, and these endpoints don't need the graph.
"""

import pytest
import requests
from fastapi.testclient import TestClient

from server import geocode
from server.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_caches():
    """lru_cache outlives each test; a stale hit would silently bypass a
    test's own monkeypatch and let it assert against another test's data."""
    geocode.search.cache_clear()
    geocode.reverse.cache_clear()
    yield
    geocode.search.cache_clear()
    geocode.reverse.cache_clear()


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body


def feature(props: dict, lon: float = -73.99, lat: float = 40.68) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def photon_body(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def patch_upstream(monkeypatch, body: dict, status_code: int = 200) -> list[dict]:
    """Replace the real HTTP hop with a canned answer; returns the call
    log so tests can assert on params sent and on call COUNTS (the cache
    tests are counts)."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(body, status_code)

    monkeypatch.setattr(geocode._session, "get", fake_get)
    return calls


# --- reverse label: an address field needs an address, never a POI name --

def test_reverse_label_prefers_address_over_poi_name():
    # Lucali: Photon puts the restaurant's own housenumber/street right
    # beside its name, so the address parts win and the name never shows.
    props = {"name": "Lucali", "housenumber": "575", "street": "Henry Street",
             "osm_key": "amenity", "osm_value": "restaurant"}
    assert geocode._reverse_label(props) == "575 Henry Street"


def test_reverse_label_street_without_housenumber():
    assert geocode._reverse_label({"street": "Henry Street"}) == "Henry Street"


def test_reverse_label_accepts_a_walkable_way_by_name():
    # A park path/drive is a way you physically stand on -- its name IS
    # the honest address-ish label (the old West Drive case).
    props = {"name": "West Drive", "osm_key": "highway", "osm_value": "path"}
    assert geocode._reverse_label(props) == "West Drive"


def test_reverse_label_rejects_named_non_ways():
    # A park polygon or an addressless shop must NOT label the field --
    # None sends the caller to its coordinate fallback.
    assert geocode._reverse_label({"name": "Prospect Park", "osm_key": "leisure"}) is None
    assert geocode._reverse_label({}) is None


# --- search feature parsing ----------------------------------------------

def test_search_parses_poi_with_district_context():
    f = feature({"name": "Lucali", "district": "Carroll Gardens", "city": "New York"},
                lon=-74.0004, lat=40.6818)
    assert geocode._parse_search_feature(f) == {
        "lat": 40.6818, "lon": -74.0004, "label": "Lucali, Carroll Gardens",
    }


def test_search_parses_plain_address():
    # An address point has housenumber+street and NO name; forward search
    # labels it like a person would type it.
    f = feature({"housenumber": "575", "street": "Henry Street", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "575 Henry Street, New York"


def test_search_label_does_not_repeat_context():
    f = feature({"name": "New York", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "New York"


def test_search_drops_unusable_features():
    assert geocode._parse_search_feature({"properties": {"name": "x"}}) is None  # no geometry
    assert geocode._parse_search_feature(feature({})) is None  # nothing displayable


# --- the five-borough filter ---------------------------------------------

def test_search_drops_results_outside_the_city(monkeypatch):
    # The bbox rectangle admits Hoboken, Nassau, and Westchester; the
    # city/state filter is what actually keeps autocomplete to the five
    # boroughs (user report 2026-09-02). Photon's admin-hierarchy `city`
    # is "New York" for every borough result, verified live the same day.
    patch_upstream(monkeypatch, photon_body(
        feature({"name": "Citi Bike - 11 St", "street": "11th Street",
                 "city": "Hoboken", "state": "New Jersey"}),
        feature({"housenumber": "45", "street": "Charles Street",
                 "city": "Valley Stream", "state": "New York",
                 "district": "Alden Manor"}),
        feature({"housenumber": "45", "street": "Charles Street",
                 "city": "New York", "state": "New York",
                 "district": "Manhattan"}),
    ))
    results = geocode.search("45 charles street", 5)
    assert [r["label"] for r in results] == ["45 Charles Street, Manhattan"]


def test_search_requires_city_to_be_present(monkeypatch):
    # A feature with no admin hierarchy at all can't prove it's in the
    # city — it goes, same as a wrong one.
    patch_upstream(monkeypatch, photon_body(
        feature({"name": "Somewhere", "state": "New York"}),
    ))
    assert geocode.search("somewhere", 5) == ()


# --- upstream plumbing ---------------------------------------------------

def test_search_pins_bbox_and_language(monkeypatch):
    calls = patch_upstream(monkeypatch, photon_body())
    geocode.search("court st", 5)
    assert len(calls) == 1
    params = calls[0]["params"]
    assert params["bbox"] == geocode._CITY_BBOX
    assert params["lang"] == "en"
    assert params["limit"] == 5
    assert calls[0]["url"].endswith("/api")


def test_session_identifies_the_app():
    # Photon's fair-use ask. The header sits on the session, so every
    # request carries it without any per-call code.
    assert "shady-stroll" in geocode._session.headers["User-Agent"]


def test_non_200_raises_upstream_error(monkeypatch):
    patch_upstream(monkeypatch, {}, status_code=429)
    with pytest.raises(geocode.UpstreamError):
        geocode.search("court st", 1)


def test_network_failure_raises_upstream_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise requests.Timeout("upstream slow")

    monkeypatch.setattr(geocode._session, "get", fake_get)
    with pytest.raises(geocode.UpstreamError):
        geocode.reverse(40.68, -73.99)


# --- the cache -----------------------------------------------------------

def test_repeat_search_hits_cache_not_upstream(monkeypatch):
    calls = patch_upstream(monkeypatch, photon_body(
        feature({"name": "Court Street", "city": "New York", "state": "New York"})))
    first = geocode.search("court st", 1)
    second = geocode.search("court st", 1)
    assert first == second
    assert len(calls) == 1  # the second answer came from the cache


def test_failures_are_never_cached(monkeypatch):
    # An outage must not be memoized: the next request retries and gets
    # the real answer once the upstream is back.
    patch_upstream(monkeypatch, {}, status_code=503)
    with pytest.raises(geocode.UpstreamError):
        geocode.search("smith st", 1)
    calls = patch_upstream(monkeypatch, photon_body(
        feature({"name": "Smith Street", "city": "New York", "state": "New York"})))
    assert geocode.search("smith st", 1)[0]["label"] == "Smith Street, New York"
    assert len(calls) == 1


# --- the endpoints -------------------------------------------------------

def test_geocode_endpoint_shape(monkeypatch):
    patch_upstream(monkeypatch, photon_body(
        feature({"name": "Court Street", "city": "New York", "state": "New York"},
                lon=-73.998, lat=40.68)))
    resp = client.get("/geocode", params={"q": "court st"})
    assert resp.status_code == 200
    assert resp.json() == {"results": [
        {"lat": 40.68, "lon": -73.998, "label": "Court Street, New York"},
    ]}


def test_geocode_endpoint_normalizes_whitespace(monkeypatch):
    calls = patch_upstream(monkeypatch, photon_body())
    client.get("/geocode", params={"q": "  court   st  "})
    assert calls[0]["params"]["q"] == "court st"


def test_geocode_endpoint_rejects_blank_and_oversized_q(monkeypatch):
    patch_upstream(monkeypatch, photon_body())
    assert client.get("/geocode", params={"q": "   "}).status_code == 422
    assert client.get("/geocode", params={"q": "x" * 201}).status_code == 422


def test_geocode_endpoint_caps_limit(monkeypatch):
    patch_upstream(monkeypatch, photon_body())
    assert client.get("/geocode", params={"q": "a", "limit": 50}).status_code == 422


def test_upstream_failure_maps_to_502_without_echoing_q(monkeypatch):
    patch_upstream(monkeypatch, {}, status_code=500)
    resp = client.get("/geocode", params={"q": "575 henry st"})
    assert resp.status_code == 502
    assert "575" not in resp.text  # query text is location data


def test_reverse_endpoint_passes_null_through(monkeypatch):
    patch_upstream(monkeypatch, photon_body())  # nothing nearby
    resp = client.get("/geocode/reverse", params={"lat": 40.68, "lon": -73.99})
    assert resp.status_code == 200
    assert resp.json() == {"label": None}


def test_reverse_endpoint_rejects_out_of_range_coords():
    assert client.get("/geocode/reverse", params={"lat": 95, "lon": 0}).status_code == 422
