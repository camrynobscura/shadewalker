"""Tests for pipeline/config.py's tile-grid resolution (get_tile_bbox(),
get_tile_ids_for_bbox())."""

import math

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


# ---------------------------------------------------------------------------
# The grid's absolute position, pinned to literal coordinates on purpose.
#
# Every other test in this file states the grid RELATIVE to CITY_BBOX and
# TILE_SIZE_*_DEG ("r0c0 starts where CITY_BBOX starts"). Those are true by
# construction and stay true no matter where the grid is moved to -- so when
# commit a461e36 shifted CITY_BBOX.lat_min from 40.49 to 40.472 (exactly
# TILE_SIZE_LAT_DEG, one whole row), every assertion above shifted with it
# and the entire suite stayed green. Meanwhile every tile id quietly came to
# mean the area one row south of what it had meant, and two tiles whose
# fetch caches predated the shift kept serving another neighbourhood's
# streets under their old filenames for twelve days: r17c14 published South
# Bronx as Astoria, r19c13 published Washington Heights.
#
# A relative test cannot catch that class of change. These absolute ones
# can: they fail the moment the grid's origin or cell size moves, which is
# the signal that every cached fetch and every exported tile keyed by tile
# id has just been invalidated.
#
# IF THIS TEST FAILS because you deliberately changed the grid:
#   1. Bump GRAPH_CACHE_VERSION in pipeline/fetch/streets.py -- its own
#      comment already says to do this when "the fetch bbox logic changes,"
#      and redefining the grid changes it for every tile at once.
#   2. Bump TREE_CACHE_VERSION in pipeline/fetch/trees.py too; tree data is
#      cached per tile id and is just as misattributed by a shift.
#   3. Re-run every borough -- existing exports in data/tiles/ are keyed by
#      tile id and are now wrong.
#   4. THEN update the literals below to the new expected values.
# Updating the literals first defeats the point of the test existing.
# ---------------------------------------------------------------------------

def test_grid_origin_is_where_it_is_expected_to_be():
    origin = config.get_tile_bbox("r0c0")
    assert (origin.lat_min, origin.lon_min) == (40.472, -74.26)


def test_grid_cell_size_is_unchanged():
    assert config.TILE_SIZE_LAT_DEG == 0.018
    assert config.TILE_SIZE_LON_DEG == 0.024


def test_a_known_mid_grid_tile_still_covers_its_known_real_place():
    # r16c11 covers the south end of Central Park -- the tile every
    # park-path routing regression on this branch was verified against, so
    # if the grid moves under it those verifications silently stop meaning
    # what they say.
    bbox = config.get_tile_bbox("r16c11")
    assert bbox.lat_min == pytest.approx(40.760)
    assert bbox.lat_max == pytest.approx(40.778)
    assert bbox.lon_min == pytest.approx(-73.996)
    assert bbox.lon_max == pytest.approx(-73.972)


def test_the_two_tiles_that_were_misattributed_map_where_they_should():
    # Regression coverage for the real incident: under the pre-a461e36 grid
    # these ids meant one row north of these coordinates, and stale caches
    # under those names went unnoticed because nothing pinned the mapping.
    r17c14 = config.get_tile_bbox("r17c14")
    assert r17c14.lat_min == pytest.approx(40.778)  # NOT 40.796 (the old grid)
    r19c13 = config.get_tile_bbox("r19c13")
    assert r19c13.lat_min == pytest.approx(40.814)  # NOT 40.832 (the old grid)


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


def test_get_tile_ids_for_bbox_clamps_a_sliver_south_of_city_bbox():
    # Real case: Queens' real polygon dips ~20m south of CITY_BBOX.lat_min
    # (almost certainly open water off the Rockaways' tip) -- a bbox
    # reaching that far past the grid's own edge used to produce a
    # negative row index ("r-1c9"), which get_tile_bbox() then rejected
    # with a ValueError instead of get_tile_ids_for_bbox() ever returning.
    sliver_past_edge = config.Bbox(
        lat_min=config.CITY_BBOX.lat_min - 0.0002,
        lat_max=config.CITY_BBOX.lat_min + config.TILE_SIZE_LAT_DEG,
        lon_min=config.CITY_BBOX.lon_min,
        lon_max=config.CITY_BBOX.lon_min + config.TILE_SIZE_LON_DEG,
    )
    assert config.get_tile_ids_for_bbox(sliver_past_edge) == ["r0c0"]


def test_get_tile_ids_for_bbox_clamps_a_sliver_past_every_other_edge():
    # Same clamp, exercised on the other three edges (north/east/west) --
    # not hit by any real borough today, but the fix isn't direction-
    # specific, so this pins that on purpose.
    far_past_every_edge = config.Bbox(
        lat_min=config.CITY_BBOX.lat_min,
        lat_max=config.CITY_BBOX.lat_max + 1.0,
        lon_min=config.CITY_BBOX.lon_min - 1.0,
        lon_max=config.CITY_BBOX.lon_max + 1.0,
    )
    tile_ids = config.get_tile_ids_for_bbox(far_past_every_edge)
    max_row = math.ceil(round(
        (config.CITY_BBOX.lat_max - config.CITY_BBOX.lat_min) / config.TILE_SIZE_LAT_DEG, 6
    )) - 1
    max_col = math.ceil(round(
        (config.CITY_BBOX.lon_max - config.CITY_BBOX.lon_min) / config.TILE_SIZE_LON_DEG, 6
    )) - 1
    assert f"r{max_row}c{max_col}" in tile_ids
    assert f"r{max_row + 1}c0" not in tile_ids
    assert f"r0c{max_col + 1}" not in tile_ids


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


