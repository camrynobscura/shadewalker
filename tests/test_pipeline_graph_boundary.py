"""Tests for pipeline/graph/boundary.py's polygon-union and tile-selection
logic. Two synthetic squares stand in for the real 5-borough dataset --
exercising the union/containment logic without needing network access or
the real 3MB file in the test suite."""

import networkx as nx
import pytest
from shapely.geometry import Point, box, shape

from pipeline import config
from pipeline.graph import boundary

TWO_SQUARES = {
    "type": "FeatureCollection",
    "features": [
        {
            "properties": {"boroname": "A"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
        {
            "properties": {"boroname": "B"},
            "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]},
        },
    ],
}


def test_nyc_boundary_unions_every_feature():
    union = boundary.nyc_boundary(TWO_SQUARES)
    assert union.contains(Point(0.5, 0.5))  # inside square A
    assert union.contains(Point(2.5, 0.5))  # inside square B


def test_nyc_boundary_excludes_the_gap_between_features():
    union = boundary.nyc_boundary(TWO_SQUARES)
    assert not union.contains(Point(1.5, 0.5))  # between the two squares, inside neither


def test_nyc_boundary_excludes_points_outside_every_feature():
    union = boundary.nyc_boundary(TWO_SQUARES)
    assert not union.contains(Point(10, 10))


def test_borough_polygon_matches_by_name_case_insensitively():
    result = boundary.borough_polygon(TWO_SQUARES, "a")
    assert result.equals(shape(TWO_SQUARES["features"][0]["geometry"]))


def test_borough_polygon_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="No borough named"):
        boundary.borough_polygon(TWO_SQUARES, "nonexistent")


TWO_PARKS_ONE_EXCLUDED = {
    "type": "FeatureCollection",
    "features": [
        {
            "properties": {"typecategory": "Flagship Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
        {
            "properties": {"typecategory": "Parkway"},  # excluded: roadside median
            "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]},
        },
    ],
}


def test_park_polygon_includes_a_real_park():
    result = boundary.park_polygon(TWO_PARKS_ONE_EXCLUDED)
    assert result.contains(Point(0.5, 0.5))


def test_park_polygon_excludes_a_roadside_typecategory():
    result = boundary.park_polygon(TWO_PARKS_ONE_EXCLUDED)
    assert not result.contains(Point(2.5, 0.5))


# FIXES.md 1d: typecategory alone bundles real park land in with genuinely
# non-park land under the same label. Confirmed against the real dataset
# (2026-08-08) that `subcategory` reliably tells the two apart -- but only
# for the typecategories where a real bundling error was actually found
# (Buildings/Institutions, Triangle/Plaza, Mall). Parkway/Strip/Lot/
# Operations/Retired N/A keep their blanket exclusion regardless of
# subcategory -- see PARKWAY_WITH_A_PARK_LIKE_SUBCATEGORY below for why.
BUILDINGS_INSTITUTIONS_MIXED = {
    "type": "FeatureCollection",
    "features": [
        {
            # Real: Theodore Roosevelt Park (17.6 acres) is bundled under
            # "Buildings/Institutions", but its own subcategory reads
            # "Large Park".
            "properties": {"typecategory": "Buildings/Institutions", "subcategory": "Large Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
        {
            # Real: a small pool building under the same typecategory,
            # correctly non-park (e.g. Kosciuszko Pool, subcategory
            # "Building").
            "properties": {"typecategory": "Buildings/Institutions", "subcategory": "Building"},
            "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]},
        },
    ],
}


def test_park_polygon_includes_a_real_park_mislabeled_under_an_excluded_typecategory():
    result = boundary.park_polygon(BUILDINGS_INSTITUTIONS_MIXED)
    assert result.contains(Point(0.5, 0.5))


def test_park_polygon_still_excludes_a_genuine_building_under_the_same_typecategory():
    result = boundary.park_polygon(BUILDINGS_INSTITUTIONS_MIXED)
    assert not result.contains(Point(2.5, 0.5))


TRIANGLE_PLAZA_MIXED = {
    "type": "FeatureCollection",
    "features": [
        {
            # Real: Grand Army Plaza (14.3 acres, Prospect Park's entrance)
            # is bundled under "Triangle/Plaza", but its own subcategory
            # reads "Flagship Park".
            "properties": {"typecategory": "Triangle/Plaza", "subcategory": "Flagship Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
        {
            # Real: an ordinary traffic triangle under the same
            # typecategory -- the vast majority of it (324 of ~355
            # properties share this exact subcategory).
            "properties": {"typecategory": "Triangle/Plaza", "subcategory": "Sitting Area/Triangle/Mall"},
            "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]},
        },
    ],
}


def test_park_polygon_includes_grand_army_plaza_style_flagship_park():
    result = boundary.park_polygon(TRIANGLE_PLAZA_MIXED)
    assert result.contains(Point(0.5, 0.5))


def test_park_polygon_still_excludes_an_ordinary_traffic_triangle():
    result = boundary.park_polygon(TRIANGLE_PLAZA_MIXED)
    assert not result.contains(Point(2.5, 0.5))


MALL_MIXED = {
    "type": "FeatureCollection",
    "features": [
        {
            # Real: Ocean Parkway Malls (140 acres, the historic Olmsted
            # median walkway) is bundled under "Mall", but its own
            # subcategory reads "Large Park".
            "properties": {"typecategory": "Mall", "subcategory": "Large Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
        {
            # Real: an ordinary street mall/median under the same
            # typecategory (e.g. Park Avenue Malls) -- correctly excluded.
            "properties": {"typecategory": "Mall", "subcategory": "Sitting Area/Triangle/Mall"},
            "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]]},
        },
    ],
}


def test_park_polygon_includes_ocean_parkway_malls_style_large_park():
    result = boundary.park_polygon(MALL_MIXED)
    assert result.contains(Point(0.5, 0.5))


def test_park_polygon_still_excludes_an_ordinary_street_mall():
    result = boundary.park_polygon(MALL_MIXED)
    assert not result.contains(Point(2.5, 0.5))


PARKWAY_WITH_A_PARK_LIKE_SUBCATEGORY = {
    "type": "FeatureCollection",
    "features": [
        {
            # Real: Belt Parkway/Shore Parkway (760 acres) carries
            # subcategory "Large Park" despite being a highway median, not
            # walkable park interior -- already confirmed correctly
            # excluded. This isn't a per-property bundling mistake the way
            # Buildings/Institutions is; it's a genuinely different land
            # type that happens to share a subcategory label, so the
            # subcategory override must not rescue it.
            "properties": {"typecategory": "Parkway", "subcategory": "Large Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        },
    ],
}


def test_park_polygon_excludes_a_parkway_median_even_with_a_park_like_subcategory():
    result = boundary.park_polygon(PARKWAY_WITH_A_PARK_LIKE_SUBCATEGORY)
    assert not result.contains(Point(0.5, 0.5))


SELF_INTERSECTING_PARK = {
    "type": "FeatureCollection",
    "features": [
        {
            # A "bowtie" ring -- crosses itself at (1, 1), same defect
            # shape as the 9 real Parks Properties polygons (including
            # John V. Lindsay East River Park) that crashed a later
            # .intersection() call with GEOS's "TopologyException: side
            # location conflict" during the citywide park-canopy re-score.
            "properties": {"typecategory": "Flagship Park"},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
        },
    ],
}


def test_park_polygon_repairs_a_self_intersecting_ring():
    result = boundary.park_polygon(SELF_INTERSECTING_PARK)
    assert result.is_valid
    # Must survive a real intersection() call, not just return without
    # raising -- this is the exact operation that crashed on the
    # unrepaired geometry.
    result.intersection(box(0, 0, 1, 1))


def _tile_box(row: int, col: int):
    b = config.get_tile_bbox(f"r{row}c{col}")
    return box(b.lon_min, b.lat_min, b.lon_max, b.lat_max)


def test_tile_ids_for_polygon_matches_the_bbox_result_for_a_literal_rectangle():
    # A polygon shaped exactly like a rectangle should select exactly the
    # same tiles get_tile_ids_for_bbox() already does -- proves the extra
    # intersection filtering doesn't change behavior for the degenerate
    # (rectangular) case.
    rect_bbox = config.Bbox(lat_min=40.570, lat_max=40.740, lon_min=-74.045, lon_max=-73.833)
    rect = box(rect_bbox.lon_min, rect_bbox.lat_min, rect_bbox.lon_max, rect_bbox.lat_max)
    assert set(boundary.tile_ids_for_polygon(rect)) == set(config.get_tile_ids_for_bbox(rect_bbox))


def test_tile_ids_for_polygon_excludes_tiles_the_polygon_never_touches():
    # Two tiles' worth of squares placed on opposite corners of a 3x3
    # block. Their bounding box spans all 9 tiles (the naive bbox-only
    # pass), but the squares themselves -- plus whatever they happen to
    # touch at a shared edge or corner -- only really reach 7 of them;
    # r0c2 and r2c0 (the two *other* corners) share no area, edge, or
    # point with either square. Real numbers confirmed by direct
    # computation before pinning this assertion, not derived by hand.
    square1, square2 = _tile_box(0, 0), _tile_box(2, 2)
    two_corners = square1.union(square2)

    candidate_bbox = config.Bbox(
        lat_min=config.CITY_BBOX.lat_min,
        lat_max=config.CITY_BBOX.lat_min + 3 * config.TILE_SIZE_LAT_DEG,
        lon_min=config.CITY_BBOX.lon_min,
        lon_max=config.CITY_BBOX.lon_min + 3 * config.TILE_SIZE_LON_DEG,
    )
    naive_candidates = set(config.get_tile_ids_for_bbox(candidate_bbox))
    assert naive_candidates == {f"r{r}c{c}" for r in range(3) for c in range(3)}

    result = set(boundary.tile_ids_for_polygon(two_corners))
    assert result == naive_candidates - {"r0c2", "r2c0"}


def _make_street_graph():
    """Two nodes inside a unit square, one outside, an edge crossing the
    boundary and an edge staying entirely inside."""
    graph = nx.MultiDiGraph()
    graph.add_node("inside_1", x=0.5, y=0.5)
    graph.add_node("inside_2", x=0.8, y=0.2)
    graph.add_node("outside_1", x=5.0, y=5.0)
    graph.add_edge("inside_1", "inside_2", key=0)
    graph.add_edge("inside_1", "outside_1", key=0)
    return graph


def test_clip_to_nyc_drops_nodes_outside_the_boundary():
    nyc_shape = box(0, 0, 1, 1)
    clipped = boundary.clip_to_nyc(_make_street_graph(), nyc_shape)
    assert set(clipped.nodes) == {"inside_1", "inside_2"}


def test_clip_to_nyc_drops_edges_incident_to_a_removed_node():
    nyc_shape = box(0, 0, 1, 1)
    clipped = boundary.clip_to_nyc(_make_street_graph(), nyc_shape)
    assert not clipped.has_edge("inside_1", "outside_1")
    assert clipped.has_edge("inside_1", "inside_2")


def test_clip_to_nyc_does_not_mutate_the_original_graph():
    graph = _make_street_graph()
    boundary.clip_to_nyc(graph, box(0, 0, 1, 1))
    assert "outside_1" in graph.nodes
