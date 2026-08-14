"""Tests for pipeline/fetch/interior_sidewalks.py's fetch/cache/pagination
behavior. No real network call -- mirrors the mocking style in
test_pipeline_fetch_boundaries.py. Real data was checked manually against
the live ArcGIS FeatureServer (FIXES.md item 1a): the 2,000-row
maxRecordCount cap, the ~43,843-segment citywide total, the STATUS
field's real value distribution (New/Updated/Unchanged, no filtering
needed), and the outSR=4326 server-side reprojection were all confirmed
live, not assumed."""

import json

from pipeline.fetch import interior_sidewalks

FAKE_SEGMENT = {
    "type": "Feature",
    "properties": {"OBJECTID": 1, "SOURCE_ID": 1, "STATUS": "Unchanged"},
    "geometry": {"type": "LineString", "coordinates": [[-73.95, 40.78], [-73.94, 40.79]]},
}


def _fake_page(features):
    return {"type": "FeatureCollection", "features": features}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_interior_sidewalks_downloads_a_single_page_and_caches(monkeypatch, tmp_path):
    cache_path = tmp_path / "interior_sidewalks_2022.geojson"
    monkeypatch.setattr(interior_sidewalks, "CACHE_PATH", cache_path)

    calls = []

    def fake_get(url, params, timeout):
        calls.append(params)
        return _FakeResponse(_fake_page([FAKE_SEGMENT]))

    monkeypatch.setattr(interior_sidewalks.requests, "get", fake_get)

    result = interior_sidewalks.fetch_interior_sidewalks()

    assert result["features"] == [FAKE_SEGMENT]
    assert len(calls) == 1  # one page, fewer rows than PAGE_SIZE -- stop
    assert json.loads(cache_path.read_text()) == result


def test_fetch_interior_sidewalks_pages_past_the_server_row_cap(monkeypatch, tmp_path):
    """The real server caps at 2,000 rows/request but the dataset has
    ~43,843 segments (confirmed live) -- unlike boundaries.py/parks.py's
    single-request fetch, this needs a real pagination loop. Uses a small
    PAGE_SIZE here to keep the test fast, not the real 2,000."""
    cache_path = tmp_path / "interior_sidewalks_2022.geojson"
    monkeypatch.setattr(interior_sidewalks, "CACHE_PATH", cache_path)
    monkeypatch.setattr(interior_sidewalks, "PAGE_SIZE", 2)

    pages = [
        _fake_page([FAKE_SEGMENT, FAKE_SEGMENT]),  # full page -- keep going
        _fake_page([FAKE_SEGMENT]),                # partial page -- last one
    ]
    offsets_requested = []

    def fake_get(url, params, timeout):
        offsets_requested.append(params["resultOffset"])
        return _FakeResponse(pages.pop(0))

    monkeypatch.setattr(interior_sidewalks.requests, "get", fake_get)

    result = interior_sidewalks.fetch_interior_sidewalks()

    assert len(result["features"]) == 3
    assert offsets_requested == [0, 2]


def test_fetch_interior_sidewalks_reads_cache_without_a_network_call(monkeypatch, tmp_path):
    cache_path = tmp_path / "interior_sidewalks_2022.geojson"
    cached = _fake_page([FAKE_SEGMENT])
    cache_path.write_text(json.dumps(cached))
    monkeypatch.setattr(interior_sidewalks, "CACHE_PATH", cache_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not hit the network on a cache hit")

    monkeypatch.setattr(interior_sidewalks.requests, "get", fail_if_called)

    assert interior_sidewalks.fetch_interior_sidewalks() == cached


def test_fetch_interior_sidewalks_refresh_forces_a_new_download(monkeypatch, tmp_path):
    cache_path = tmp_path / "interior_sidewalks_2022.geojson"
    cache_path.write_text(json.dumps({"stale": True}))
    monkeypatch.setattr(interior_sidewalks, "CACHE_PATH", cache_path)
    monkeypatch.setattr(
        interior_sidewalks.requests, "get", lambda *a, **k: _FakeResponse(_fake_page([FAKE_SEGMENT]))
    )

    result = interior_sidewalks.fetch_interior_sidewalks(refresh=True)

    assert result["features"] == [FAKE_SEGMENT]


def test_fetch_interior_sidewalks_requests_wgs84_output(monkeypatch, tmp_path):
    """The source data is natively EPSG:2263 (State Plane feet); the fetch
    relies on the server's own outSR=4326 reprojection rather than a
    separate pyproj step -- confirmed this works against the live API
    (FIXES.md item 1a). Pinned here so a future edit can't silently drop
    the param and start receiving feet instead of lon/lat degrees."""
    cache_path = tmp_path / "interior_sidewalks_2022.geojson"
    monkeypatch.setattr(interior_sidewalks, "CACHE_PATH", cache_path)

    seen_params = {}

    def fake_get(url, params, timeout):
        seen_params.update(params)
        return _FakeResponse(_fake_page([FAKE_SEGMENT]))

    monkeypatch.setattr(interior_sidewalks.requests, "get", fake_get)

    interior_sidewalks.fetch_interior_sidewalks()

    assert seen_params["outSR"] == "4326"
    assert seen_params["f"] == "geojson"
