"""Score kerb-less pavement from the land-cover raster.

THE GAP THIS CLOSES
-------------------
Block-face scoring (pipeline/scoring/blocks.py) can only shade pavement
that sits beside a kerb -- a block face IS one side of one block. Park
paths, plazas, campus walks, steps and alleys have no kerb, so they scored
zero by construction: 94.3% of the 1,230km of pavement inside real parks,
Central Park's 95km of paths included. The raster covers exactly that
pavement.

ONE INSTRUMENT PER PAVEMENT KIND, ONE FALLBACK FOR NO ANSWER
------------------------------------------------------------
Selection is by KIND, never by geometry:

    footway/sidewalk       Forestry tree data, via block faces. Where
                           Forestry structurally has NO answer, the raster
                           fills in -- score_sidewalk_fallback below. A
                           HIERARCHY, not a blend: every edge is scored by
                           exactly one instrument, so neighbouring blocks
                           stay comparable.
    crossings + islands    zero by design; a crossing runs across a
                           roadway. Never touched by either function
                           here.
    everything else        score_park_paths. "What fraction of this
                           pavement is under canopy" is already a fraction
                           at any length, so there is no denominator to
                           build and none of the block-face machinery
                           applies.

WHERE FORESTRY HAS NO ANSWER (score_sidewalk_fallback)
------------------------------------------------------
Two sidewalk populations read 0% while the satellite saw real canopy
overhead (measured 2026-08-27):

  no_face        2,325 edges / 66.5 km: sidewalk-tagged pavement with no
                 kerb within BLOCK_FACE_MAX_M -- boardwalks, esplanades,
                 campus walkways. No face means no route by which ANY tree
                 data could reach the edge, so its zero is the absence of
                 the instrument, not a finding. Falls back everywhere,
                 park or not.
  treeless_face  a real face with zero attached trees. Two diseases, one
                 symptom: 37,575 of these are ordinary streets whose zero
                 the raster CONFIRMS (median leaf fraction 0.000) -- they
                 keep it. The 300 edges / 11.0 km majority-inside a city
                 park are Forestry blindness wearing a zero (41% mean
                 measured canopy; 60% of the length is Central Park).
                 Only those fall back. The empty face itself selects the
                 blind parks: well-inventoried parks' drives carry their
                 recorded trees, so Prospect / Riverside / Flushing
                 Meadows measure 0.00 km affected and stay untouched.

Park polygons therefore enter scoring for exactly ONE narrow question --
"is this treeless-face edge inside a city park" -- with bounded error both
ways: a street mislabeled park gets the raster, which reads bare as bare;
park pavement mislabeled street keeps today's zero. The original
no-mixing fear does not apply to fallback: for a no-answer edge the
alternative is not Forestry's number, it is a hard zero, the least
comparable score pavement can have. Deliberately not falling back:
ordinary treeless streets (their leafy tail is unrecorded private canopy),
and the state/federal parks the city list omits (39 km measuring ~10% mean
canopy -- near-bare, not worth dragging an OSM area pass into the build).

NOT A THIRD INSTRUMENT: BUILDING SHADE
--------------------------------------
pipeline/scoring/shadows.py scores the shadows buildings cast on every
edge, per month and hour. That is a second physical layer over the same
pavement, not another canopy instrument: it never enters the hierarchy
above, and the server combines it with the tree fraction by union at
request time (server/graph_store.py, _edge_density).

THE SCORE
---------
    credit = DENSITY_AT_FULL_COVERAGE x covered_fraction x length_m

DENSITY_AT_FULL_COVERAGE is the measured leaf-cover exchange rate (see its
comment in pipeline/config.py), so a fully covered path scores exactly the
density at which the server calls pavement fully shaded, and can never
out-bid a real street. All of it lands in `tree_deciduous`: the raster has
no species information, and treating park canopy as leaf-dropping is
honest for NYC, where parks genuinely go bare in winter.
`tree_park_canopy` records the same value so the frontend knows the
credit is area-based rather than counted trees.

A missing raster is a clean skip, not an error -- pilot/CI builds run
without the 1.7GB file on purpose, and park paths then keep zero shade.
"""

import logging

import rasterio
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import LineString
from shapely.ops import transform as shp_transform
from shapely.prepared import prep

from pipeline import config
from pipeline.fetch.parks import fetch_park_properties
from pipeline.graph.boundary import park_polygon
from pipeline.scoring.blocks import SHADED_KINDS

logger = logging.getLogger(__name__)

# Pavement that runs across a roadway scores zero -- a crossing with no
# trees and a bare sidewalk are the same thing, unshaded pavement -- and
# traffic islands ride with them.
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

    Runs after blocks.score_edges() and mutates `edges` in place the same
    way it does. Sidewalks and crossings are never touched by this
    function -- sidewalk edges Forestry could not answer get their raster
    shade from score_sidewalk_fallback below, under its own narrow rules
    -- so this cannot double-count against the tree data and cannot
    change a street.
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


def _fraction_of_line_in_parks(coords, length_m: float, park_prep) -> float:
    """Fraction of probes along the WHOLE line inside the park union.

    One probe per ~10m (min 3), the same sampling rule as
    naming._probe_points -- never a midpoint, which says nothing about
    where the rest of the line is. The probes interpolate the lon/lat line
    directly: spacing comes from the edge's own metric length, and
    point-in-polygon needs no metric geometry (nothing is buffered or
    measured in degrees).
    """
    line = LineString(coords)
    if line.length < 1e-12:
        return 0.0
    count = max(3, int(length_m // 10))
    inside = 0
    for i in range(count):
        probe = line.interpolate(line.length * i / (count - 1))
        if park_prep.contains(probe):
            inside += 1
    return inside / count


def score_sidewalk_fallback(edges: list[dict], park_shape=None) -> dict:
    """Raster shade for the sidewalk edges Forestry could not answer.

    Runs after score_park_paths and consumes the `face_outcome` marker
    blocks.score_edges records (only sidewalk-kind edges ever carry it,
    so crossings and park paths cannot reach this). Two cases fall back;
    everything else is untouched -- see WHERE FORESTRY HAS NO ANSWER in
    the module docstring for the populations:

      no_face        -> raster, unconditionally
      treeless_face  -> raster ONLY when the edge is majority-inside the
                        city park union
                        (config.SIDEWALK_FALLBACK_PARK_FRACTION)

    `park_shape` is injectable for tests; by default it is the same
    Parks Properties union the audit maps draw (boundary.park_polygon,
    cached fetch). It is built lazily and only when a treeless-face edge
    exists, so raster-less pilot/CI builds never touch parks data.

    Fallback edges set `tree_park_canopy`, which both tells the frontend
    the credit is area-based and lets tools/audit/fit_exchange_rate.py
    exclude them -- a future fit that kept them would regress the raster
    against itself.
    """
    tally = {"no_face_scored": 0, "park_treeless_scored": 0,
             "street_treeless_kept_zero": 0, "no_reading": 0, "km": 0.0}
    if not config.CANOPY_RASTER_PATH.exists():
        logger.warning(
            f"  [canopy] no raster at {config.CANOPY_RASTER_PATH} -- "
            "no-answer sidewalks keep zero shade (pilot/CI builds run "
            "without the 1.7GB raster on purpose)")
        return tally

    no_face, treeless = [], []
    for edge in edges:
        outcome = edge.get("face_outcome")
        if outcome == "no_face":
            no_face.append(edge)
        elif outcome == "treeless_face":
            treeless.append(edge)

    in_park = []
    if treeless:
        if park_shape is None:
            park_shape = park_polygon(fetch_park_properties())
        park_prep = prep(park_shape)
        for edge in treeless:
            fraction = _fraction_of_line_in_parks(
                edge["coords"], edge["length_m"], park_prep)
            if fraction >= config.SIDEWALK_FALLBACK_PARK_FRACTION:
                in_park.append(edge)
            else:
                tally["street_treeless_kept_zero"] += 1

    to_raster = Transformer.from_crs(
        "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform
    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        for edge, bucket in ([(e, "no_face_scored") for e in no_face]
                             + [(e, "park_treeless_scored") for e in in_park]):
            fraction = leaf_fraction(src, edge["coords"], to_raster)
            if fraction is None:
                tally["no_reading"] += 1
                continue
            credit = (config.DENSITY_AT_FULL_COVERAGE * fraction
                      * edge["length_m"])
            # These edges are zero by construction (that is what the
            # marker means), so this sets rather than tops up.
            edge["tree_deciduous"] = edge.get("tree_deciduous", 0.0) + credit
            edge["tree_park_canopy"] = round(credit, 3)
            tally[bucket] += 1
            tally["km"] += edge["length_m"] / 1000.0

    logger.info(
        f"  [canopy] sidewalk fallback: {tally['no_face_scored']:,} "
        f"no-face + {tally['park_treeless_scored']:,} in-park treeless "
        f"edges scored from the raster ({tally['km']:,.1f} km); "
        f"{tally['street_treeless_kept_zero']:,} street treeless edges "
        f"keep their honest zero; {tally['no_reading']:,} no reading")
    return tally
