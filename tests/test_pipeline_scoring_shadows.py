"""Tests for pipeline/scoring/shadows.py on synthetic geometry with known answers.

Worlds are laid out in metres and converted to lon/lat through the inverse
of naming._to_m, so the end-to-end path (edge dicts with `coords`, raw
footprint rows with `the_geom` + `height_roof` in feet) is what runs.
Geometry keeps >= 3 m clear of every shadow edge: the raster march places
a shadow's edge only to within a cell plus a hop, and that positional
error is measured by the Gate 1 instrument, not asserted here.

What is pinned:
  - the physics on one building (9 m shaded, 11 m not, at 45 deg), on BOTH
    engines, and that the engines agree away from shadow edges;
  - direction: a building west of a N-S street shades the near sidewalk
    with the sun in the west and nothing with the sun in the east;
  - night is 0; a crossing is scored like a sidewalk; a point inside a
    footprint is excluded, not counted either way;
  - the strip rule: a shadow covering two of the three lines reads 2/3;
  - the sweep path reaches where the march cannot, and the reach cap holds;
  - shadows cross tile borders;
  - the output shape: 288 bytes, month-major.
"""

import math

import numpy as np
import pytest
from shapely.geometry import box, mapping
from shapely.strtree import STRtree

from pipeline import config
from pipeline.graph.naming import _K, _LAT_M
from pipeline.scoring import shadows

M_PER_FT = 0.3048


# ── world builders ───────────────────────────────────────────────────────────

def _lonlat(x_m: float, y_m: float) -> list[float]:
    return [x_m / _K, y_m / _LAT_M]


def _edge(points_m, kind="footway/sidewalk") -> dict:
    return {"coords": [_lonlat(x, y) for x, y in points_m], "kind": kind}


def _building(x0, y0, x1, y1, height_m) -> dict:
    corners = [_lonlat(x0, y0), _lonlat(x1, y0), _lonlat(x1, y1), _lonlat(x0, y1)]
    geom = {"type": "MultiPolygon", "coordinates": [[corners + [corners[0]]]]}
    return {"the_geom": geom, "height_roof": f"{height_m / M_PER_FT:.4f}",
            "bin": "1", "feature_code": "2100", "last_status_type": "Constructed"}


def _table(**slots):
    """A 12x24 sun table, all night except the given (month, hour) slots:
    _table(m7h12=(180.0, 45.0))."""
    table = [[None] * 24 for _ in range(12)]
    for key, (az, el) in slots.items():
        month, hour = key[1:].split("h")
        table[int(month) - 1][int(hour)] = [az, el]
    return table


def _slot(month, hour):
    return (month - 1) * 24 + hour


def _values(edge, month, hour) -> int:
    return edge["building_shade"][_slot(month, hour)]


def _score(edges, buildings, table, **overrides):
    return shadows.score_building_shade(edges, buildings, table, **overrides)


# ── sample points ────────────────────────────────────────────────────────────

def test_sample_points_sit_at_slice_centres_three_across():
    edge = _edge([(0, 0), (10, 0)])
    points, owner = shadows.sample_points([edge], step_m=5.0, offsets_m=(-1.0, 0.0, 1.0))
    assert len(points) == 6 and set(owner) == {0}
    xs = sorted(set(np.round(points[:, 0], 6)))
    ys = sorted(set(np.round(points[:, 1], 6)))
    assert xs == pytest.approx([2.5, 7.5])       # slice centres, never the midpoint alone
    assert ys == pytest.approx([-1.0, 0.0, 1.0])  # across the strip


def test_sample_points_skip_degenerate_edges():
    points, owner = shadows.sample_points([_edge([(0, 0)]), _edge([(3, 3), (3, 3)])])
    assert len(points) == 0 and len(owner) == 0


# ── the physics, both engines ────────────────────────────────────────────────

def _one_building_world():
    """A 10 m tall building due south of the origin: x in [-5, 5], y in [-10, 0]."""
    geom = box(-5, -10, 5, 0)
    return np.array([geom], dtype=object), np.array([10.0])


def test_raster_march_shades_9m_north_not_11m_at_45_degrees():
    geoms, heights = _one_building_world()
    grid, x0, y_top = shadows.build_height_grid(geoms, heights, (-20, -20, 20, 20), 2.0)
    points = np.array([[0.0, 9.0], [0.0, 11.0], [0.0, 30.0]])
    shaded = shadows.raster_shaded(points, grid, x0, y_top, 2.0, azimuth=180.0,
                                   elevation=45.0, march_step_m=2.0,
                                   height_cap_m=100.0, max_reach_m=1000.0)
    assert shaded.tolist() == [True, False, False]


def test_sweep_shades_9m_north_not_11m_at_45_degrees():
    geoms, heights = _one_building_world()
    points = shapely_points([[0.0, 9.0], [0.0, 11.0], [0.0, 30.0]])
    shaded = shadows.sweep_shaded(STRtree(points), 3, geoms, heights, azimuth=180.0,
                                  elevation=45.0, max_reach_m=1000.0)
    assert shaded.tolist() == [True, False, False]


def shapely_points(xy):
    import shapely
    return shapely.points(np.asarray(xy, dtype=float))


def test_engines_agree_away_from_shadow_edges():
    # A 40 x 40 m, 30 m tall block: its 52 m shadow at 30 deg holds hundreds
    # of random points even after the boundary band is removed.
    geoms, heights = np.array([box(-20, -40, 20, 0)], dtype=object), np.array([30.0])
    grid, x0, y_top = shadows.build_height_grid(geoms, heights, (-100, -100, 100, 100), 2.0)
    rng = np.random.default_rng(20260924)
    points = rng.uniform(-90, 90, size=(6000, 2))
    az, el = 225.0, 30.0
    exact = shadows.shadow_polygon(geoms[0], heights[0], az, el, 1000.0)
    # keep points >= 3 m clear of the true shadow boundary (and of the footprint)
    clear = np.array([exact.boundary.distance(p) >= 3.0 and not geoms[0].contains(p)
                      for p in shapely_points(points)])
    points = points[clear]
    r = shadows.raster_shaded(points, grid, x0, y_top, 2.0, az, el, 2.0, 100.0, 1000.0)
    s = shadows.sweep_shaded(STRtree(shapely_points(points)), len(points), geoms, heights,
                             az, el, 1000.0)
    assert r.sum() > 100          # the case exercises real shade, not two empty arrays
    assert np.array_equal(r, s)


def test_night_and_no_buildings_are_never_shaded():
    geoms, heights = _one_building_world()
    grid, x0, y_top = shadows.build_height_grid(geoms, heights, (-20, -20, 20, 20), 2.0)
    points = np.array([[0.0, 5.0]])
    assert not shadows.raster_shaded(points, grid, x0, y_top, 2.0, 180.0, 0.0, 2.0, 100.0, 1000.0)[0]
    assert not shadows.raster_shaded(points, grid, x0, y_top, 2.0, 180.0, -5.0, 2.0, 100.0, 1000.0)[0]
    empty_grid, ex0, ey = shadows.build_height_grid(np.array([], dtype=object), np.array([]),
                                                    (-20, -20, 20, 20), 2.0)
    assert not shadows.raster_shaded(points, empty_grid, ex0, ey, 2.0, 180.0, 45.0, 2.0, 100.0, 1000.0)[0]


# ── end to end: direction, kinds, exclusion, output ──────────────────────────

def _street_world():
    """A N-S street. One 10 m building on the WEST side (x in [-14, -4]);
    the west sidewalk 2 m from its wall at x = -2, the east sidewalk at
    x = +14, both running y in [-30, 30]."""
    west = _edge([(-2, -30), (-2, 30)])
    east = _edge([(14, -30), (14, 30)])
    building = _building(-14, -40, -4, 40, height_m=10.0)
    return west, east, building


def test_sun_in_the_west_shades_the_near_sidewalk_only():
    west, east, building = _street_world()
    # tan(el) = 1.25 -> shadow length 8 m: covers x in [-4, 4]
    el = math.degrees(math.atan(1.25))
    _score([west, east], [building], _table(m7h16=(270.0, el)))
    assert _values(west, 7, 16) == 255
    assert _values(east, 7, 16) == 0


def test_sun_in_the_east_shades_neither_sidewalk():
    west, east, building = _street_world()
    el = math.degrees(math.atan(1.25))
    _score([west, east], [building], _table(m7h8=(90.0, el)))
    assert _values(west, 7, 8) == 0
    assert _values(east, 7, 8) == 0


def test_night_slots_are_zero_and_daylight_slot_count_is_reported():
    west, east, building = _street_world()
    el = math.degrees(math.atan(1.25))
    tally = _score([west, east], [building], _table(m7h16=(270.0, el)))
    assert tally["slots_daylight"] == 1
    night = [v for i, v in enumerate(west["building_shade"]) if i != _slot(7, 16)]
    assert set(night) == {0}


def test_a_crossing_is_scored_like_a_sidewalk():
    west, _, building = _street_world()
    crossing = _edge([(-2, -30), (-2, 30)], kind="footway/crossing")
    el = math.degrees(math.atan(1.25))
    _score([west, crossing], [building], _table(m7h16=(270.0, el)))
    assert _values(crossing, 7, 16) == _values(west, 7, 16) == 255


def test_points_inside_a_footprint_are_excluded_not_counted():
    west, _, building = _street_world()
    arcade = _edge([(-9, -30), (-9, 30)])          # runs through the building
    el = math.degrees(math.atan(1.25))
    tally = _score([west, arcade], [building], _table(m7h16=(270.0, el)))
    arcade_points = len(shadows.sample_points([arcade])[0])
    assert tally["points_inside_footprints"] == arcade_points
    assert tally["edges_without_points"] == 1
    assert _values(arcade, 7, 16) == 0
    assert _values(west, 7, 16) == 255              # the neighbour is unaffected


def test_output_is_288_bytes_month_major():
    west, east, building = _street_world()
    el = math.degrees(math.atan(1.25))
    _score([west, east], [building], _table(m3h9=(270.0, el), m11h15=(270.0, el)))
    assert isinstance(west["building_shade"], bytes)
    assert len(west["building_shade"]) == 288
    lit = {i for i, v in enumerate(west["building_shade"]) if v}
    assert lit == {_slot(3, 9), _slot(11, 15)}


# ── the strip rule, the sweep path, the reach cap, tiles ─────────────────────

def _tall_world(distance_m: float, height_m: float):
    """A building of `height_m` whose east wall is `distance_m` west of a
    N-S edge at x = 0; sun due west."""
    edge = _edge([(0, -20), (0, 20)])
    building = _building(-distance_m - 30, -60, -distance_m, 60, height_m=height_m)
    return edge, building


def test_strip_rule_two_of_three_lines_shaded_reads_two_thirds():
    # 120 m building (above the raster cap), shadow length 300.5 m: covers
    # the -1 m and 0 m lines of an edge 300 m away, not the +1 m line.
    edge, building = _tall_world(300.0, 120.0)
    el = math.degrees(math.atan(120.0 / 300.5))
    _score([edge], [building], _table(m7h17=(270.0, el)))
    assert _values(edge, 7, 17) == round(255 * 2 / 3)


def test_sweep_path_reaches_beyond_the_raster_march():
    # At el = 20 deg the march stops at cap / tan(el) = 251 m; a 120 m
    # building 300 m away throws a 330 m shadow only the sweep path sees.
    edge, tall = _tall_world(300.0, 120.0)
    _score([edge], [tall], _table(m7h17=(270.0, 20.0)))
    assert _values(edge, 7, 17) == 255
    # The same footprint just UNDER the cap gets no sweep: unshaded, by design.
    edge, short = _tall_world(300.0, 90.0)
    _score([edge], [short], _table(m7h17=(270.0, 20.0)))
    assert _values(edge, 7, 17) == 0


def test_max_reach_caps_both_paths():
    edge, tall = _tall_world(300.0, 120.0)
    _score([edge], [tall], _table(m7h17=(270.0, 20.0)), max_reach_m=200.0)
    assert _values(edge, 7, 17) == 0


def test_shadows_cross_tile_borders():
    # 50 m tiles: the building sits in tile -1, the edge in tile 0.
    edge = _edge([(10, -20), (10, 20)])
    building = _building(-30, -40, -20, 40, height_m=40.0)      # 30 m from the edge
    el = math.degrees(math.atan(40.0 / 50.0))                   # shadow 50 m long
    tally = _score([edge], [building], _table(m7h16=(270.0, el)), tile_m=50.0,
                   max_reach_m=100.0)
    assert tally["tiles"] >= 1
    assert _values(edge, 7, 16) == 255


def test_prepare_buildings_converts_feet_and_projects():
    row = _building(0, 0, 10, 20, height_m=30.0)
    geoms, heights = shadows.prepare_buildings([row])
    assert heights[0] == pytest.approx(30.0, abs=1e-3)
    minx, miny, maxx, maxy = geoms[0].bounds
    assert (maxx - minx, maxy - miny) == pytest.approx((10.0, 20.0), abs=1e-6)
