"""Score kerb-less pavement from the land-cover raster.

THE GAP THIS CLOSES
-------------------
Block-face scoring (pipeline/scoring/blocks.py) can only shade pavement
that sits beside a kerb -- a block face IS one side of one block. Park
paths, plazas, campus walks, steps and alleys have no kerb, so they scored
zero by construction: 94.3% of the 1,230km of pavement inside real parks,
Central Park's 95km of paths included. The raster covers exactly that
pavement.

ONE INSTRUMENT PER PAVEMENT KIND
--------------------------------
Selection is by KIND, never by geometry and never by a park mask:

    footway/sidewalk       Forestry tree data, via block faces. Never
                           touched here -- even where no block face was
                           found. Mixing instruments on one pavement kind
                           makes neighbouring blocks incomparable.
    crossings + islands    zero by user decision (2026-08-24); a crossing
                           runs ACROSS a roadway.
    everything else        this module. "What fraction of this pavement is
                           under canopy" is already a fraction at any
                           length, so there is no denominator to build and
                           none of the block-face machinery applies.

No park polygons are involved: the raster answers per-strip, so whether a
plaza is "a park" never has to be decided, and the state/federal parks the
city's own park list omits are covered like everywhere else.

THE SCORE
---------
    credit = DENSITY_AT_FULL_COVERAGE x covered_fraction x length_m

DENSITY_AT_FULL_COVERAGE is the measured leaf-cover exchange rate (see its
comment in pipeline/config.py), so a fully covered path scores exactly the
density at which the server calls pavement fully shaded, and can never
out-bid a real street. All of it lands in `tree_deciduous`: the raster has
no species information, and treating park canopy as leaf-dropping (user
decision, 2026-08-26) is honest for NYC, where parks genuinely go bare in
winter. `tree_park_canopy` records the same value so the frontend knows
the credit is area-based rather than counted trees.

A missing raster is a clean skip, not an error -- pilot/CI builds run
without the 1.7GB file on purpose, and park paths then keep zero shade
exactly as they did before this module existed.
"""

import logging

import rasterio
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import LineString
from shapely.ops import transform as shp_transform

from pipeline import config
from pipeline.scoring.blocks import SHADED_KINDS

logger = logging.getLogger(__name__)

# Pavement that runs ACROSS a roadway scores zero by user decision
# (2026-08-24) -- a crossing with no trees and a bare sidewalk are the
# same thing, unshaded pavement -- and traffic islands ride with them.
CROSSING_KINDS = frozenset({
    "footway/crossing", "footway/traffic_island",
    "path/crossing", "cycleway/crossing",
})

# One US survey foot in metres. The raster's CRS (EPSG:2263) is in survey
# feet, and the strip must be buffered THERE -- never in degrees (this
# project's #1 bug class) and not in metres either.
_M_PER_SURVEY_FT = 0.3048006096012192


def leaf_fraction(src, coords, to_raster) -> float | None:
    """Fraction of the walker strip along `coords` that is tree canopy.

    None when the raster has no answer -- the strip falls off the raster,
    or holds fewer than config.CANOPY_MIN_VALID_PIXELS non-nodata pixels.
    "No reading" and "no canopy" are different facts and only the second
    is 0.0; blurring them would score border slivers as bare.
    """
    line = shp_transform(to_raster, LineString(coords))
    strip = line.buffer(config.CANOPY_SAMPLE_STRIP_M / _M_PER_SURVEY_FT)
    minx, miny, maxx, maxy = strip.bounds
    window = rasterio.windows.from_bounds(minx, miny, maxx, maxy,
                                          src.transform)
    window = window.round_offsets().round_lengths()
    if window.width < 2 or window.height < 2:
        return None
    data = src.read(1, window=window)
    if data.size == 0:
        return None
    covered = geometry_mask([strip], out_shape=data.shape,
                            transform=src.window_transform(window),
                            invert=True)
    values = data[covered]
    values = values[values != 0]   # nodata stays out of the denominator
    if values.size < config.CANOPY_MIN_VALID_PIXELS:
        return None
    return float((values == config.CANOPY_RASTER_TREE_CLASS).mean())


def score_park_paths(edges: list[dict]) -> dict:
    """Fill canopy shade on every kerb-less, non-crossing edge.

    Runs AFTER blocks.score_edges() and mutates `edges` in place the same
    way it does. Sidewalks and crossings are never touched, so this can
    only add shade to pavement that scored zero -- it cannot double-count
    against the tree data and cannot change a street.
    """
    tally = {"scored": 0, "no_reading": 0, "km": 0.0, "full_cover": 0}
    if not config.CANOPY_RASTER_PATH.exists():
        logger.warning(
            f"  [canopy] no raster at {config.CANOPY_RASTER_PATH} -- "
            "park paths keep zero shade (pilot/CI builds run without the "
            "1.7GB raster on purpose)")
        return tally

    to_raster = Transformer.from_crs(
        "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform
    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        for edge in edges:
            kind = edge.get("kind")
            if kind in SHADED_KINDS or kind in CROSSING_KINDS:
                continue
            fraction = leaf_fraction(src, edge["coords"], to_raster)
            if fraction is None:
                tally["no_reading"] += 1
                continue
            credit = (config.DENSITY_AT_FULL_COVERAGE * fraction
                      * edge["length_m"])
            edge["tree_deciduous"] = edge.get("tree_deciduous", 0.0) + credit
            # Rounded here, unrounded above: tree_park_canopy is a
            # reporting field the export rounds anyway, while the shade
            # score follows blocks.py's no-rounding rule.
            edge["tree_park_canopy"] = round(credit, 3)
            tally["scored"] += 1
            tally["km"] += edge["length_m"] / 1000.0
            if fraction >= 0.999:
                tally["full_cover"] += 1

    logger.info(
        f"  [canopy] {tally['scored']:,} kerb-less edges scored from the "
        f"raster ({tally['km']:,.0f} km); {tally['no_reading']:,} no "
        f"reading; {tally['full_cover']:,} fully covered")
    return tally
