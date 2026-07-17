"""Tests for pipeline/fetch/boundaries.py's cache behavior. No real network
call -- mirrors the mocking style in test_pipeline_fetch_streets.py. The
real dataset's own shape (Governors Island genuinely sitting inside
Manhattan's polygon, Jersey City genuinely excluded from the NYC union)
was checked manually against the live fetch, per PLAN.md's convention of
keeping network-dependent checks out of the automated suite."""

import json

from pipeline.fetch import boundaries

FAKE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"boroname": "Test Borough"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
    ],
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_borough_boundaries_downloads_and_caches(monkeypatch, tmp_path):
    cache_path = tmp_path / "borough_boundaries.geojson"
    monkeypatch.setattr(boundaries, "CACHE_PATH", cache_path)
    monkeypatch.setattr(boundaries.requests, "get", lambda *a, **k: _FakeResponse(FAKE_GEOJSON))

    result = boundaries.fetch_borough_boundaries()

    assert result == FAKE_GEOJSON
    assert json.loads(cache_path.read_text()) == FAKE_GEOJSON


def test_fetch_borough_boundaries_reads_cache_without_a_network_call(monkeypatch, tmp_path):
    cache_path = tmp_path / "borough_boundaries.geojson"
    cache_path.write_text(json.dumps(FAKE_GEOJSON))
    monkeypatch.setattr(boundaries, "CACHE_PATH", cache_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not hit the network on a cache hit")

    monkeypatch.setattr(boundaries.requests, "get", fail_if_called)

    assert boundaries.fetch_borough_boundaries() == FAKE_GEOJSON


def test_fetch_borough_boundaries_refresh_forces_a_new_download(monkeypatch, tmp_path):
    cache_path = tmp_path / "borough_boundaries.geojson"
    cache_path.write_text(json.dumps({"stale": True}))
    monkeypatch.setattr(boundaries, "CACHE_PATH", cache_path)
    monkeypatch.setattr(boundaries.requests, "get", lambda *a, **k: _FakeResponse(FAKE_GEOJSON))

    assert boundaries.fetch_borough_boundaries(refresh=True) == FAKE_GEOJSON
