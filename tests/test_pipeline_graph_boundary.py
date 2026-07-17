"""Tests for pipeline/graph/boundary.py's polygon-union logic. Two
synthetic squares stand in for the real 5-borough dataset -- exercising
the union/containment logic without needing network access or the real
3MB file in the test suite."""

from shapely.geometry import Point

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
