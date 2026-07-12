"""All tuning constants and geographic definitions for the pipeline.

Everything a human might want to tweak lives here, in one file. Constants are
ALL_CAPS by Python convention (meaning: set by a person, never computed), and
names carry their units (`_M` = meters, `_IN` = inches, `_DEG` = degrees).
"""

from pathlib import Path
from typing import NamedTuple

# ── Directories ───────────────────────────────────────────────────────────────

# Path(__file__) is this file; .parents[1] walks up two levels to the repo root.
# (JS analogy: path.resolve(__dirname, ".."))
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"          # pathlib overloads "/" to join paths
RAW_DIR = DATA_DIR / "raw"             # cached API downloads (never re-fetched)
TILES_DIR = DATA_DIR / "tiles"         # pipeline output: one graph chunk per tile


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
CITY_BBOX = Bbox(lat_min=40.49, lat_max=40.92, lon_min=-74.26, lon_max=-73.68)

# Tile size for the citywide grid (Stage 2). Both ≈ 2 km: one degree of
# latitude is ~111 km everywhere, but a degree of longitude shrinks with
# latitude (~84 km at NYC), hence the two different numbers.
TILE_SIZE_LAT_DEG = 0.018
TILE_SIZE_LON_DEG = 0.024

# When fetching data for a tile, reach slightly beyond its edges so streets
# and trees that straddle a border are complete in both neighboring tiles.
# Duplicated edges are deduplicated at server load time.
FETCH_BUFFER_M = 150


def get_tile_bbox(tile_id: str) -> Bbox:
    """Resolve a tile id to its bounding box.

    Stage 1 only knows the pilot tile; the citywide grid ("r12c07"-style ids
    derived from CITY_BBOX and the tile sizes) arrives in Stage 2 / M5.
    """
    if tile_id == "pilot":
        return PILOT_BBOX
    raise ValueError(f"Unknown tile id {tile_id!r} — only 'pilot' exists until Stage 2 (M5)")


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

# How many meters of leafy street a walker would trade for one bare meter.
# Density (tree value per meter) is typically 0–0.1, so this must be order-10
# to matter. M3 calibration (4 pilot walks × weights 5/10/20/40): in leafy
# areas routes barely change between 5–20 (shortest is often already green);
# 40 buys big tree gains at ~20%+ detours. 10 = takes every cheap win without
# forced detours; UI slider should span 0–40.
TREE_WEIGHT = 10.0

# When computing tree density (score ÷ length), treat very short edges as at
# least this long. Tiny intersection stubs (2 m edges) inherit the cross
# street's trees in their buffer corridor and would otherwise post absurd
# densities (0.8+ vs a leafy block's 0.05).
DENSITY_LENGTH_FLOOR_M = 20.0


# ── Data sources ──────────────────────────────────────────────────────────────

SOCRATA_BASE_URL = "https://data.cityofnewyork.us/resource"
TREES_DATASET_ID = "hn5i-inap"   # Forestry Tree Points — the live NYC Tree Map data
SOCRATA_PAGE_SIZE = 50_000       # rows per request (underscores are just digit separators)
