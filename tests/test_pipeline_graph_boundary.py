"""Tests for pipeline/graph/boundary.py's polygon-union and tile-selection
logic. Two synthetic squares stand in for the real 5-borough dataset --
exercising the union/containment logic without needing network access or
the real 3MB file in the test suite."""

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


def _tile_box(row: int, col: int):
    b = config.get_tile_bbox(f"r{row}c{col}")
    return box(b.lon_min, b.lat_min, b.lon_max, b.lat_max)


def test_tile_ids_for_polygon_matches_the_bbox_result_for_a_literal_rectangle():
    # A polygon shaped exactly like a rectangle should select exactly the
    # same tiles get_tile_ids_for_bbox() already does -- proves the extra
    # intersection filtering doesn't change behavior for the degenerate
    # (already-shipped, Brooklyn-shaped) case.
    brooklyn = config.BOROUGH_BBOXES["brooklyn"]
    rect = box(brooklyn.lon_min, brooklyn.lat_min, brooklyn.lon_max, brooklyn.lat_max)
    assert set(boundary.tile_ids_for_polygon(rect)) == set(config.get_tile_ids_for_bbox(brooklyn))


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
