"""pipeline/scoring/canopy.py against a synthetic raster.

The raster is built in the real CANOPY_RASTER_CRS (EPSG:2263) around a
real Manhattan coordinate, mirroring test_pipeline_canopy.py's synthetic-
raster approach from the centerline era: no network, no 1.7GB file, and
the geometry still exercises the real 4326 -> 2263 reprojection.

Layout, in raster columns (2ft pixels, 400x400):
    left half     class 1 -- tree canopy
    right half    class 6 -- road (valid data, zero canopy)
    a nodata band (value 0) along the far-right edge
"""

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from pipeline import config
from pipeline.scoring import canopy

# A real spot in Central Park; the raster is built around wherever this
# lands in EPSG:2263, so the test never hardcodes State Plane coordinates.
BASE_LON, BASE_LAT = -73.9650, 40.7800

PIXEL_FT = 2.0
SIZE = 400          # pixels per side -> 800ft x 800ft


@pytest.fixture()
def synthetic_raster(tmp_path, monkeypatch):
    to_raster = Transformer.from_crs("EPSG:4326", "EPSG:2263",
                                     always_xy=True)
    base_x, base_y = to_raster.transform(BASE_LON, BASE_LAT)
    half_ft = SIZE * PIXEL_FT / 2

    data = np.full((SIZE, SIZE), 6, dtype=np.uint8)     # road everywhere
    data[:, : SIZE // 2] = 1                            # left half canopy
    data[:, -40:] = 0                                   # nodata band

    path = tmp_path / "canopy.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=SIZE, height=SIZE, count=1,
        dtype="uint8", crs="EPSG:2263", nodata=0,
        transform=from_origin(base_x - half_ft, base_y + half_ft,
                              PIXEL_FT, PIXEL_FT),
    ) as dst:
        dst.write(data, 1)

    monkeypatch.setattr(config, "CANOPY_RASTER_PATH", path)
    return to_raster


def edge_at(kind, offset_ft, to_raster, length_m=100.0):
    """A north-south edge `offset_ft` east of the raster centre.

    Built by nudging longitude, so coordinates stay honest 4326 the way
    real edges are; ~200ft of latitude keeps the strip well inside the
    raster vertically.
    """
    # One degree of longitude at this latitude, in survey feet.
    lon_ft = 111_320.0 / 0.3048006096012192 * np.cos(np.radians(BASE_LAT))
    lon = BASE_LON + offset_ft / lon_ft
    return {
        "kind": kind,
        "length_m": length_m,
        "coords": [[lon, BASE_LAT - 0.00027], [lon, BASE_LAT + 0.00027]],
        "tree_deciduous": 0.0,
        "tree_evergreen": 0.0,
    }


def test_full_canopy_footway_scores_full_coverage(synthetic_raster):
    edge = edge_at("footway", -200.0, synthetic_raster)   # deep in canopy
    tally = canopy.score_park_paths([edge])
    assert tally["scored"] == 1
    assert edge["tree_deciduous"] == pytest.approx(
        config.DENSITY_AT_FULL_COVERAGE * 100.0, rel=0.02)
    assert edge["tree_park_canopy"] == pytest.approx(
        edge["tree_deciduous"], abs=0.001)
    assert tally["full_cover"] == 1


def test_bare_footway_scores_zero_but_counts_as_scored(synthetic_raster):
    edge = edge_at("footway", 200.0, synthetic_raster)    # road half
    tally = canopy.score_park_paths([edge])
    # A real reading of "no canopy" is a score, not a failure -- the
    # difference this module exists to preserve.
    assert tally["scored"] == 1
    assert tally["no_reading"] == 0
    assert edge["tree_deciduous"] == 0.0
    assert edge["tree_park_canopy"] == 0.0


def test_half_covered_path_scores_half(synthetic_raster):
    # Straddling the canopy/road boundary at the raster centre.
    edge = edge_at("path", 0.0, synthetic_raster)
    canopy.score_park_paths([edge])
    fraction = edge["tree_deciduous"] / (
        config.DENSITY_AT_FULL_COVERAGE * 100.0)
    assert 0.35 < fraction < 0.65


def test_sidewalk_and_crossing_are_never_touched(synthetic_raster):
    sidewalk = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    sidewalk["tree_deciduous"] = 7.7      # pretend blocks.py scored it
    crossing = edge_at("footway/crossing", -200.0, synthetic_raster)
    tally = canopy.score_park_paths([sidewalk, crossing])
    assert tally["scored"] == 0
    assert sidewalk["tree_deciduous"] == 7.7
    assert "tree_park_canopy" not in sidewalk
    assert crossing["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in crossing


def test_nodata_band_yields_no_reading_not_zero(synthetic_raster):
    # The nodata band spans columns 360-400, i.e. 320-400ft east of the
    # raster centre. 350ft east puts the ~13ft-wide strip fully inside it,
    # so every pixel is nodata and the pixel floor refuses a reading.
    edge = edge_at("footway", 350.0, synthetic_raster)
    tally = canopy.score_park_paths([edge])
    assert tally["no_reading"] == 1
    assert tally["scored"] == 0
    assert edge["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in edge


def test_missing_raster_skips_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CANOPY_RASTER_PATH",
                        tmp_path / "not_there.tif")
    edge = {"kind": "footway", "length_m": 50.0,
            "coords": [[BASE_LON, BASE_LAT], [BASE_LON, BASE_LAT + 0.0005]],
            "tree_deciduous": 0.0, "tree_evergreen": 0.0}
    tally = canopy.score_park_paths([edge])
    assert tally == {"scored": 0, "no_reading": 0, "km": 0.0,
                     "full_cover": 0}
    assert edge["tree_deciduous"] == 0.0
