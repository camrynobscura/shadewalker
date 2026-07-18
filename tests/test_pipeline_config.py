"""Tests for pipeline/config.py's tile-grid resolution (get_tile_bbox(),
get_tile_ids_for_bbox())."""

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


def test_buffered_bbox_expands_in_every_direction():
    bbox = config.Bbox(lat_min=40.0, lat_max=40.02, lon_min=-74.0, lon_max=-73.98)
    padded = config.buffered_bbox(bbox, 150)

    assert padded.lat_min < bbox.lat_min
    assert padded.lat_max > bbox.lat_max
    assert padded.lon_min < bbox.lon_min
    assert padded.lon_max > bbox.lon_max


def test_buffered_bbox_widens_longitude_more_than_latitude_at_nyc():
    # A degree of longitude is a shorter real distance than a degree of
    # latitude at NYC's latitude (~40.7N), so the same 150m buffer should
    # need MORE longitude degrees than latitude degrees to cover it.
    bbox = config.Bbox(lat_min=40.6, lat_max=40.7, lon_min=-74.0, lon_max=-73.9)
    padded = config.buffered_bbox(bbox, 150)

    lat_buffer_deg = bbox.lat_min - padded.lat_min
    lon_buffer_deg = bbox.lon_min - padded.lon_min
    assert lon_buffer_deg > lat_buffer_deg


def test_buffered_bbox_zero_buffer_is_a_no_op():
    bbox = config.Bbox(lat_min=40.0, lat_max=40.02, lon_min=-74.0, lon_max=-73.98)
    assert config.buffered_bbox(bbox, 0) == bbox


def test_is_grid_tile_id_true_for_pilot_and_grid_ids():
    assert config.is_grid_tile_id("pilot")
    assert config.is_grid_tile_id("r12c07")


def test_is_grid_tile_id_false_for_borough_names():
    # run_tile.py's main() relies on this distinction to route a CLI
    # argument to run() vs run_borough() -- a borough name must never be
    # mistaken for a tile id, or it'd fail tile-id parsing instead of
    # reaching run_borough().
    assert not config.is_grid_tile_id("brooklyn")
    assert not config.is_grid_tile_id("manhattan")


