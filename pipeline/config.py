"""All tuning constants and geographic definitions for the pipeline.

Everything a human might want to tweak lives here, in one file. Constants are
ALL_CAPS by Python convention (set by a person, never computed), and names
carry their units (`_M` = meters, `_IN` = inches, `_DEG` = degrees).
"""

import os
from pathlib import Path
from typing import NamedTuple

# ── Directories ───────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"             # cached API downloads (never re-fetched)
ORACLE_DIR = DATA_DIR / "oracle"       # the pinned OSM extract, source of truth

# The one OSM extract everything reads: the pipeline and every tools/audit/
# script. Geofabrik's New York State daily, pinned 2026-08-20
# (data/oracle/new-york-latest.timestamp.txt records the exact pin). It
# covers far more than NYC, so every read of it is clipped to the real
# borough boundaries (pipeline/graph/pedestrian.py).
OSM_EXTRACT_PATH = ORACLE_DIR / "new-york-latest.osm.pbf"

# Where the one citywide export (and the server's coverage cache beside it)
# lives. Overridable so the e2e suite (web/playwright.config.ts) can boot a
# real server against the small pilot fixture instead of whatever citywide
# export this machine holds, and so a practice build can write somewhere
# scratch instead of over what a live server is serving.
EXPORT_DIR = Path(os.environ.get("SHADEWALKER_EXPORT_DIR", DATA_DIR / "export"))

# The built frontend (web/dist) the server hands out alongside the API: one
# self-sufficient process, with Caddy in front in production. When the
# directory doesn't exist (dev with vite, CI, a fresh checkout) the server
# serves API-only.
WEB_DIST_DIR = Path(os.environ.get("SHADEWALKER_WEB_DIST", REPO_ROOT / "web" / "dist"))


# ── Geography ─────────────────────────────────────────────────────────────────

# The projected CRS for anything that buffers or measures area or distance:
# UTM zone 18N, meter units, accurate for NYC. Geometry is stored and
# exported in EPSG:4326 (lon/lat degrees) because that is what the frontend
# draws with, but a degree is not a meter and buffering in degrees is this
# project's #1 bug class: reproject here first, measure, then come back.
# Route lengths don't need this; they come from pyproj.Geod on the WGS84
# ellipsoid. Distinct from CANOPY_RASTER_CRS below (EPSG:2263, State Plane,
# US survey feet), the land-cover raster's own CRS.
METRIC_CRS = "EPSG:32618"


class Bbox(NamedTuple):
    """A lat/lon bounding box."""
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

# The original pilot area (Carroll Gardens + Gowanus); still what
# tools/build_pilot_fixture.py cuts the Playwright fixture down to.
PILOT_BBOX = Bbox(lat_min=40.664, lat_max=40.690, lon_min=-74.008, lon_max=-73.978)

# All of NYC: the bbox the tree fetch and the seeded routing harness draw
# against. Geometry filtering uses the real borough polygons
# (pipeline/graph/boundary.py), not this rectangle; it is the cheap outer
# bound for things that only need one. lat_min reaches Staten Island's real
# southern extent, and the tree cache is keyed on the area it covers.
CITY_BBOX = Bbox(lat_min=40.472, lat_max=40.92, lon_min=-74.26, lon_max=-73.68)


# ── Tree scoring ──────────────────────────────────────────────────────────────

# A tree's size factor is min(dbh, cap)/cap: trunk diameter as a canopy
# proxy, capped so one giant (or mistyped) trunk can't dominate a block's
# score.
DBH_CAP_IN = 30

# How far a sidewalk sample (or a naming probe) may sit from NYC's nearest
# kerb line and still be attributed to that kerb's block face. Lines only;
# tree attachment has its own, wider cap (TREE_ATTACH_MAX_M). Measured
# 2026-08-24: sidewalks sit a median 2.20 m from their kerb (p90 3.35 m),
# and 5 m captures 98.4% of sidewalk length where 3 m captures 81%, so 5 m
# is the knee, not a round number.
BLOCK_FACE_MAX_M = 5.0

# How far a tree trunk may sit from its nearest kerb and still credit that
# kerb's block face. Measured 2026-08-27 over all 887,329 usable trees:
# sidewalks within 10 m of a tree in the 5–8 m band carry +5 to +36 points
# more leaf cover than their Forestry score predicts (controls ±0), so those
# trees genuinely overhang the pavement; the attach rate went 80.8% → 84.1%.
# Don't raise it further: past 8 m the extra signal no longer decays with
# distance, which marks unrecorded canopy nearby rather than the tree's own
# crown, and crediting a tree for canopy that isn't its own is exactly the
# kind of rule this project doesn't keep.
TREE_ATTACH_MAX_M = 8.0

# How far apart to sample along a sidewalk edge when deciding which block
# face each part of it is beside. One answer per whole edge fails for edges
# longer than a block (28.3% of the city's sidewalk length is in pieces over
# 200 m): beside any one block the line is ~2 m from the kerb, but averaged
# over its length it is far from that block and matches nothing. Sampling
# asks per step instead. 2 m lands within 0.5% of a 1 m reference citywide
# at half the cost; 15 m steps clean over short blocks and loses ~9,300 of
# them (measured 2026-08-24).
BLOCK_FACE_SAMPLE_STEP_M = 2.0

# Compass-side language ("the north side of Court Street") declines rather
# than stretches: no side word when the pavement's mean away-from-kerb
# bearing sits within this margin of a 90-degree bin boundary, a true
# diagonal where neither word is honest. Street tilt citywide is nearly
# uniform over 0–45 degrees (measured 2026-08-28, 20k edges), so there is no
# natural gap to put the boundary in; 5 keeps the plain word through every
# real grid, Manhattan's 29-degree tilt included, and silences ~7% of length.
SIDE_DECLINE_MARGIN_DEG = 5.0

# ...and no side word when the direction wanders along the edge (an L
# wrapping a corner faces two ways; naming either is wrong for half the
# walk). Resultant of the per-probe unit vectors after
# BlockFaceIndex.compass_side's corner-arc outlier rejection: 1.0 is
# perfectly consistent, a half-N-half-W L reads ~0.71. 0.8 separates real
# curves from straight pavement with room on both sides (measured
# 2026-08-28: 72% of edges sit above 0.9).
SIDE_MIN_RESULTANT = 0.8

# Pavement Edge `feat_code` for a road edge, the kerb along a street, which
# is the only class with sidewalks beside it. The layer also carries 2270
# ALLEY and 2230 AIRPORT RUNWAY; alleys have no sidewalks, and leaving them
# in mis-assigned 27 km of real sidewalk to alley faces (measured
# 2026-08-24).
ROAD_EDGE_FEAT_CODE = "2260"

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
# always count at 1.0. Each value is the factor on the 15th; the server
# blends between 15ths by day (graph_store._month_blend, #113), so nothing
# jumps on the 1st. Hand-set, checked against NASA MODIS land-surface
# phenology for NYC (MCD12Q2 v6.1, 2015–2024): green-up is 15% by ~Apr 16,
# 50% by ~May 8, 90% by ~Jun 5, so April and May sit lower than a textbook
# curve. Fall can't be checked that way (turned leaves still shade but no
# longer read as green). The 0.2 bare-branch floor is deliberate: a leafless
# crown cuts ~25–60% of the sun (Heisler 1986; Youngberg 1983), but as thin
# branch shadows no walker would call shade.
CANOPY_BY_MONTH = [0.2, 0.2, 0.2, 0.35, 0.75, 1.0, 1.0, 1.0, 0.95, 0.8, 0.45, 0.25]

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
# enforces the same ceiling on /route's tree_weight param: a value far
# outside this range can push an edge's cost toward or below zero on dense
# blocks, breaking Dijkstra's non-negative-edge-weight assumption.
MAX_TREE_WEIGHT = 40.0

# How many tree_weights one /route request may ask for. Each weight is a
# synchronous Dijkstra run that blocks a worker thread, so an uncapped list
# is a denial-of-service hole. 8 is double the frontend's four presets:
# room to experiment from a script without being a meaningful load. Per
# weight, in-process on a dev Mac (measured 2026-09-09): ~14 ms for a 1 km
# route, ~95 ms for 20 km, and the production box runs ~4x slower per core,
# so 8 cross-borough weights still hold the single worker for seconds.
# Don't raise it without re-measuring the tail.
MAX_TREE_WEIGHTS_PER_REQUEST = 8

# glibc malloc arenas for the server process (server/malloc_arenas.py, #111).
# By default glibc gives each allocating thread its own arena and an arena
# keeps freed memory, so /route's per-request arrays, run on FastAPI's
# threadpool, leave leftovers in several arenas at once. Measured in a
# container calibrated to the box: +121 MB over 10,000 requests with the
# default 8 arenas, +7 MB with one, same throughput. One is safe here: route
# work holds the GIL, so threads rarely wait on the allocator, and the box
# has one core.
SERVER_MALLOC_ARENAS = 1

# --- Geocoding proxy (server/geocode.py) --------------------------------
# All geocoding goes through our own server, never straight from the
# visitor's browser to a third party. Upstream is Photon: the public komoot
# instance by default, a self-hosted index by flipping this env var; the
# frontend never knows which.
PHOTON_URL = os.environ.get("SHADEWALKER_PHOTON_URL", "https://photon.komoot.io")
PHOTON_TIMEOUT_S = 3.0
# A geocoder query is a street address, not a document; anything longer is
# garbage or abuse and gets a 422.
MAX_GEOCODE_QUERY_CHARS = 200
MAX_GEOCODE_RESULTS = 10
# Per-process LRU on search + reverse. No TTL on purpose: staleness is
# bounded by the monthly refresh restart, and repeat prefixes in one bounded
# city are exactly what a cache eats. Being polite to the fair-use upstream
# is the point, not saving our own milliseconds.
GEOCODE_CACHE_MAX_ENTRIES = 10_000

# Per-client rate limits (slowapi), keyed by the real client IP: X-Real-IP
# behind Caddy, the socket peer in dev. /route runs four Dijkstras per call
# but a human comparing presets makes one call, so 30/min is generous for
# real use and caps a flood of the single worker. /geocode relays to
# Photon's fair-use upstream, so forward and reverse share one budget;
# 60/min covers active autocomplete typing with headroom.
ROUTE_RATE_LIMIT = "30/minute"
GEOCODE_RATE_LIMIT = "60/minute"

# Edge kinds that are connectors: pavement you pass over, not a street
# walked along with an identity. Matched as substrings of an edge's `kind`
# ("footway/crossing", "footway/traffic_island"). Two consumers must agree:
# the server folds these legs into the street runs around them and never
# renders their name (graph_store._is_connector_kind), and naming skips
# deriving names for them because that work could never be seen (~105k
# edges of the citywide build).
CONNECTOR_KIND_MARKERS = ("crossing", "traffic_island")

# The density at which pavement is fully covered by canopy: the single
# saturation point for both routing cost and the displayed shade_fraction,
# so the router optimizes exactly what the user is shown and shade_fraction
# is measured coverage (a 65%-covered block displays 65%). Building shade
# is folded in before this saturation (graph_store._edge_density), so the
# same field carries both layers.
#
# It is the leaf-cover exchange rate, density = rate x covered fraction,
# fitted by tools/audit/fit_exchange_rate.py on 25,000 sidewalks against the
# 2021 land-cover raster: three fitting methods agree within 0.0325–0.0336
# (2026-08-27, on the 8 m attach cap; the rate and TREE_ATTACH_MAX_M are one
# calibration and move together). Above the rate, score is trunk inventory,
# not shade: at ≥90% ground-truth cover, scores span 0.00–0.058, and a
# blind Street View test could not tell a 5x score gap apart at matched
# coverage. Only ~3–4% of pavement sits above it, so the cost saturation is
# surgical. 100% shade means literally unbroken canopy and is meant to be
# effectively unreachable: a route always includes crossings and gaps
# between trees.
DENSITY_AT_FULL_COVERAGE = 0.033

# How far a requested point may sit from the nearest graph node and still be
# considered in coverage. Intersections along a real block are 80–100 m
# apart, so this has to be generous enough not to reject a legitimate
# mid-block address; it is a backstop for genuine gaps (a point in the
# middle of the Gowanus Canal), not a precision check.
MAX_SNAP_DISTANCE_M = 200.0


# ── Park canopy (raster supplement) ─────────────────────────────────────────────

# 2021 NYC Land Cover raster (Zenodo record 14053441, TNC + UVM Spatial
# Analysis Lab), the supplement for what the Forestry tree dataset misses.
# 1.7 GB, gitignored under data/, not fetched by any pipeline/fetch/
# script: download it manually from the Zenodo record.
#
# What Forestry misses is forest interior and particular institutional
# properties, not "Conservancy parks": Forestry inventories individually
# managed trees. Measured 2026-08-25 as canopy m² per recorded tree (a
# mature crown is ~50–200 m²): Prospect, Fort Greene, Bryant and Riverside
# read a plausible ~90–110; Central Park reads 18,810, Brooklyn Botanic
# Garden 52,955, and The High Line has no trees on record; the big Bronx
# and Queens parks record their roads but not their forest. The test
# separates "recorded" from "essentially absent", not "complete" from
# "half recorded", so covered parks are not demonstrably missing trees
# rather than verified complete.
CANOPY_RASTER_PATH = RAW_DIR / "canopy" / "landcover_nyc_2021_6in.tif"

# The raster's own CRS (NAD83 State Plane Long Island, US survey feet),
# confirmed off the file and its .xml sidecar. Vectors get reprojected into
# this for sampling; the raster itself is never resampled.
CANOPY_RASTER_CRS = "EPSG:2263"

# Land-cover class code for "tree canopy (crowns > 8ft)" in the raster's
# 8-class legend (2=grass/shrub, 3=bare, 4=water, 5=building, 6=road,
# 7=other impervious, 8=railroad). Confirmed via the raster's own colormap.
CANOPY_RASTER_TREE_CLASS = 1

# The walker strip sampled from the raster: a buffer of this radius (metres,
# converted to the raster's survey feet at use) around a path's line. 2.0 m
# is what a walker occupies, and the exchange rate it feeds is insensitive
# to it: re-fit at 1–3 m the rate moves under ±7% (2026-08-26).
CANOPY_SAMPLE_STRIP_M = 2.0

# Fewer valid (non-nodata) raster pixels than this under a strip and the
# edge gets no reading rather than a noisy one. At 6-inch pixels a 2 m strip
# along even a 5 m edge holds hundreds, so only slivers at the raster's
# border hit this: exactly 1 of 85,708 target edges citywide.
CANOPY_MIN_VALID_PIXELS = 30

# A sidewalk edge whose block face came back treeless counts as
# park-interior, and falls back to the raster (canopy.score_sidewalk_fallback),
# when at least this fraction of probes sampled along its whole line (one
# per ~10 m, min 3, never a midpoint) lands inside the city park union. A
# majority, not "any probe": an edge straddling a park fence is mostly
# street, and its street part has an honest Forestry answer. Measured
# 2026-08-27: 300 edges / 11.0 km qualify, 60% of that length in Central
# Park and none in the well-inventoried parks, so the empty face itself
# selects the Forestry-blind parks.
SIDEWALK_FALLBACK_PARK_FRACTION = 0.5

# NYC Parks Properties `typecategory` values excluded from the park polygon
# (boundary.park_polygon) that the park canopy mask reads: roadside and
# traffic-island types that aren't "a park". Cemeteries stay in: OSRM,
# Valhalla and BRouter all route straight through cemetery footways with no
# special-casing, so excluding them was a data gap, not a product decision.
# Federal and state green land (Green-Wood etc.) isn't in this
# NYC-Parks-only dataset at all, so no filter here helps it.
#
# Blanket-excluded regardless of any property on the feature: Parkway in
# particular carries properties whose own subcategory reads "Large Park"
# (Belt Parkway, 760 acres; Richmond Parkway, 351 acres), real highway
# medians despite the label, so PARK_LIKE_SUBCATEGORIES below does not
# apply to these.
PARK_EXCLUDED_TYPECATEGORIES = frozenset({
    "Parkway", "Strip",  # highway medians/rights-of-way
    "Lot", "Operations", "Retired N/A",  # not park land, no bundling error found
})

# Typecategories where real park land is filed under a label meant for
# something else (Theodore Roosevelt Park and Brooklyn Botanic Garden under
# "Buildings/Institutions"; Grand Army Plaza under "Triangle/Plaza"; Ocean
# Parkway Malls under "Mall"). For exactly these three, park_polygon()
# checks the property's own `subcategory` instead of excluding the whole
# typecategory; see PARK_LIKE_SUBCATEGORIES.
PARK_TYPECATEGORIES_NEEDING_SUBCATEGORY_CHECK = frozenset({
    "Buildings/Institutions", "Triangle/Plaza", "Mall",
})

# `subcategory` values that mean real, walkable park land wherever they
# appear on the typecategories above. Deliberately narrow: only labels with
# an unambiguous real-park meaning, checked against the dataset. Excludes
# e.g. "Sitting Area/Triangle/Mall" (the genuine traffic triangles),
# "Building"/"Recreation Center"/"Concession" (genuine buildings), and
# one-off labels ("Type 1", "Undeveloped", "REDEC") with no confirmed
# real-park example behind them.
PARK_LIKE_SUBCATEGORIES = frozenset({
    "Large Park", "Neighborhood Park", "Flagship Park", "Garden", "Neighborhood Plgd",
})


# ── Data sources ──────────────────────────────────────────────────────────────

SOCRATA_BASE_URL = "https://data.cityofnewyork.us/resource"
TREES_DATASET_ID = "hn5i-inap"      # Forestry Tree Points — the live NYC Tree Map data
PARKS_DATASET_ID = "enfh-gkve"      # Parks Properties — one polygon per NYC Parks property;
                                     # `typecategory` separates real parkland from roadside
                                     # slivers (see PARK_EXCLUDED_TYPECATEGORIES)
PARK_TRAILS_DATASET_ID = "vjbm-hsyr" # NYC Parks Trails — official park-interior trails, some
                                     # missing from OSM entirely
BOUNDARIES_DATASET_ID = "wh2p-dxnf" # Borough Boundaries, water areas included: a bridge's
                                     # midspan sits over water, and the water-excluded sibling
                                     # dataset (gthc-hcne) silently severed every inter-borough
                                     # bridge. Water jurisdiction still stops at the state line
                                     # (the NJ side of the GWB is excluded).
BUILDINGS_DATASET_ID = "5zhs-2jue" # Building Footprints (OTI) -- the building-shade layer's
                                     # heights and outlines; see the "Building footprints" block
SOCRATA_PAGE_SIZE = 50_000          # rows per request

# Optional: unset means anonymous requests (fine at pilot scale, risks
# throttling citywide). Set as a real env var, never committed; get one
# from data.cityofnewyork.us (see README).
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")


# ── Sun (building shade) ──────────────────────────────────────────────────────

# Where the sun table is computed from: one point for the whole city, the
# centre of CITY_BBOX. Across all 146 daylight anchor slots the four bbox
# corners differ from it by at most 0.31° in elevation and 1.00° in azimuth
# (measured 2026-09-24; pipeline/sun.py's test pins it), which moves a
# shadow sideways by under one raster cell at a 100 m reach, so per-borough
# tables would buy nothing the raster could resolve.
SUN_OBSERVER_LAT = (CITY_BBOX.lat_min + CITY_BBOX.lat_max) / 2   # 40.696
SUN_OBSERVER_LON = (CITY_BBOX.lon_min + CITY_BBOX.lon_max) / 2   # -73.970

# A slot is (month, hour): the sun on the 15th of the month, on the hour,
# New York clock time. The 15th falls after both DST transitions, so every
# anchor is a plain clock hour. The year is fixed so a rebuild reproduces
# the same table: the same anchor drifts ~0.04°/year with the leap cycle,
# enough to make two builds' shade tables differ for no reason worth
# chasing.
SUN_ANCHOR_YEAR = 2026
SUN_ANCHOR_DAY = 15
SUN_TIMEZONE = "America/New_York"


# ── Building footprints (building shade) ──────────────────────────────────────

# Bumped whenever the fetched columns change; part of the cache file name
# (the TREE_CACHE_VERSION idiom) so an old download can never be read as
# the new shape. Deleting the file is the routine cache bust; the dataset
# itself updates daily.
BUILDINGS_CACHE_VERSION = 1

# `height_roof` is feet above the building's own ground. Rows above this
# are dropped, not clipped: a value this large is a data error, and a
# clipped one would still throw a 1,600 ft shadow from what is really a
# small building. 1,600 ft is above every real roof in the city (Central
# Park Tower, 1,550 ft, is the tallest), so only garbage crosses it: on the
# live dataset exactly one row does, whose height is its own BIN pasted
# into the height column (2026-09-24). The same one-bad-record logic as
# DBH_CAP_IN for trees.
BUILDING_HEIGHT_CAP_FT = 1600

# `last_status_type` values whose building is recorded as gone. "Marked for
# Demolition" and "Investigate Demolition" are kept because the building
# still stands when they are set. At the heights involved (13 demolished
# rows, 10–35 ft, measured 2026-09-24) the choice moves a handful of
# low-rise shadows either way.
BUILDING_EXCLUDED_STATUSES = frozenset({"Demolition"})

# `feature_code` 1003 = "Placeholder": a stand-in record, not a surveyed
# outline (30 rows, 7 of them with a positive height, 2026-09-24). Every
# other code is kept: garages (5110), gas-station canopies (1001),
# skybridges (2110) and cantilevers (1006) all cast shade.
BUILDING_EXCLUDED_FEATURE_CODES = frozenset({"1003"})


# ── Building shade (engine) ───────────────────────────────────────────────────
# pipeline/scoring/shadows.py; the design and its measurements are in
# #108–#110.

# Along each edge, one slice per this many metres, sampled at slice centres
# (the blockface.py rule: never a midpoint). 2 m costs 2.4x the build time.
SHADOW_SAMPLE_STEP_M = 5.0

# Across each slice, three points: the line itself and +/- half the tree
# layer's walker strip, so both layers are measured over the same 2 m band
# and the display curve's lane-choice credit applies once. A slice reads
# 0, 1/3, 2/3 or 1.
SHADOW_STRIP_OFFSETS_M = (-CANOPY_SAMPLE_STRIP_M / 2, 0.0, CANOPY_SAMPLE_STRIP_M / 2)

# The obstacle-height raster the march runs on (named for what it holds,
# not "building height": tree crowns could join it later). Cell size sets
# the positional error of a shadow's edge, about one cell plus one march
# hop, and noon shadows in low-rise Brooklyn are only ~4 m long, so this is
# not a detail. Chosen against the exact sweep engine over five areas
# (2026-09-24, tools/audit/measure_shadow_feasibility.py): 0.5 m cells with
# 0.25 m hops agree with it at 99.0% of points, and past that the curve is
# flat while the cost keeps climbing. The table is rebuilt monthly, so the
# hours recur.
SHADOW_CELL_M = 0.5
SHADOW_MARCH_STEP_M = 0.25

# Buildings up to this height are answered by the raster march; taller
# ones also get the exact swept-polygon path, because their shadows at low
# sun outrun any sensible march. 1,092 footprints exceed 300 ft
# (2026-09-24).
SHADOW_RASTER_HEIGHT_CAP_M = 300 * 0.3048

# No shadow is followed further than this from the point, by either path.
# Beyond it a point reads unshaded even if a distant tower would shade it
# at very low sun. The cap only binds with the sun under ~5° (13 of 146
# slots) and changes under 0.6% of points even then, when the city is
# already ~99% shaded. Uncapping costs memory, not time: every tile would
# load buildings 5 km around it (2–2.6 GB grids).
SHADOW_MAX_REACH_M = 1000.0

# The engine works one square tile of sample points at a time, rasterizing
# the buildings within the tile plus a SHADOW_MAX_REACH_M margin, so a
# shadow crossing a tile edge is still seen and tiling is invisible in the
# result. Memory per tile at 0.5 m cells: ((2000 + 2 x 1000) / 0.5)^2 x 4
# bytes = 256 MB; 4 km tiles would be 576 MB, which swapped on the build
# machine.
SHADOW_TILE_M = 2000.0
