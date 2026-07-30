"""Tests for pipeline/scoring/canopy.py's raster sampler.

Uses a tiny synthetic GeoTIFF (20x20 px, 10ft pixels, EPSG:2263) instead of
the real 1.7GB raster -- deterministic, fast, and exercises the same
reprojection/nodata logic the real file does. Geometry boxes/lines are
built in the raster's own CRS then round-tripped into METRIC_CRS (the unit
canopy_fraction()/sample_corridors() actually take), the same
pyproj-round-trip trick test_pipeline_scoring.py uses for TREE_BUFFER_M.

Layout (row 0 = north/top): columns 0-9 = class 1 (canopy), columns
10-19 = class 6 (road); the bottom row (row 19) is nodata (0), regardless
of column, so nodata-exclusion tests don't have to touch any other pixel.
"""

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import LineString, box
from shapely.ops import transform as shapely_transform

from pipeline import config
from pipeline.graph.centerline import METRIC_CRS
from pipeline.scoring import canopy

WEST, NORTH = 980_000.0, 200_200.0
PIXEL_SIZE = 10.0
WIDTH = HEIGHT = 20

_to_metric = Transformer.from_crs(config.CANOPY_RASTER_CRS, METRIC_CRS, always_xy=True).transform


def _box_m(minx, miny, maxx, maxy):
    """A box given in the raster's own (2263) coordinates, returned in
    METRIC_CRS -- the CRS canopy_fraction()/sample_corridors() expect."""
    return shapely_transform(_to_metric, box(minx, miny, maxx, maxy))


def _line_m(x0, y0, x1, y1):
    return shapely_transform(_to_metric, LineString([(x0, y0), (x1, y1)]))


@pytest.fixture
def synthetic_raster(tmp_path):
    array = np.ones((HEIGHT, WIDTH), dtype="uint8")
    array[:, 10:] = 6  # right half: road
    array[19, :] = 0   # bottom row: nodata, both halves

    path = tmp_path / "synthetic_landcover.tif"
    transform = from_origin(WEST, NORTH, PIXEL_SIZE, PIXEL_SIZE)
    with rasterio.open(
        path, "w", driver="GTiff", height=HEIGHT, width=WIDTH, count=1,
        dtype="uint8", crs=config.CANOPY_RASTER_CRS, transform=transform, nodata=0,
    ) as dst:
        dst.write(array, 1)
    return path


def test_canopy_fraction_all_canopy_region(synthetic_raster):
    # cols 1-8, rows 1-8: entirely inside the canopy half, well clear of
    # the nodata bottom row.
    geom = _box_m(980_010, 200_110, 980_090, 200_190)
    with rasterio.open(synthetic_raster) as raster:
        fraction, valid = canopy.canopy_fraction(raster, geom)
    assert valid == 64  # 8x8 pixels
    assert fraction == 1.0


def test_canopy_fraction_mixed_region(synthetic_raster):
    # cols 5-14 (5 canopy + 5 road), rows 1-8.
    geom = _box_m(980_050, 200_110, 980_150, 200_190)
    with rasterio.open(synthetic_raster) as raster:
        fraction, valid = canopy.canopy_fraction(raster, geom)
    assert valid == 80  # 10x8 pixels
    assert fraction == pytest.approx(0.5)


def test_canopy_fraction_excludes_nodata_from_denominator(synthetic_raster):
    # cols 1-8, rows 18-19: row 18 is canopy, row 19 is nodata. A naive
    # "count all pixels" would read this as 8/16 = 50% canopy; nodata
    # exclusion should read it as 8/8 = 100% over the 8 pixels that
    # actually carry data.
    geom = _box_m(980_010, 200_000, 980_090, 200_020)
    with rasterio.open(synthetic_raster) as raster:
        fraction, valid = canopy.canopy_fraction(raster, geom)
    assert valid == 8
    assert fraction == 1.0


def test_canopy_fraction_outside_raster_extent_reports_zero_valid_pixels(synthetic_raster):
    # Far outside the synthetic raster's tiny 200x200ft extent -- must be
    # told apart from a real, sampled all-non-canopy result.
    geom = _box_m(500_000, 100_000, 500_100, 100_100)
    with rasterio.open(synthetic_raster) as raster:
        fraction, valid = canopy.canopy_fraction(raster, geom)
    assert valid == 0
    assert fraction == 0.0


def test_sample_corridors_buffers_before_sampling(synthetic_raster):
    line = _line_m(980_020, 200_150, 980_080, 200_150)
    [(_, valid_small)] = canopy.sample_corridors([line], buffer_m=2, raster_path=synthetic_raster)
    [(_, valid_large)] = canopy.sample_corridors([line], buffer_m=20, raster_path=synthetic_raster)
    assert valid_large > valid_small


def test_raster_available(tmp_path, synthetic_raster):
    assert canopy.raster_available(synthetic_raster) is True
    assert canopy.raster_available(tmp_path / "nope.tif") is False


def _edge_row(line, tree_deciduous=1.0):
    return gpd.GeoDataFrame({
        "geometry_m": [line], "length_m": [line.length], "tree_deciduous": [tree_deciduous],
    })


def test_apply_park_canopy_adds_credit_for_canopy_inside_the_park(synthetic_raster):
    park_shape = _box_m(980_000, 200_000, 980_200, 200_200)  # whole raster, both classes
    line = _line_m(980_020, 200_150, 980_080, 200_150)  # entirely in the canopy half
    edges = _edge_row(line)

    result = canopy.apply_park_canopy(edges, park_shape, raster_path=synthetic_raster, buffer_m=2.0)

    reach = line.buffer(2.0).intersection(park_shape)
    with rasterio.open(synthetic_raster) as raster:
        fraction, _ = canopy.canopy_fraction(raster, reach)
    assert fraction > 0  # sanity: the reach really does overlap canopy pixels
    scored_length = max(line.length, config.DENSITY_LENGTH_FLOOR_M)
    expected = 1.0 + config.CANOPY_FRACTION_TO_DENSITY_SLOPE * fraction * scored_length
    assert result["tree_deciduous"].iloc[0] == pytest.approx(expected)


def test_apply_park_canopy_adds_nothing_where_the_reach_has_no_canopy(synthetic_raster):
    park_shape = _box_m(980_000, 200_000, 980_200, 200_200)
    line = _line_m(980_120, 200_150, 980_180, 200_150)  # entirely in the road half
    edges = _edge_row(line)

    result = canopy.apply_park_canopy(edges, park_shape, raster_path=synthetic_raster, buffer_m=2.0)

    assert result["tree_deciduous"].iloc[0] == 1.0


def test_apply_park_canopy_adds_nothing_outside_the_park_polygon(synthetic_raster):
    park_shape = _box_m(980_000, 200_000, 980_050, 200_050)  # tiny corner, far from the edge below
    line = _line_m(980_020, 200_150, 980_080, 200_150)  # canopy half, but nowhere near the park box
    edges = _edge_row(line)

    result = canopy.apply_park_canopy(edges, park_shape, raster_path=synthetic_raster, buffer_m=2.0)

    assert result["tree_deciduous"].iloc[0] == 1.0


def test_apply_park_canopy_returns_edges_unchanged_when_raster_missing(tmp_path):
    park_shape = _box_m(980_000, 200_000, 980_200, 200_200)
    line = _line_m(980_020, 200_150, 980_080, 200_150)
    edges = _edge_row(line)

    result = canopy.apply_park_canopy(edges, park_shape, raster_path=tmp_path / "missing.tif")

    assert result["tree_deciduous"].iloc[0] == 1.0


def test_citywide_park_shape_m_is_always_valid(monkeypatch):
    """Regression coverage for two confirmed failure modes (see
    boundary.park_polygon()'s docstring): a real Parks Properties polygon
    can have a self-intersecting ring, AND reprojecting an
    already-repaired, valid EPSG:4326 union into METRIC_CRS can
    reintroduce invalidity on its own -- confirmed live near East River
    Park, where it crashed apply_park_canopy()'s .intersection() call
    with GEOS's "TopologyException: side location conflict". A bowtie at
    real NYC-area coordinates stands in for that real polygon.
    """
    bowtie = {
        "type": "FeatureCollection",
        "features": [{
            "properties": {"typecategory": "Flagship Park"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [-73.99, 40.71], [-73.97, 40.73], [-73.97, 40.71], [-73.99, 40.73], [-73.99, 40.71],
            ]]},
        }],
    }
    monkeypatch.setattr(canopy.park_fetch, "fetch_park_properties", lambda refresh=False: bowtie)
    canopy.citywide_park_shape_m.cache_clear()
    try:
        assert canopy.citywide_park_shape_m().is_valid
    finally:
        canopy.citywide_park_shape_m.cache_clear()
