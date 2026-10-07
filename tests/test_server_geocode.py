"""Tests for server/geocode.py + the /geocode endpoints -- the Photon
proxy and the backup geocoder behind it.

The behaviors pinned here:
- the address-not-POI reverse rule: reverse right outside a restaurant
  must label the field "575 Henry Street", never "Lucali";
- upstream failures raise so the LRU cache can never memoize an outage;
- the cache actually absorbs repeat queries -- politeness to a fair-use
  public upstream is the proxy's reason to exist;
- the 502 mapping never echoes query text back (location data);
- when Photon fails the backup answers, Photon is re-asked only in the
  background, and a backup answer is never served once Photon is back.

No test here touches the network: _session.get is monkeypatched with
canned GeoJSON. TestClient is used without its context manager on
purpose -- lifespan (and with it the graph load) only runs inside
`with`, and these endpoints don't need the graph.
"""

import threading

import pytest
import requests
from fastapi.testclient import TestClient

from pipeline import config
from server import geocode
from server.app import app

client = TestClient(app)

_start_real_thread = geocode._run_in_background


@pytest.fixture(autouse=True)
def clear_caches():
    """lru_cache and the Photon-is-down flag outlive each test; a stale hit
    would silently bypass a test's own monkeypatch and let it assert
    against another test's data."""
    geocode.clear_caches()
    yield
    geocode.clear_caches()


@pytest.fixture(autouse=True)
def background(monkeypatch) -> list:
    """Background Photon checks are collected here instead of started, so a
    test runs one when it chooses and no thread outlives a test."""
    tasks = []
    monkeypatch.setattr(geocode, "_run_in_background", tasks.append)
    return tasks


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(geocode, "_now", fake)
    return fake


class FakeResponse:
    def __init__(self, body, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
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
    log so tests can assert on params sent and on call counts (the cache
    tests are counts)."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(body, status_code)

    monkeypatch.setattr(geocode._session, "get", fake_get)
    return calls


def patch_upstreams(monkeypatch, photon, backup=None) -> list[dict]:
    """Canned answers per upstream: a body (answered with 200), a status
    code, or an exception to raise. Returns the call log, each call tagged
    with the upstream it went to."""
    answers = {"photon": photon, "backup": backup}
    calls = []

    def fake_get(url, params=None, timeout=None):
        upstream = "backup" if url.startswith(config.BACKUP_GEOCODER_URL) else "photon"
        calls.append({"upstream": upstream, "url": url, "params": params, "timeout": timeout})
        answer = answers[upstream]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return FakeResponse({}, answer)
        return FakeResponse(answer)

    monkeypatch.setattr(geocode._session, "get", fake_get)
    return calls


def asked(calls: list[dict], upstream: str) -> int:
    return sum(1 for call in calls if call["upstream"] == upstream)


def backup_feature(props: dict, lon: float = -73.99, lat: float = 40.68) -> dict:
    return feature({"layer": "venue", "source": "nycpad", **props}, lon=lon, lat=lat)


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
    # A park path/drive is a way you physically stand on -- its name is
    # the honest address-ish label.
    props = {"name": "West Drive", "osm_key": "highway", "osm_value": "path"}
    assert geocode._reverse_label(props) == "West Drive"


def test_reverse_label_rejects_named_non_ways():
    # A park polygon or an addressless shop must not label the field --
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
    # An address point has housenumber+street and no name; forward search
    # labels it like a person would type it.
    f = feature({"housenumber": "575", "street": "Henry Street", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "575 Henry Street, New York"


def test_search_named_place_carries_its_street_address():
    # Two branches of a chain in one borough must not read the same.
    f = feature({"name": "Whole Foods Market", "housenumber": "250", "street": "7th Avenue",
                 "locality": "Chelsea District", "district": "Manhattan", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "Whole Foods Market, 250 7th Avenue, Manhattan"


def test_search_named_place_without_an_address_carries_its_neighborhood():
    f = feature({"name": "Whole Foods Promenade", "locality": "Gowanus",
                 "district": "Brooklyn", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "Whole Foods Promenade, Gowanus, Brooklyn"


def test_search_named_street_does_not_repeat_itself_as_its_address():
    # A feature whose `street` is its own name must not say it twice.
    f = feature({"name": "Court Street", "street": "Court Street",
                 "locality": "Cobble Hill", "district": "Brooklyn", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "Court Street, Brooklyn"


def test_search_plain_address_stays_short():
    f = feature({"housenumber": "575", "street": "Henry Street", "locality": "Carroll Gardens",
                 "district": "Brooklyn", "city": "New York"})
    parsed = geocode._parse_search_feature(f)
    assert parsed is not None and parsed["label"] == "575 Henry Street, Brooklyn"


def test_search_merges_results_that_read_the_same(monkeypatch):
    shop = {"name": "Whole Foods", "housenumber": "301", "street": "West 50th Street",
            "district": "Manhattan", "city": "New York", "state": "New York"}
    patch_upstream(monkeypatch, photon_body(
        feature(shop, lon=-73.9870, lat=40.7627),
        feature(shop, lon=-73.9871, lat=40.7628),
        feature({**shop, "housenumber": "66", "street": "Broadway"}),
    ))
    results = geocode.search("whole foods", 5)
    assert [r["label"] for r in results] == [
        "Whole Foods, 301 West 50th Street, Manhattan",
        "Whole Foods, 66 Broadway, Manhattan",
    ]
    assert results[0]["lon"] == -73.9870  # the first of a pair is the one kept


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
    # boroughs. Photon's admin-hierarchy `city` is "New York" for every
    # borough result.
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

def test_a_refusal_is_logged_by_status_without_the_searched_text(monkeypatch, caplog):
    # A throttle from the fair-use upstream must leave a trace; what was
    # searched for must not.
    patch_upstream(monkeypatch, photon_body(), status_code=429)
    with caplog.at_level("WARNING", logger="server.geocode"):
        with pytest.raises(geocode.UpstreamError):
            geocode.search("250 court st", 5)
    assert "Photon answered 429" in caplog.text
    assert "court" not in caplog.text


def test_a_network_failure_is_logged_by_kind_without_the_url(monkeypatch, caplog):
    def boom(url, params=None, timeout=None):
        raise requests.ConnectTimeout("timed out: https://photon.example/api?q=250+court+st")

    monkeypatch.setattr(geocode._session, "get", boom)
    with caplog.at_level("WARNING", logger="server.geocode"):
        with pytest.raises(geocode.UpstreamError):
            geocode.search("250 court st", 5)
    assert "Photon unreachable: ConnectTimeout" in caplog.text
    assert "court" not in caplog.text


def test_repeat_search_hits_cache_not_upstream(monkeypatch):
    calls = patch_upstream(monkeypatch, photon_body(
        feature({"name": "Court Street", "city": "New York", "state": "New York"})))
    first = geocode.search("court st", 1)
    second = geocode.search("court st", 1)
    assert first == second
    assert len(calls) == 1  # the second answer came from the cache


def test_failures_are_never_cached(monkeypatch):
    # An outage of both geocoders must not be memoized: the next request
    # asks again and gets a real answer once one of them is back.
    patch_upstreams(monkeypatch, photon=503, backup=503)
    with pytest.raises(geocode.UpstreamError):
        geocode.search("smith st", 1)
    calls = patch_upstreams(monkeypatch, photon=503, backup=photon_body(
        backup_feature({"name": "100 SMITH STREET", "housenumber": "100",
                        "street": "SMITH STREET", "borough": "Brooklyn"})))
    assert geocode.search("smith st", 1)[0]["label"] == "100 Smith Street, Brooklyn"
    assert asked(calls, "backup") == 1


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


# --- the backup geocoder: its labels -------------------------------------

EMPIRE_STATE = backup_feature({"name": "350 FIFTH AVENUE", "housenumber": "350",
                               "street": "FIFTH AVENUE", "borough": "Manhattan"},
                              lon=-73.9857, lat=40.7484)
PROSPECT_PARK = backup_feature({"name": "PROSPECT PARK", "housenumber": None,
                                "street": "PROSPECT PARK", "borough": "Brooklyn"})


def test_backup_address_reads_like_a_photon_address():
    assert geocode._parse_backup_feature(EMPIRE_STATE) == {
        "lat": 40.7484, "lon": -73.9857, "label": "350 Fifth Avenue, Manhattan"}


def test_backup_named_place_carries_its_borough():
    # The directory repeats a place's name as its street; the label must not.
    assert geocode._parse_backup_feature(PROSPECT_PARK)["label"] == "Prospect Park, Brooklyn"


def test_backup_labels_are_not_left_in_upper_case():
    parsed = geocode._parse_backup_feature(backup_feature(
        {"name": "BARCLAY'S CENTER & ARENA", "street": "BARCLAY'S CENTER & ARENA",
         "borough": "Brooklyn"}))
    assert parsed["label"] == "Barclay's Center & Arena, Brooklyn"
    queens = geocode._parse_backup_feature(backup_feature(
        {"name": "37-10 30 AVENUE", "housenumber": "37-10", "street": "30 AVENUE",
         "borough": "Queens"}))
    assert queens["label"] == "37-10 30 Avenue, Queens"
    # str.title would have written "5Th".
    assert geocode._title("EAST 5TH STREET") == "East 5th Street"


def test_backup_drops_anything_outside_the_five_boroughs():
    # What the backup returns for a point it has nothing near.
    country = feature({"name": "United States", "layer": "country"})
    assert geocode._parse_backup_feature(country) is None
    assert geocode._parse_backup_feature({"properties": {"borough": "Brooklyn"}}) is None
    assert geocode._parse_backup_feature(backup_feature({"borough": "Brooklyn"})) is None


# --- the backup geocoder: when it answers --------------------------------

def test_backup_answers_when_photon_times_out(monkeypatch):
    calls = patch_upstreams(monkeypatch, photon=requests.ReadTimeout("slow"),
                            backup=photon_body(EMPIRE_STATE, EMPIRE_STATE))
    results = geocode.search("350 fifth", 5)
    # Two rows that read the same are one choice, as with Photon.
    assert [r["label"] for r in results] == ["350 Fifth Avenue, Manhattan"]
    assert asked(calls, "photon") == 1
    backup_call = calls[-1]
    assert backup_call["url"] == f"{config.BACKUP_GEOCODER_URL}/autocomplete"
    assert backup_call["params"] == {"text": "350 fifth", "size": 5}
    assert backup_call["timeout"] == config.BACKUP_GEOCODER_TIMEOUT_S


def test_backup_answers_when_photon_refuses_or_answers_garbage(monkeypatch):
    patch_upstreams(monkeypatch, photon=429, backup=photon_body(PROSPECT_PARK))
    assert geocode.search("prospect park", 1)[0]["label"] == "Prospect Park, Brooklyn"

    geocode.clear_caches()
    monkeypatch.setattr(geocode._session, "get", _json_error_for_photon(PROSPECT_PARK))
    assert geocode.search("prospect park", 1)[0]["label"] == "Prospect Park, Brooklyn"


def _json_error_for_photon(backup_result: dict):
    """Photon answers 200 with a body that is not JSON; the backup is fine."""
    def fake_get(url, params=None, timeout=None):
        if url.startswith(config.BACKUP_GEOCODER_URL):
            return FakeResponse(photon_body(backup_result))
        return FakeResponse(ValueError("Expecting value"))
    return fake_get


def test_photon_finding_nothing_is_an_answer_not_a_failure(monkeypatch):
    # The backup must never patch Photon's coverage, only its outages.
    calls = patch_upstreams(monkeypatch, photon=photon_body(), backup=photon_body(PROSPECT_PARK))
    assert geocode.search("zzzz", 5) == ()
    assert asked(calls, "backup") == 0


def test_while_photon_is_down_searches_do_not_wait_on_it(monkeypatch, clock, background):
    calls = patch_upstreams(monkeypatch, photon=503, backup=photon_body(PROSPECT_PARK))
    geocode.search("prospect", 5)          # discovers the outage
    assert asked(calls, "photon") == 1

    clock.advance(config.PHOTON_RECHECK_AFTER_S - 1)
    geocode.search("prospect p", 5)
    geocode.search("prospect pa", 5)
    assert asked(calls, "photon") == 1     # not asked again
    assert asked(calls, "backup") == 3
    assert background == []                # and no check before the wait is up


def test_a_due_check_runs_in_the_background_while_the_backup_answers(monkeypatch, clock, background):
    patch_upstreams(monkeypatch, photon=503, backup=photon_body(PROSPECT_PARK))
    geocode.search("prospect", 5)
    clock.advance(config.PHOTON_RECHECK_AFTER_S)

    court = feature({"name": "Court Street", "city": "New York", "state": "New York"})
    calls = patch_upstreams(monkeypatch, photon=photon_body(court),
                            backup=photon_body(PROSPECT_PARK))
    # The visitor's own search is answered by the backup, without Photon.
    assert geocode.search("prospect park", 5)[0]["label"] == "Prospect Park, Brooklyn"
    assert asked(calls, "photon") == 0
    assert len(background) == 1

    # A second search while that check is still out starts no second one.
    geocode.search("prospect park w", 5)
    assert len(background) == 1

    background.pop()()                     # the check gets its answer
    assert asked(calls, "photon") == 1
    assert calls[-1]["params"]["q"] == "prospect park"   # it asked the search in hand
    assert geocode.search("court st", 5)[0]["label"] == "Court Street, New York"
    assert asked(calls, "backup") == 2     # nothing more went to the backup


def test_a_check_that_fails_waits_before_the_next_one(monkeypatch, clock, background):
    calls = patch_upstreams(monkeypatch, photon=requests.ReadTimeout("slow"),
                            backup=photon_body(PROSPECT_PARK))
    geocode.search("prospect", 5)
    clock.advance(config.PHOTON_RECHECK_AFTER_S)
    geocode.search("prospect p", 5)
    background.pop()()                     # Photon is still down
    assert asked(calls, "photon") == 2

    geocode.search("prospect pa", 5)
    assert background == []                # the wait started over
    clock.advance(config.PHOTON_RECHECK_AFTER_S)
    geocode.search("prospect par", 5)
    assert len(background) == 1


def test_a_check_that_blows_up_does_not_block_later_checks(monkeypatch, clock, background):
    patch_upstreams(monkeypatch, photon=503, backup=photon_body(PROSPECT_PARK))
    geocode.search("prospect", 5)
    clock.advance(config.PHOTON_RECHECK_AFTER_S)
    # A 200 whose JSON is a list, which the parser was never written for.
    patch_upstreams(monkeypatch, photon=["not", "a", "collection"],
                    backup=photon_body(PROSPECT_PARK))
    geocode.search("prospect p", 5)
    background.pop()()                     # raises inside; must be swallowed

    clock.advance(config.PHOTON_RECHECK_AFTER_S)
    geocode.search("prospect pa", 5)
    assert len(background) == 1            # a new check could start


SHOP = feature({"name": "Whole Foods Market", "housenumber": "214", "street": "3rd Street",
                "district": "Brooklyn", "city": "New York", "state": "New York"})
SHOP_LABEL = "Whole Foods Market, 214 3rd Street, Brooklyn"


def test_a_backup_answer_is_never_served_once_photon_is_back(monkeypatch, clock, background):
    # During an outage the backup knows no businesses, and here Photon is
    # too slow even for the long wait. That empty answer must not stick to
    # the search after Photon recovers.
    patch_upstreams(monkeypatch, photon=requests.ReadTimeout("slow"), backup=photon_body())
    assert geocode.search("whole foods", 5) == ()

    patch_upstreams(monkeypatch, photon=photon_body(SHOP), backup=photon_body())
    clock.advance(config.PHOTON_RECHECK_AFTER_S)
    geocode.search("whole foods", 5)       # starts the check
    background.pop()()
    assert geocode.search("whole foods", 5)[0]["label"] == SHOP_LABEL


# --- a business name during an outage: the backup is empty, so Photon is
# asked once more with the long wait -------------------------------------

def patch_photon_by_speed(monkeypatch, fast, slow, backup) -> list[dict]:
    """Photon answers `fast` to a call with the normal wait and `slow` to one
    with the long wait; the backup answers `backup`. Same answer shapes as
    patch_upstreams."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        if url.startswith(config.BACKUP_GEOCODER_URL):
            upstream, answer = "backup", backup
        elif timeout == config.PHOTON_SLOW_TIMEOUT_S:
            upstream, answer = "photon-slow", slow
        else:
            upstream, answer = "photon", fast
        calls.append({"upstream": upstream, "url": url, "params": params, "timeout": timeout})
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return FakeResponse({}, answer)
        return FakeResponse(answer)

    monkeypatch.setattr(geocode._session, "get", fake_get)
    return calls


def test_an_empty_backup_answer_asks_photon_again_with_the_long_wait(monkeypatch):
    calls = patch_photon_by_speed(monkeypatch, fast=requests.ReadTimeout("slow"),
                                  slow=photon_body(SHOP), backup=photon_body())
    assert geocode.search("whole foods", 5)[0]["label"] == SHOP_LABEL
    assert [c["upstream"] for c in calls] == ["photon", "backup", "photon-slow"]
    assert calls[-1]["params"]["q"] == "whole foods"
    assert config.PHOTON_SLOW_TIMEOUT_S > config.PHOTON_TIMEOUT_S


def test_a_backup_answer_with_results_never_waits_on_photon(monkeypatch):
    calls = patch_photon_by_speed(monkeypatch, fast=503, slow=photon_body(SHOP),
                                  backup=photon_body(PROSPECT_PARK))
    assert geocode.search("prospect park", 5)[0]["label"] == "Prospect Park, Brooklyn"
    assert asked(calls, "photon-slow") == 0


def test_when_the_long_wait_fails_too_the_answer_is_nothing_found_not_an_error(monkeypatch):
    patch_photon_by_speed(monkeypatch, fast=503, slow=requests.ReadTimeout("still slow"),
                          backup=photon_body())
    assert geocode.search("whole foods", 5) == ()
    resp = client.get("/geocode", params={"q": "whole foods"})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


def test_a_slow_answer_does_not_mark_photon_up(monkeypatch, clock, background):
    calls = patch_photon_by_speed(monkeypatch, fast=503, slow=photon_body(SHOP),
                                  backup=photon_body())
    geocode.search("whole foods", 5)
    clock.advance(1)
    geocode.search("prospect park", 5)     # the backup, still: no fast Photon call
    assert asked(calls, "photon") == 1
    assert background == []


def test_a_slow_answer_is_cached_for_the_outage(monkeypatch):
    calls = patch_photon_by_speed(monkeypatch, fast=503, slow=photon_body(SHOP),
                                  backup=photon_body())
    geocode.search("whole foods", 5)
    geocode.search("whole foods", 5)
    assert asked(calls, "photon-slow") == 1


def test_a_failed_long_wait_is_not_cached(monkeypatch):
    patch_photon_by_speed(monkeypatch, fast=503, slow=requests.ReadTimeout("slow"),
                          backup=photon_body())
    assert geocode.search("whole foods", 5) == ()
    calls = patch_photon_by_speed(monkeypatch, fast=503, slow=photon_body(SHOP),
                                  backup=photon_body())
    assert geocode.search("whole foods", 5)[0]["label"] == SHOP_LABEL
    assert asked(calls, "backup") == 0     # the backup's empty answer was cached; only Photon was re-asked


def test_reverse_never_uses_the_long_wait(monkeypatch):
    # An empty reverse answer means nothing is nearby, not a business.
    calls = patch_photon_by_speed(monkeypatch, fast=503, slow=photon_body(), backup=photon_body())
    assert geocode.reverse(40.6628, -73.9690) is None
    assert asked(calls, "photon-slow") == 0


def test_the_real_background_runner_starts_a_thread():
    ran = threading.Event()
    _start_real_thread(ran.set)
    assert ran.wait(timeout=5)


def test_the_switch_and_the_recovery_are_logged_without_the_searched_text(
        monkeypatch, clock, background, caplog):
    patch_upstreams(monkeypatch, photon=503, backup=photon_body(PROSPECT_PARK))
    with caplog.at_level("INFO", logger="server.geocode"):
        geocode.search("250 court st", 5)
        geocode.search("250 court str", 5)
        clock.advance(config.PHOTON_RECHECK_AFTER_S)
        patch_upstreams(monkeypatch, photon=photon_body(), backup=photon_body(PROSPECT_PARK))
        geocode.search("250 court stre", 5)
        background.pop()()
    assert caplog.text.count("the backup geocoder answers until it recovers") == 1
    assert "Photon is answering again" in caplog.text
    assert "court" not in caplog.text


def test_a_backup_failure_is_logged_by_kind_without_the_url(monkeypatch, caplog):
    boom = requests.ConnectTimeout("timed out: https://example/autocomplete?text=250+court+st")
    patch_upstreams(monkeypatch, photon=503, backup=boom)
    with caplog.at_level("WARNING", logger="server.geocode"):
        with pytest.raises(geocode.UpstreamError):
            geocode.search("250 court st", 5)
    assert "backup geocoder unreachable: ConnectTimeout" in caplog.text
    assert "court" not in caplog.text


# --- the backup geocoder: reverse ----------------------------------------

def test_backup_reverse_takes_the_nearest_address_not_the_named_place(monkeypatch):
    # The directory lists a lot's named places ahead of its addresses.
    arena = backup_feature({"name": "BARCLAY'S CENTER & ARENA",
                            "street": "BARCLAY'S CENTER & ARENA",
                            "borough": "Brooklyn", "distance": 0.012})
    address = backup_feature({"name": "620 ATLANTIC AVENUE", "housenumber": "620",
                              "street": "ATLANTIC AVENUE", "borough": "Brooklyn",
                              "distance": 0.012})
    calls = patch_upstreams(monkeypatch, photon=503, backup=photon_body(arena, address))
    assert geocode.reverse(40.6826, -73.9754) == "620 Atlantic Avenue"
    backup_call = calls[-1]
    assert backup_call["url"] == f"{config.BACKUP_GEOCODER_URL}/reverse"
    assert backup_call["params"]["point.lat"] == 40.6826
    assert backup_call["params"]["point.lon"] == -73.9754


def test_backup_reverse_ignores_an_address_too_far_to_be_where_you_stand(monkeypatch):
    far_km = (config.BACKUP_REVERSE_MAX_DISTANCE_M + 1) / 1000
    far = backup_feature({"name": "41 EAST DRIVE", "housenumber": "41", "street": "EAST DRIVE",
                          "borough": "Brooklyn", "distance": far_km})
    nowhere = feature({"name": "United States", "layer": "country", "distance": 3195.22})
    patch_upstreams(monkeypatch, photon=503, backup=photon_body(far, nowhere))
    assert geocode.reverse(40.6628, -73.9690) is None


def test_reverse_endpoint_uses_the_backup_too(monkeypatch):
    address = backup_feature({"name": "16 MOTT STREET", "housenumber": "16",
                              "street": "MOTT STREET", "borough": "Manhattan",
                              "distance": 0.004})
    patch_upstreams(monkeypatch, photon=requests.ReadTimeout("slow"),
                    backup=photon_body(address))
    resp = client.get("/geocode/reverse", params={"lat": 40.71425, "lon": -73.99855})
    assert resp.status_code == 200
    assert resp.json() == {"label": "16 Mott Street"}


def test_search_endpoint_answers_from_the_backup_when_photon_is_down(monkeypatch):
    patch_upstreams(monkeypatch, photon=requests.ReadTimeout("slow"),
                    backup=photon_body(EMPIRE_STATE))
    resp = client.get("/geocode", params={"q": "350 fifth ave", "limit": 5})
    assert resp.status_code == 200
    assert resp.json() == {"results": [
        {"lat": 40.7484, "lon": -73.9857, "label": "350 Fifth Avenue, Manhattan"}]}
