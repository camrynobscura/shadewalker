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
from shapely.geometry import box

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


def test_score_park_paths_never_touches_sidewalks_or_crossings(synthetic_raster):
    """Deliberately REWRITTEN 2026-08-27, not deleted: the old name
    promised sidewalks were never raster-scored AT ALL, and that premise
    changed -- score_sidewalk_fallback now covers the sidewalks Forestry
    could not answer, under its own tests below. What still holds, and is
    pinned here, is that score_park_paths itself never crosses the kind
    line, and that crossings get raster credit from NEITHER function."""
    sidewalk = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    sidewalk["tree_deciduous"] = 7.7      # pretend blocks.py scored it
    crossing = edge_at("footway/crossing", -200.0, synthetic_raster)
    tally = canopy.score_park_paths([sidewalk, crossing])
    assert tally["scored"] == 0
    assert sidewalk["tree_deciduous"] == 7.7
    assert "tree_park_canopy" not in sidewalk
    assert crossing["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in crossing


# --- the sidewalk fallback (2026-08-27) -------------------------------
#
# blocks.score_edges marks the sidewalk edges Forestry could not answer
# (`face_outcome`), and score_sidewalk_fallback gives exactly those the
# raster's answer: no-face edges unconditionally, treeless-face edges only
# when majority-inside the park polygon. Everything else it must not
# touch. The park shape is injected -- production builds it from Parks
# Properties; these tests only need "covers the edge" / "doesn't".

def park_over_everything():
    return box(BASE_LON - 0.01, BASE_LAT - 0.01,
               BASE_LON + 0.01, BASE_LAT + 0.01)


def test_no_face_sidewalk_falls_back_to_the_raster(synthetic_raster):
    edge = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    edge["face_outcome"] = "no_face"
    tally = canopy.score_sidewalk_fallback([edge])   # no park shape needed
    assert tally["no_face_scored"] == 1
    assert edge["tree_deciduous"] == pytest.approx(
        config.DENSITY_AT_FULL_COVERAGE * 100.0, rel=0.02)
    assert edge["tree_park_canopy"] == pytest.approx(
        edge["tree_deciduous"], abs=0.001)


def test_in_park_treeless_sidewalk_falls_back(synthetic_raster):
    edge = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    edge["face_outcome"] = "treeless_face"
    tally = canopy.score_sidewalk_fallback([edge],
                                           park_shape=park_over_everything())
    assert tally["park_treeless_scored"] == 1
    assert edge["tree_deciduous"] == pytest.approx(
        config.DENSITY_AT_FULL_COVERAGE * 100.0, rel=0.02)
    assert edge["tree_park_canopy"] > 0


def test_street_treeless_sidewalk_keeps_its_honest_zero(synthetic_raster):
    """The load-bearing negative: a bare street's zero is Forestry's
    ANSWER, and the leafy exceptions are private-garden canopy the user
    declined to credit (2026-08-26). Outside a park, treeless stays 0."""
    edge = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    edge["face_outcome"] = "treeless_face"
    far_away = box(BASE_LON + 0.5, BASE_LAT + 0.5,
                   BASE_LON + 0.6, BASE_LAT + 0.6)
    tally = canopy.score_sidewalk_fallback([edge], park_shape=far_away)
    assert tally["street_treeless_kept_zero"] == 1
    assert tally["park_treeless_scored"] == 0
    assert edge["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in edge


def test_a_fence_straddling_edge_is_majority_ruled_to_the_street(synthetic_raster):
    """~30% of the edge's probes inside the park is below the 0.5
    majority (config.SIDEWALK_FALLBACK_PARK_FRACTION): mostly-street
    pavement keeps Forestry's zero. Probes sample the WHOLE line, so
    this is decided by length share, not by any single point."""
    edge = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    edge["face_outcome"] = "treeless_face"
    # The edge runs BASE_LAT-0.00027 -> +0.00027; cover its southern ~30%.
    south_sliver = box(BASE_LON - 0.01, BASE_LAT - 0.01,
                       BASE_LON + 0.01, BASE_LAT - 0.00011)
    tally = canopy.score_sidewalk_fallback([edge], park_shape=south_sliver)
    assert tally["street_treeless_kept_zero"] == 1
    assert edge["tree_deciduous"] == 0.0


def test_unmarked_edges_are_untouched_by_the_fallback(synthetic_raster):
    """The marker's absence is the protection: a scored sidewalk and a
    crossing carry no `face_outcome`, so the fallback must not know they
    exist -- whatever their kind or position over the canopy."""
    scored = edge_at("footway/sidewalk", -200.0, synthetic_raster)
    scored["tree_deciduous"] = 7.7
    crossing = edge_at("footway/crossing", -200.0, synthetic_raster)
    tally = canopy.score_sidewalk_fallback(
        [scored, crossing], park_shape=park_over_everything())
    assert tally == {"no_face_scored": 0, "park_treeless_scored": 0,
                     "street_treeless_kept_zero": 0, "no_reading": 0,
                     "km": 0.0}
    assert scored["tree_deciduous"] == 7.7
    assert "tree_park_canopy" not in scored
    assert crossing["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in crossing


def test_fallback_nodata_yields_no_reading_not_zero_credit(synthetic_raster):
    edge = edge_at("footway/sidewalk", 350.0, synthetic_raster)  # nodata band
    edge["face_outcome"] = "no_face"
    tally = canopy.score_sidewalk_fallback([edge])
    assert tally["no_reading"] == 1
    assert tally["no_face_scored"] == 0
    assert edge["tree_deciduous"] == 0.0
    assert "tree_park_canopy" not in edge


def test_missing_raster_skips_the_fallback_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CANOPY_RASTER_PATH",
                        tmp_path / "not_there.tif")
    edge = {"kind": "footway/sidewalk", "length_m": 50.0,
            "face_outcome": "no_face",
            "coords": [[BASE_LON, BASE_LAT], [BASE_LON, BASE_LAT + 0.0005]],
            "tree_deciduous": 0.0, "tree_evergreen": 0.0}
    tally = canopy.score_sidewalk_fallback([edge])
    assert tally == {"no_face_scored": 0, "park_treeless_scored": 0,
                     "street_treeless_kept_zero": 0, "no_reading": 0,
                     "km": 0.0}
    assert edge["tree_deciduous"] == 0.0


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
