"""Tests for server/coverage_frame.py -- the offshore frame (FIXES 12).

The frame is derived at load from live coverage rings against COMMITTED
political geometry (server/frame_inputs.json), so these run offline: no
network, no citywide tiles. Shape checks use a small synthetic coverage
square in Brooklyn -- the political inputs are the real committed ones,
which is the point (they're part of the contract under test).
"""

import logging

from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform, unary_union

from pipeline.config import METRIC_CRS
from server import coverage_frame, graph_store

_TO_M = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform

# A ~1km coverage square in brownstone Brooklyn (Carroll Gardens-ish),
# comfortably inside the city's land -- closed ring, [lon, lat].
BROOKLYN_SQUARE = [
    [-73.999, 40.678], [-73.988, 40.678], [-73.988, 40.686],
    [-73.999, 40.686], [-73.999, 40.678],
]


def _build():
    return coverage_frame.build_frame([BROOKLYN_SQUARE])


def test_frame_contains_the_coverage_it_frames():
    # The frame is the generous OUTER boundary -- coverage poking outside
    # it would draw routable streets in the dimmed zone.
    result = _build()
    frame_m = unary_union([
        Polygon([_TO_M(lon, lat) for lon, lat in ring]) for ring in result["frame"]
    ])
    coverage_m = Polygon([_TO_M(lon, lat) for lon, lat in BROOKLYN_SQUARE])
    assert frame_m.contains(coverage_m)


def test_frame_stays_inside_the_committed_political_clip():
    # The whole point of the clip: the frame may sit in NYC's own land
    # and jurisdiction water (plus the curated corridors) but never enter
    # New Jersey / Nassau / Westchester. Small tolerance for the
    # post-clip simplification.
    result = _build()
    clip_m, _ = coverage_frame._inputs_m()
    frame_m = unary_union([
        Polygon([_TO_M(lon, lat) for lon, lat in ring]) for ring in result["frame"]
    ])
    outside = frame_m.difference(clip_m.buffer(coverage_frame.SIMPLIFY_M * 2))
    assert outside.area < 1.0, f"frame leaks {outside.area:.1f} m^2 outside the political clip"


def test_feathers_nest_outward_from_the_frame():
    # The dim fades OUTWARD: frame within feather_350 within feather_800.
    result = _build()

    def as_geom(rings):
        return unary_union([Polygon([_TO_M(lon, lat) for lon, lat in r]) for r in rings])

    frame_m = as_geom(result["frame"])
    f350_m = as_geom(result["feather_350"])
    f800_m = as_geom(result["feather_800"])
    # simplify() can nibble a few meters; buffer the outer side slightly.
    assert f350_m.buffer(coverage_frame.SIMPLIFY_M).contains(frame_m)
    assert f800_m.buffer(coverage_frame.SIMPLIFY_M).contains(f350_m)


def test_frame_params_are_part_of_the_coverage_fingerprint(tmp_path, monkeypatch, caplog):
    # Same stale-cache discipline as the hide rule: editing the frame
    # recipe must invalidate cached frames like a re-exported tile does.
    (tmp_path / "fake.json.gz").write_bytes(b"placeholder tile bytes")
    paths = sorted(tmp_path.glob("*.json.gz"))
    before = graph_store._tiles_fingerprint(paths)
    monkeypatch.setattr(coverage_frame, "FRAME_PARAMS", "frame:v999-test")
    assert graph_store._tiles_fingerprint(paths) != before


def test_build_frame_logs_its_ring_count(caplog):
    # The load-time log line is how a bad frame (0 rings, 50 rings) gets
    # noticed on a server boot -- keep it emitting.
    with caplog.at_level(logging.INFO):
        _build()
    assert any("[coverage_frame]" in r.message for r in caplog.records)
