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
TREE_BUFFER_M = 12

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


# ── Data sources ──────────────────────────────────────────────────────────────

SOCRATA_BASE_URL = "https://data.cityofnewyork.us/resource"
TREES_DATASET_ID = "hn5i-inap"      # Forestry Tree Points — the live NYC Tree Map data
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
