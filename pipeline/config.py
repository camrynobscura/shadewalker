"""All tuning constants and geographic definitions for the pipeline.

Everything a human might want to tweak lives here, in one file. Constants are
ALL_CAPS by Python convention (meaning: set by a person, never computed), and
names carry their units (`_M` = meters, `_IN` = inches, `_DEG` = degrees).
"""

import math
import os
import re
from pathlib import Path
from typing import NamedTuple

# ── Directories ───────────────────────────────────────────────────────────────

# Path(__file__) is this file; .parents[1] walks up two levels to the repo root.
# (JS analogy: path.resolve(__dirname, ".."))
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"          # pathlib overloads "/" to join paths
RAW_DIR = DATA_DIR / "raw"             # cached API downloads (never re-fetched)

# Overridable via SHADEWALKER_TILES_DIR -- e2e tests (web/playwright.config.ts)
# boot a real server against a real filesystem path, with no equivalent of
# pytest's fixture-level isolation (tests/conftest.py) available. Without
# this, an e2e run on a machine that's also done real borough work (this
# directory holding real Brooklyn/Manhattan tiles alongside the pilot one)
# silently tests against city-scale data instead of the small,
# deterministic tile its specs are written against -- a real case: a point
# picked to be outside the pilot tile's coverage became a real, valid
# Manhattan location once Manhattan's tiles existed, and the "rejected as
# out of coverage" test started failing for a reason with nothing to do
# with the code under test.
TILES_DIR = Path(os.environ.get("SHADEWALKER_TILES_DIR", DATA_DIR / "tiles"))


# ── Geography ─────────────────────────────────────────────────────────────────

class Bbox(NamedTuple):
    """A lat/lon bounding box. NamedTuple = a tuple with named, typed fields
    (JS analogy: a frozen object literal; TS analogy: a readonly interface)."""
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

# Tile #1: sized to cover both Carroll Gardens and Gowanus, so the first
# working version of the app answers real walks in the user's neighborhood.
PILOT_BBOX = Bbox(lat_min=40.664, lat_max=40.690, lon_min=-74.008, lon_max=-73.978)

# All of NYC — the outer bound for the Stage 2 tile grid.
#
# lat_min shifted south by exactly one TILE_SIZE_LAT_DEG (40.49 -> 40.472) to
# cover Staten Island's real southern extent: its borough polygon reaches
# ~1,406m past the old lat_min, confirmed directly against
# boundary.borough_polygon(...).bounds, not a rough estimate. Because the
# shift is an exact multiple of TILE_SIZE_LAT_DEG, every existing tile's real
# lat/lon bounds are unchanged -- only its row number in the "r{row}c{col}"
# id shifts by +1. See PLAN.md/HISTORY.md for the migration this required
# (tree-cache rename, stale tile-export cleanup).
CITY_BBOX = Bbox(lat_min=40.472, lat_max=40.92, lon_min=-74.26, lon_max=-73.68)

# Tile size for the citywide grid (Stage 2). Both ≈ 2 km: one degree of
# latitude is ~111 km everywhere, but a degree of longitude shrinks with
# latitude (~84 km at NYC), hence the two different numbers.
TILE_SIZE_LAT_DEG = 0.018
TILE_SIZE_LON_DEG = 0.024

# When fetching data for a tile, reach slightly beyond its edges so streets
# and trees that straddle a border are complete in both neighboring tiles.
# Duplicated edges are deduplicated at server load time.
FETCH_BUFFER_M = 150

# For converting FETCH_BUFFER_M (meters) into degrees to pad a Bbox -- same
# ~111km/degree-of-latitude approximation TILE_SIZE_LAT_DEG's comment above
# already uses, good enough at this precision (a few meters of slop on a
# 150m buffer) without pulling in a real geodesy library for it.
METERS_PER_LAT_DEGREE = 111_320


def buffered_bbox(bbox: Bbox, buffer_m: float) -> Bbox:
    """Expand a bbox by buffer_m meters in every direction.

    Without this, two tiles fetched with their exact, non-overlapping
    bboxes never both capture a real intersection sitting near their shared
    border -- so it never gets the same OSM node id in both tiles' data,
    so GraphStore's load()-time merge (which matches on node id) has
    nothing to actually stitch together. Confirmed empirically on the
    first real multi-tile run: 92 Brooklyn tiles merged into 89 disconnected
    graph components instead of one connected network. This buffer is what
    creates the overlap the merge step depends on.

    Longitude degrees shrink with latitude (a degree of longitude is a
    shorter real distance the further from the equator you are), so the
    buffer's own mid-latitude is used for that conversion -- the same
    reasoning TILE_SIZE_LON_DEG's comment gives for NYC generally, just
    computed per-bbox instead of with one fixed citywide number.
    """
    mid_lat = (bbox.lat_min + bbox.lat_max) / 2
    lat_buffer_deg = buffer_m / METERS_PER_LAT_DEGREE
    lon_buffer_deg = buffer_m / (METERS_PER_LAT_DEGREE * math.cos(math.radians(mid_lat)))
    return Bbox(
        lat_min=bbox.lat_min - lat_buffer_deg,
        lat_max=bbox.lat_max + lat_buffer_deg,
        lon_min=bbox.lon_min - lon_buffer_deg,
        lon_max=bbox.lon_max + lon_buffer_deg,
    )

# "r{row}c{col}" grid ids, e.g. "r12c07" -- row/column offsets from
# CITY_BBOX's own lat_min/lon_min corner, in units of TILE_SIZE_LAT_DEG /
# TILE_SIZE_LON_DEG.
_GRID_ID_PATTERN = re.compile(r"r(\d+)c(\d+)")


def get_tile_bbox(tile_id: str) -> Bbox:
    """Resolve a tile id to its bounding box.

    Accepts 'pilot' (the Stage 1 pilot tile) or a citywide-grid id like
    'r12c07'. Grid ids are computed, not looked up -- any row/col is valid
    as long as the resulting box actually falls within CITY_BBOX.
    """
    if tile_id == "pilot":
        return PILOT_BBOX

    match = _GRID_ID_PATTERN.fullmatch(tile_id)
    if not match:
        raise ValueError(
            f"Unknown tile id {tile_id!r} -- expected 'pilot', a borough name "
            f"like 'brooklyn', or a grid id like 'r12c07'"
        )

    row, col = int(match.group(1)), int(match.group(2))
    lat_min = CITY_BBOX.lat_min + row * TILE_SIZE_LAT_DEG
    lon_min = CITY_BBOX.lon_min + col * TILE_SIZE_LON_DEG
    bbox = Bbox(
        lat_min=lat_min,
        lat_max=lat_min + TILE_SIZE_LAT_DEG,
        lon_min=lon_min,
        lon_max=lon_min + TILE_SIZE_LON_DEG,
    )

    outside_lat = bbox.lat_min >= CITY_BBOX.lat_max or bbox.lat_max <= CITY_BBOX.lat_min
    outside_lon = bbox.lon_min >= CITY_BBOX.lon_max or bbox.lon_max <= CITY_BBOX.lon_min
    if outside_lat or outside_lon:
        raise ValueError(f"Grid tile {tile_id!r} falls entirely outside CITY_BBOX")

    return bbox


def is_grid_tile_id(tile_id: str) -> bool:
    """Whether tile_id is 'pilot' or a citywide grid id like 'r12c07' --
    as opposed to a borough name like 'manhattan'. run_tile.py's main()
    uses this to decide which of run()/run_borough() a CLI argument means --
    every borough name resolves via its real boundary polygon (see
    pipeline/graph/boundary.py), so there's no fixed set of borough names
    to check membership against instead."""
    return tile_id == "pilot" or bool(_GRID_ID_PATTERN.fullmatch(tile_id))


def get_tile_ids_for_bbox(bbox: Bbox) -> list[str]:
    """Every citywide-grid tile id whose box overlaps the given area.

    Used as the cheap bounding-box candidate pass inside
    pipeline/graph/boundary.py's tile_ids_for_polygon() -- every borough's
    tile list is now resolved through a real boundary polygon, not a
    hand-picked Bbox, so this function's own bbox-only result is no longer
    used directly to cover a borough on its own.
    """
    # Tiles are half-open [start, start + size) -- a bbox edge that lands
    # exactly on a tile boundary belongs to the tile below it, not the one
    # above, so the end index uses ceil(...) - 1 rather than floor(...).
    # round() to 6dp first: repeated float multiplication in get_tile_bbox()
    # can put a boundary a fraction of a nanodegree past the exact tile edge
    # (e.g. 7.000000000000266 instead of 7.0), which would otherwise ceil up
    # to the wrong tile -- 6dp is still far finer than these coordinates need.
    row_start = int((bbox.lat_min - CITY_BBOX.lat_min) // TILE_SIZE_LAT_DEG)
    row_end = math.ceil(round((bbox.lat_max - CITY_BBOX.lat_min) / TILE_SIZE_LAT_DEG, 6)) - 1
    col_start = int((bbox.lon_min - CITY_BBOX.lon_min) // TILE_SIZE_LON_DEG)
    col_end = math.ceil(round((bbox.lon_max - CITY_BBOX.lon_min) / TILE_SIZE_LON_DEG, 6)) - 1

    # Clamp to the grid's own valid range. A bbox that reaches past
    # CITY_BBOX on any side (real for Queens: its real polygon dips
    # ~20m south of CITY_BBOX.lat_min, at what's almost certainly open
    # water off the Rockaways' tip -- discovered fetching real borough
    # polygons rather than hand-picked bboxes) would otherwise produce a
    # negative or out-of-range row/col that get_tile_bbox() rejects with
    # a ValueError. Dropping the sliver outside CITY_BBOX is the same
    # accepted tradeoff PILOT_BBOX/CITY_BBOX's own comments already make
    # for hand-picked-rectangle overreach, just applied at the grid's
    # edge instead of a borough's. NOT a safe fix if the clamped area is
    # real land, not water -- see PLAN.md's Staten Island note.
    max_row = math.ceil(round((CITY_BBOX.lat_max - CITY_BBOX.lat_min) / TILE_SIZE_LAT_DEG, 6)) - 1
    max_col = math.ceil(round((CITY_BBOX.lon_max - CITY_BBOX.lon_min) / TILE_SIZE_LON_DEG, 6)) - 1
    row_start, row_end = max(0, row_start), min(max_row, row_end)
    col_start, col_end = max(0, col_start), min(max_col, col_end)

    return [
        f"r{row}c{col}"
        for row in range(row_start, row_end + 1)
        for col in range(col_start, col_end + 1)
    ]


# ── Tree scoring ──────────────────────────────────────────────────────────────

# How far from a street's centerline a tree still counts toward that street.
# 14, up from the original 12: Central Park South's real tree row sits
# 12.7-14.0m out (park-side, behind the fence line -- ~2m further than a
# curbside tree pit), and the hard cutoff at 12m was excluding roughly half
# of it, making the whole street read as barren. A 12-16m sweep showed 14m
# captures everything 15m or 16m does on the measured blocks, so this is
# the smallest bump that fixes the set-back-tree case (see PLAN.md's
# 2026-07-22/23 scoring investigation).
TREE_BUFFER_M = 14

# Padding for the tree fetch around the *built edge table's* real extent
# (run_tile.py derives the tree bbox from the edges, not the tile's nominal
# padded bbox) -- must exceed TREE_BUFFER_M so every edge's full corridor is
# covered by fetched tree data. The margin past TREE_BUFFER_M is slop for
# the bbox math's ~meter-level approximations, nothing more.
TREE_FETCH_MARGIN_M = 30

# A tree's size factor is min(dbh, cap)/cap — trunk diameter as a canopy proxy,
# capped so one giant (or mistyped) trunk can't dominate a block's score.
DBH_CAP_IN = 30

# tpcondition → score. Healthier canopy = denser shade. Dead is excluded
# entirely in scoring; Unknown (~0.5% of living trees) gets the midpoint.
CONDITION_SCORES = {
    "Excellent": 1.0,
    "Good": 0.85,
    "Fair": 0.6,
    "Poor": 0.3,
    "Critical": 0.1,
    "Unknown": 0.5,
}
CONDITION_DEFAULT = 0.5  # missing/blank condition — treat like Unknown

# Monthly canopy factor for deciduous trees (index 0 = January). Evergreens
# always count at 1.0. NYC street trees leaf out ~late April and drop
# ~November; bare branches still cast a little shade, hence the 0.2 floor.
CANOPY_BY_MONTH = [0.2, 0.2, 0.3, 0.6, 0.95, 1.0, 1.0, 1.0, 0.95, 0.8, 0.45, 0.25]

# Genera that keep leaves year-round. Checked against the genus (first word of
# `genusspecies`); everything not listed is treated as deciduous, which is the
# right default — NYC's street forest is overwhelmingly deciduous.
EVERGREEN_GENERA = {
    "Pinus",         # pines
    "Picea",         # spruces
    "Abies",         # firs
    "Tsuga",         # hemlocks
    "Juniperus",     # junipers / redcedars
    "Thuja",         # arborvitae
    "Ilex",          # hollies
    "Magnolia",      # mostly evergreen-leaning in cultivation
    "Cedrus",        # true cedars
    "Chamaecyparis", # false cypresses
    "Cryptomeria",
    "Taxus",         # yews
}


# ── Routing ───────────────────────────────────────────────────────────────────

# The frontend's Shade_priority presets top out at 40 (MAX). The server
# enforces the same ceiling on /route's tree_weight param — a value far
# outside this range can push an edge's cost toward/below zero on dense
# blocks, breaking Dijkstra's non-negative-edge-weight assumption.
MAX_TREE_WEIGHT = 40.0

# When computing tree density (score ÷ length), treat very short edges as at
# least this long. Tiny intersection stubs (2 m edges) inherit the cross
# street's trees in their buffer corridor and would otherwise post absurd
# densities (0.8+ vs a leafy block's 0.05).
DENSITY_LENGTH_FLOOR_M = 20.0

# Below this per-meter tree density, an edge counts as "not shaded" for
# the /route response's shade_fraction stat. Distinct from the cost
# formula's *degree* of density above -- this is a yes/no cutoff, more
# lenient than RouteStats.tsx's SPARSE_TREES_PER_M (a whole-route warning
# threshold, not a per-edge one). Named for *shade* generally, not trees
# specifically -- Stage 4's building-shadow scoring (optional stretch
# goal) would feed the same field later without a schema change.
#
# 0.025, not the pilot tile's ~0.011 median: an initial 0.01 pick turned
# out to sit almost exactly at the citywide-tile median density, so it
# barely filtered anything (97% shade on one real test route). Checked
# the real distribution and several candidate values before landing here
# -- push this much past ~0.035 and the classification gets sensitive
# enough to individual edges that it can invert which of two routes reads
# as "more shaded," which defeats the point of the stat.
SHADE_DENSITY_THRESHOLD = 0.025

# A pedestrian is briefly exposed at every real street intersection a
# route crosses, no matter how tree-lined the blocks on either side are --
# shade_fraction's per-edge average can't see this on its own (the bug
# this constant fixes: a route over 10 different tree-lined Cobble Hill
# blocks read as exactly 100% shaded, because each block cleared
# SHADE_DENSITY_THRESHOLD in isolation). Applied once per intersection
# where *both* neighboring edges are themselves shaded -- a crossing next
# to an already-unshaded block doesn't need a separate deduction, since
# that block's own classification already accounts for the exposure
# there; only double-subtracting where the model would otherwise report
# an unbroken (and unrealistic) stretch of continuous cover.
#
# Not calibrated against real data the way SHADE_DENSITY_THRESHOLD was --
# there's no crosswalk-width dataset in this pipeline. 6m is a plain
# physical estimate (roughly one corner's curb-to-building clearance).
SHADE_CROSSING_GAP_M = 6.0

# How far a requested point may sit from the nearest graph node and still be
# considered "in coverage". Intersections along a real block are already
# 80-100 m apart, so this has to be generous enough not to reject a
# legitimate mid-block address — it's a backstop for genuine gaps (a point
# in the middle of the Gowanus Canal, say), not a precision check.
MAX_SNAP_DISTANCE_M = 200.0


# ── Park canopy (raster supplement) ─────────────────────────────────────────────

# 2021 NYC Land Cover raster (Zenodo record 14053441, TNC + UVM Spatial
# Analysis Lab) -- supplements the Forestry tree dataset for parks, whose
# Conservancy-managed trees Forestry doesn't cover (see PLAN.md's
# Park-canopy section). 1.7GB, gitignored under data/* -- not fetched by
# any pipeline/fetch/ script; download manually from the Zenodo record.
CANOPY_RASTER_PATH = RAW_DIR / "canopy" / "landcover_nyc_2021_6in.tif"

# The raster's own CRS (NAD83 State Plane Long Island, US survey feet) --
# confirmed directly off the file, matches its .xml sidecar. Vectors get
# reprojected into this for sampling; the raster itself is never resampled.
CANOPY_RASTER_CRS = "EPSG:2263"

# Land-cover class code for "tree canopy (crowns > 8ft)" in the raster's
# 8-class legend (2=grass/shrub, 3=bare, 4=water, 5=building, 6=road,
# 7=other impervious, 8=railroad). Confirmed via the raster's own colormap.
CANOPY_RASTER_TREE_CLASS = 1

# NYC Parks Properties `typecategory` values excluded from the park canopy
# mask AND the park-reach routing rule (both share citywide_park_shape_m())
# -- roadside/traffic-island types, a locked scope decision: these aren't
# "a park" for either purpose. Cemetery was excluded here too until
# 2026-07-31: NONE-priority routes through two real cemeteries came back
# 3.7-6.7% longer than OSRM/Valhalla/BRouter, all three of which route
# straight through cemetery interior footways with no special-casing --
# confirmed by inspecting all three engines' turn-by-turn (mostly-to-
# entirely unnamed `highway=footway`, no access restriction). No routing
# engine treats a cemetery path differently from a park path, so excluding
# them here was a data gap, not a real product decision -- reclassified
# from "deferred" to "just include them." Federal/state green land
# (Green-Wood etc.) is a separate, still-open gap: those aren't even in
# this NYC-Parks-only dataset, so no filter change here helps them -- see
# PLAN.md's Park-canopy section.
#
# Blanket-excluded regardless of any property on the feature -- checked
# directly against the real dataset (2026-08-08): Parkway in particular
# still carries several properties whose OWN subcategory reads "Large
# Park" or "Neighborhood Park" (Belt Parkway/Shore Parkway, 760 acres;
# Richmond Parkway, 351 acres; Pelham/Mosholu/Eastern Parkway) -- real
# highway medians, not walkable park interior, despite the park-like
# label. The subcategory override below (PARK_LIKE_SUBCATEGORIES) does
# NOT apply to these -- it would wrongly rescue ~1,300 acres of median.
PARK_EXCLUDED_TYPECATEGORIES = frozenset({
    "Parkway", "Strip",  # highway medians/rights-of-way
    "Lot", "Operations", "Retired N/A",  # not park land, no bundling error found
})

# Typecategories where a real bundling error WAS found (2026-08-08,
# FIXES.md item 1d): actual park land filed under a label meant for
# something else (Theodore Roosevelt Park and Brooklyn Botanic Garden
# under "Buildings/Institutions"; Grand Army Plaza under "Triangle/
# Plaza"; Ocean Parkway Malls under "Mall"). For exactly these three,
# park_polygon() checks the property's own `subcategory` instead of
# excluding the whole typecategory outright -- see PARK_LIKE_SUBCATEGORIES.
PARK_TYPECATEGORIES_NEEDING_SUBCATEGORY_CHECK = frozenset({
    "Buildings/Institutions", "Triangle/Plaza", "Mall",
})

# `subcategory` values confirmed (2026-08-08) to mean real, walkable park
# land wherever they appear on the typecategories above -- deliberately
# narrow, only labels with an unambiguous real-park meaning (checked
# directly against the dataset, not guessed). Excludes e.g. "Sitting
# Area/Triangle/Mall" (the genuine traffic triangles Triangle/Plaza mostly
# is), "Building"/"Recreation Center"/"Concession" (genuine buildings),
# and ambiguous one-off labels ("Type 1", "Undeveloped", "REDEC") that
# have no confirmed real-park example behind them.
PARK_LIKE_SUBCATEGORIES = frozenset({
    "Large Park", "Neighborhood Park", "Flagship Park", "Garden", "Neighborhood Plgd",
})

# Canopy fraction -> tree-density calibration (Phase 2 street audit: 750
# ordinary streets stratified across all 5 boroughs, ~150/borough, parks
# excluded by the real polygon). Fit: density = 0.0798*fraction + 0.0050,
# R^2=0.40 -- moderate, not high, is expected: fraction (area-based) and
# density (Forestry's per-tree health/size scoring) are different
# measurements, and this audit's population is deliberately where
# Forestry already works, not the park edges this slope gets applied to.
#
# Only the SLOPE carries over to the park-edge reach credit
# (apply_park_canopy() below) -- the intercept is an ordinary street's
# typical non-street-tree-canopy baseline (private yards, etc.), which
# doesn't apply to "how much extra shade does the park itself contribute
# right behind this curb": a park reach fraction of 0 (a street bordering
# open lawn) must add zero credit, not a phantom nonzero floor.
CANOPY_FRACTION_TO_DENSITY_SLOPE = 0.0798

# Buffer for the park-edge reach rule (apply_park_canopy) -- deliberately
# its OWN constant, not TREE_BUFFER_M. TREE_BUFFER_M is tuned to where
# real Forestry trees stand (12.7-14m for CPS's own fence-line row); the
# Parks Properties polygon boundary itself sits much farther from a
# park-edge street's centerline than that -- measured directly against
# Central Park: Central Park West's real distance is 5.9-27.7m (median
# 16.1m, only 1% within 14m), Central Park South 11.0-24.8m, Central Park
# North 19.5-23.5m. A 14m buffer (correct for individual trees) misses
# the park polygon entirely for ~99% of Central Park West's edges. 30m
# captures 100% of all three measured -- and can't over-credit the far
# (building) side of a street regardless of how generous it is, since
# apply_park_canopy intersects the buffered corridor with the real park
# polygon before sampling, which excludes anything not actually park land.
PARK_REACH_BUFFER_M = 30


# ── Data sources ──────────────────────────────────────────────────────────────

SOCRATA_BASE_URL = "https://data.cityofnewyork.us/resource"
TREES_DATASET_ID = "hn5i-inap"      # Forestry Tree Points — the live NYC Tree Map data
PARKS_DATASET_ID = "enfh-gkve"      # Parks Properties — one polygon per NYC Parks property,
                                     # `typecategory` distinguishes real parkland from roadside
                                     # slivers/non-park land (see PARK_EXCLUDED_TYPECATEGORIES)
PARK_TRAILS_DATASET_ID = "vjbm-hsyr" # NYC Parks Trails — official park-interior trail
                                     # centerlines, some missing from OSM entirely
                                     # (FIXES.md item 1g); `class` distinguishes real,
                                     # obvious paths (Class IV/V) from an unreliable
                                     # lower tier (see PARK_TRAIL_CLASSES in streets.py)
BOUNDARIES_DATASET_ID = "wh2p-dxnf" # Borough Boundaries (water areas included) — see PLAN.md:
                                     # a bridge's midspan sits over water, which the water-
                                     # EXCLUDED sibling dataset (gthc-hcne) doesn't cover --
                                     # that silently severed every inter-borough bridge crossing.
                                     # This version's water jurisdiction still stops at the state
                                     # line (verified: NJ side of the GWB, mid-Hudson excluded).
SOCRATA_PAGE_SIZE = 50_000          # rows per request (underscores are just digit separators)

# Optional — unset means anonymous requests (fine at pilot-tile scale, risks
# throttling at borough+ scale). Set as a real env var, never committed;
# get one from data.cityofnewyork.us (see README).
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")
