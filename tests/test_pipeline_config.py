"""Tests for pipeline/config.py's tile-grid resolution (get_tile_bbox(),
get_tile_ids_for_bbox()) and the Brooklyn borough extent."""

import pytest

from pipeline import config


def test_get_tile_bbox_returns_the_pilot_bbox_for_pilot():
    assert config.get_tile_bbox("pilot") == config.PILOT_BBOX


def test_get_tile_bbox_rejects_unknown_tile_ids():
    with pytest.raises(ValueError, match="Unknown tile id"):
        config.get_tile_bbox("not-a-real-id")


def test_get_tile_bbox_parses_grid_ids_relative_to_city_bbox():
    bbox = config.get_tile_bbox("r0c0")
    assert bbox.lat_min == config.CITY_BBOX.lat_min
    assert bbox.lon_min == config.CITY_BBOX.lon_min
    assert bbox.lat_max == pytest.approx(config.CITY_BBOX.lat_min + config.TILE_SIZE_LAT_DEG)
    assert bbox.lon_max == pytest.approx(config.CITY_BBOX.lon_min + config.TILE_SIZE_LON_DEG)


def test_get_tile_bbox_grid_ids_tile_without_gaps_or_overlap():
    # r0c0's north edge should be exactly r1c0's south edge.
    below = config.get_tile_bbox("r0c0")
    above = config.get_tile_bbox("r1c0")
    assert above.lat_min == pytest.approx(below.lat_max)


def test_get_tile_bbox_rejects_grid_ids_outside_city_bbox():
    with pytest.raises(ValueError, match="outside CITY_BBOX"):
        config.get_tile_bbox("r99999c0")


def test_get_tile_ids_for_bbox_single_tile():
    tile = config.get_tile_bbox("r5c5")
    # A point well inside one tile's box should resolve to just that tile.
    inner = config.Bbox(
        lat_min=tile.lat_min + 0.001,
        lat_max=tile.lat_max - 0.001,
        lon_min=tile.lon_min + 0.001,
        lon_max=tile.lon_max - 0.001,
    )
    assert config.get_tile_ids_for_bbox(inner) == ["r5c5"]


def test_get_tile_ids_for_bbox_spans_adjacent_tiles():
    left = config.get_tile_bbox("r5c5")
    right = config.get_tile_bbox("r5c6")
    combined = config.Bbox(
        lat_min=left.lat_min,
        lat_max=left.lat_max,
        lon_min=left.lon_min,
        lon_max=right.lon_max,
    )
    assert config.get_tile_ids_for_bbox(combined) == ["r5c5", "r5c6"]


def test_brooklyn_bbox_contains_the_pilot_tile():
    # The pilot tile (Carroll Gardens + Gowanus) is real, known-good
    # Brooklyn coverage -- if a future edit to BROOKLYN_BBOX shrinks it
    # enough to exclude the pilot tile, that's a real regression.
    brooklyn = config.BOROUGH_BBOXES["brooklyn"]
    assert brooklyn.lat_min <= config.PILOT_BBOX.lat_min
    assert brooklyn.lat_max >= config.PILOT_BBOX.lat_max
    assert brooklyn.lon_min <= config.PILOT_BBOX.lon_min
    assert brooklyn.lon_max >= config.PILOT_BBOX.lon_max
