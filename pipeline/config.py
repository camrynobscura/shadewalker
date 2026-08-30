"""All tuning constants and geographic definitions for the pipeline.

Everything a human might want to tweak lives here, in one file. Constants are
ALL_CAPS by Python convention (meaning: set by a person, never computed), and
names carry their units (`_M` = meters, `_IN` = inches, `_DEG` = degrees).
"""

import os
from pathlib import Path
from typing import NamedTuple

# ── Directories ───────────────────────────────────────────────────────────────

# Path(__file__) is this file; .parents[1] walks up two levels to the repo root.
# (JS analogy: path.resolve(__dirname, ".."))
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"          # pathlib overloads "/" to join paths
RAW_DIR = DATA_DIR / "raw"             # cached API downloads (never re-fetched)
ORACLE_DIR = DATA_DIR / "oracle"       # the pinned OSM extract, source of truth

# The one OSM extract everything reads -- the pipeline and every
# tools/audit/ script. A single constant on purpose: this path used to be
# copy-pasted into eight audit tools, so nothing stopped a new one from
# pointing at a different (or stale) file and reporting confidently
# against it.
#
# Geofabrik's NEW YORK STATE daily, pinned 2026-08-20 (495,222,845 bytes;
# data/oracle/new-york-latest.timestamp.txt records the exact pin). It
# covers far more than NYC -- its pedestrian ways alone span
# -79.738,40.496 .. -71.856,45.035 -- so every read of it must be clipped
# to the real borough boundaries. See pipeline/graph/pedestrian.py.
OSM_EXTRACT_PATH = ORACLE_DIR / "new-york-latest.osm.pbf"

# Where the one citywide export (and the server's coverage cache beside
# it) lives. Named EXPORT_DIR since 2026-08-28 -- it was TILES_DIR from
# the tiled centerline pipeline, which wrote one file per grid tile; the
# sidewalk pipeline writes exactly one citywide file, so "tiles" had
# become a lie.
#
# Overridable via SHADEWALKER_EXPORT_DIR -- e2e tests
# (web/playwright.config.ts) boot a real server against a real filesystem
# path, with no equivalent of pytest's fixture-level isolation
# (tests/conftest.py) available. Without this, an e2e run on a machine
# holding a real citywide export silently tests against city-scale data
# instead of the small, deterministic pilot fixture its specs are written
# against -- a real case: a point picked to be outside the pilot
# fixture's coverage became a real, valid Manhattan location once
# Manhattan data existed, and the "rejected as out of coverage" test
# started failing for a reason with nothing to do with the code under
# test. A practice build must point this somewhere scratch, or it writes
# over what a live server is serving.
EXPORT_DIR = Path(os.environ.get("SHADEWALKER_EXPORT_DIR", DATA_DIR / "export"))

# The built frontend (web/dist) the server hands out alongside the API --
# the "serving shape" decided 2026-08-30: one self-sufficient process, a
# Caddy layer added in front at hosting time (history/geocoding-photon.md
# era of the deploy plan; see PLAN.md). Overridable for deploys where the
# build lands elsewhere. When the directory doesn't exist (dev with vite,
# CI, fresh checkout) the server simply serves API-only.
WEB_DIST_DIR = Path(os.environ.get("SHADEWALKER_WEB_DIST", REPO_ROOT / "web" / "dist"))


# ── Geography ─────────────────────────────────────────────────────────────────

# The projected CRS for anything that BUFFERS or MEASURES AREA/DISTANCE.
# UTM zone 18N — meter units, accurate for NYC.
#
# Geometry is stored and exported in EPSG:4326 (lon/lat degrees) because that
# is what the frontend draws with, but a degree is not a meter and buffering
# in degrees is this project's #1 bug class. Reproject here first, measure,
# then come back. Route LENGTHS don't need this: they come from pyproj.Geod,
# which measures on the WGS84 ellipsoid directly.
#
# Lived in pipeline/graph/centerline.py until 2026-08-23 and moved here when
# that module was deleted -- it is a fact about New York's location, not
# about how streets are modelled, so it never belonged to the centerline
# code. pipeline/graph/pedestrian.py's own docstring was already telling
# readers to "see config.METRIC_CRS" before this constant existed here.
METRIC_CRS = "EPSG:32618"

# Distinct from CANOPY_RASTER_CRS below (EPSG:2263, State Plane, US survey
# feet), which is the land-cover raster's own CRS and is NOT interchangeable
# with this one.


class Bbox(NamedTuple):
    """A lat/lon bounding box. NamedTuple = a tuple with named, typed fields
    (JS analogy: a frozen object literal; TS analogy: a readonly interface)."""
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

# The original pilot area (Carroll Gardens + Gowanus), chosen so the first
# working version of the app answered real walks in the user's
# neighborhood. Still what tools/build_pilot_fixture.py cuts the Playwright
# fixture down to.
PILOT_BBOX = Bbox(lat_min=40.664, lat_max=40.690, lon_min=-74.008, lon_max=-73.978)

# All of NYC -- the bbox the tree fetch and the seeded routing harness draw
# against. Geometry filtering uses the real borough polygons
# (pipeline/graph/boundary.py), not this rectangle; it survives as the
# cheap outer bound for things that only need one.
#
# lat_min was shifted south (40.49 -> 40.472) on the tiled pipeline to
# cover Staten Island's real southern extent: its borough polygon reaches
# ~1,406m past the old lat_min, confirmed directly against
# boundary.borough_polygon(...).bounds, not a rough estimate. The tile
# grid that dictated the shift's exact size is gone (stripped 2026-08-28);
# the value stays because the tree cache is keyed on the area it covers.
CITY_BBOX = Bbox(lat_min=40.472, lat_max=40.92, lon_min=-74.26, lon_max=-73.68)

# The tile grid (TILE_SIZE_LAT_DEG/LON_DEG, get_tile_bbox, is_grid_tile_id,
# get_tile_ids_for_bbox, buffered_bbox, FETCH_BUFFER_M) was deleted on
# 2026-08-28 with PLAN's `citywide-guards` step. It existed to cut the city
# into ~2km fetch/export tiles; the sidewalk pipeline reads the pinned
# .pbf once and writes one citywide file, so by deletion time the grid's
# only callers were its own tests and a broken debug tool. History:
# history/citywide-guards.md.


# ── Tree scoring ──────────────────────────────────────────────────────────────

# TREE_BUFFER_M and TREE_FETCH_MARGIN_M were deleted on 2026-08-23 with the
# centerline scoring code. Both measured distance from a street CENTERLINE --
# the corridor half-width that decided which edges a tree credited, and the
# fetch padding defined only as "must exceed TREE_BUFFER_M". Under
# per-sidewalk edges a tree belongs to the nearest pavement, so the question
# a corridor width answered no longer exists. Their values and the
# measurements behind them: history/centerline-scoring-constants.md.

# A tree's size factor is min(dbh, cap)/cap — trunk diameter as a canopy proxy,
# capped so one giant (or mistyped) trunk can't dominate a block's score.
DBH_CAP_IN = 30

# How far a SIDEWALK SAMPLE (or a naming probe) may sit from NYC's nearest
# kerb line and still be attributed to that kerb's block face. LINES ONLY
# since 2026-08-27 -- tree attachment has its own, wider cap with its own
# evidence (TREE_ATTACH_MAX_M below). Measured 2026-08-23/24 rather than
# chosen:
#   sidewalks sit a median 2.20m from their kerb (p90 3.35m), and 5m captures
#   98.35% of sidewalk length -- 97.89% with a resolvable block face. 2m
#   captures only 40.59% and 3m only 81.24%, so 5m is the knee, not a
#   round number.
# This replaces the centerline era's TREE_BUFFER_M, which measured from a
# street's MIDDLE and so varied with road width (5.93m on a side street to
# 13.38m on a boulevard). A kerb is scale-invariant: +0.0036 m/ft of street
# width, versus the centerline's +0.0618.
BLOCK_FACE_MAX_M = 5.0

# How far a TREE trunk may sit from its nearest kerb and still credit that
# kerb's block face. Raised 5.0 -> 8.0 on 2026-08-27, overturning the
# 2026-08-24 rejection ("mostly PARK trees dragged onto street faces",
# history/per-side-trees.md) with the direct evidence that rejection asked
# for. Full-population measurements (all 887,329 usable trees, not a
# sample; method + tables in history/tree-reach-8m.md):
#   - the 5-8m band holds 28,994 trees: 58% inside real parks, 42% outside.
#   - raster residual test: sidewalks within 10m of a 5-8m band tree
#     measure +5 to +36 points MORE leaf cover than their Forestry score
#     predicts (control edges: +/-0), dose-responsive, in BOTH slices.
#     Park-fence trees genuinely overhang perimeter sidewalks; front-yard
#     trees overhang street sidewalks. Attach rate 80.8% -> 84.1%.
# DO NOT RAISE TO 10 OR 12m. Measured the same day: where attribution is
# clean (sidewalks already carrying some score), the effect DECAYS with
# distance -- +13-17 points at 5-8m, +8 at 8-10m, +4 at 10-12m -- which is
# crown reach fading. The outer rings' residual signal sits only on
# bare-scored sidewalks and does NOT decay with distance: that is a MARKER
# effect (a recorded tree 10m out flags unrecorded private canopy nearby),
# and crediting a tree for canopy that is not its own is the
# works-but-unjustifiable rule family this project deletes on sight.
# Of trees attaching to nothing even at this cap, ~4/5 are inside parks
# (correctly excluded -- park pavement scores from the raster); the genuine
# failure, a street tree beside a kerb we cannot attach, is well under 1%
# of all trees (breakdown measured 2026-08-24, re-validated 2026-08-27).
TREE_ATTACH_MAX_M = 8.0

# How far apart to sample along a sidewalk edge when deciding which block
# face each part of it is beside. Settled by measurement, not chosen.
#
# WHY SAMPLING AT ALL: one answer per whole edge fails for edges longer than
# a block. OSM often draws a sidewalk as a single unbroken way, and beside
# any ONE block that line is ~2m from the kerb -- but averaged over its whole
# length most of it is far from that block, so a median-distance rule exceeds
# BLOCK_FACE_MAX_M and the edge matches NOTHING. 28.3% of the city's sidewalk
# length sits in pieces over 200m, so this was not a rare case. Sampling asks
# the question separately for each step instead.
#
# WHY 2m: distance from the 1m answer, length-weighted, citywide --
#     15m step   median 1.05%  p90 4.32%  p99 18.37%   >10% off: 2.22% of km
#      5m step   median 0.88%  p90 3.13%  p99  8.48%   >10% off: 0.78%
#      2m step   median 0.54%  p90 2.04%  p99  5.45%   >10% off: 0.36%
# 2m lands within 0.5% of the 1m reference and is uniform across all five
# boroughs (0.39-0.60% median). 1m costs roughly double for no useful gain.
#
# NOT 15m, which was inherited from the diagnostic tool: a 15m stride steps
# clean OVER short blocks, finding 121,542 block faces against 1m's 130,815.
# About 9,300 blocks would receive NO pavement at all and so manufacture the
# starved-denominator pathology this sampling exists to remove. 2m finds
# 130,159 of 130,815 (misses 0.5%).
#
# Cost is measured, not estimated: 14,482 point lookups/sec, so 2m over the
# citywide sidewalk network is ~7.1M lookups, about 9.5 minutes.
BLOCK_FACE_SAMPLE_STEP_M = 2.0

# Compass-side language ("the north side of Court Street") declines rather
# than stretches: no side word is emitted when the pavement's mean
# away-from-kerb bearing sits within this margin of a 90-degree bin
# boundary (a true diagonal -- neither "north" nor "east" is honest), per
# the user's 2026-08-28 call: four plain words with generous rounding,
# silence on the rest.
#
# MEASURED 2026-08-28 (20k-edge seeded sample, 965 km): street tilt
# citywide is essentially UNIFORM over 0-45 degrees (every 5-degree band
# holds 10-13% of length -- the boroughs' many rotated grids average flat),
# so there is NO natural gap to put this boundary in: each degree of
# margin silences ~2% of sidewalk length. 5 keeps the plain word through
# every real grid (Manhattan's 29-degree tilt included, words up to
# 40 degrees) and silences only the truly-diagonal ~7% of length.
SIDE_DECLINE_MARGIN_DEG = 5.0

# ...and no side word when the direction genuinely WANDERS along the edge
# (an L wrapping a corner faces two ways; naming either is wrong for half
# the walk). Resultant of the per-probe unit vectors after
# BlockFaceIndex.compass_side's corner-arc outlier rejection: 1.0 =
# perfectly consistent; the half-N-half-W L reads ~0.71. Measured
# 2026-08-28: 72% of edges sit above 0.9 outright and the mass below 0.4
# is the corner-arc ARTIFACT the rejection repairs, so after rejection
# 0.8 separates real curves from straight pavement with room on both
# sides.
SIDE_MIN_RESULTANT = 0.8

# Pavement Edge `feat_code` for a ROAD EDGE -- the kerb along a street, which
# is the only class that has sidewalks beside it. The layer also carries
# 2270 ALLEY (7,613 lines, 581 km) and 2230 AIRPORT RUNWAY (349 lines, 15 km),
# both of which were silently in the index until audited on 2026-08-24.
# Leaving them in put 1,649 alley faces (5.5%) into the "block faces with no
# sidewalk" pile -- which is why ALLEY topped that list, and it was never a
# finding: alleys do not have sidewalks. It also mis-assigned 27 km of real
# OSM sidewalk to alley faces. Excluding them lifts measured citywide
# coverage 65.1% -> 66.9% and Brooklyn 80.9% -> 83.8%.
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

# How many tree_weights one /route request may ask for (FIXES item 7,
# audit §2.1). Each weight costs a synchronous two-Dijkstra pass that
# blocks a worker thread, so an uncapped list is a denial-of-service hole
# once the API is public. 8 = double the frontend's four presets -- room
# to experiment from a script without ever being a meaningful load.
#
# Cost re-measured on the sidewalk graph 2026-08-23 (386,576 nodes,
# 488,538 loaded edges), median of 7 runs per pair, route() in-process
# with no HTTP or serialization:
#
#     ~1km route     57ms per weight
#     ~2km route     60ms
#     ~10km route   140ms
#     ~20km route   276ms
#
# The old comment here said ~13ms, measured on the deleted centerline
# model. It is 4.6x that now at the median, and the cost scales with how
# much graph Dijkstra explores -- so a long route is far worse than the
# median suggests. At the cap of 8, a cross-borough request blocks a
# worker for ~2.2 SECONDS. That strengthens the case for the cap rather
# than weakening it; don't raise it without re-measuring the tail.
MAX_TREE_WEIGHTS_PER_REQUEST = 8

# --- Geocoding proxy (server/geocode.py) --------------------------------
# All geocoding goes through our own server, never straight from the
# visitor's browser to a third party (decided 2026-08-30,
# history/geocoding-photon.md). Upstream is Photon: the public komoot
# instance by default, a self-hosted NYC index by flipping this env var —
# the frontend never knows which.
PHOTON_URL = os.environ.get("SHADEWALKER_PHOTON_URL", "https://photon.komoot.io")
PHOTON_TIMEOUT_S = 3.0
# Free-text length cap — a geocoder query is a street address, not a
# document; anything longer is garbage or abuse and gets a 422.
MAX_GEOCODE_QUERY_CHARS = 200
MAX_GEOCODE_RESULTS = 10
# Per-process LRU (functools.lru_cache) on search + reverse. No TTL on
# purpose: staleness is bounded by the monthly refresh restart
# (server/app.py docstring), and repeat prefixes in one bounded city are
# exactly what a cache eats — being polite to the fair-use upstream is
# the point, not saving our own milliseconds.
GEOCODE_CACHE_MAX_ENTRIES = 10_000

# Edge kinds that are CONNECTORS — pavement you pass over, not a street
# walked along with an identity. Matched as substrings of an edge's `kind`
# ("footway/crossing", "footway/traffic_island"). One vocabulary, two
# consumers that must agree: the server folds these legs into the street
# runs around them and never renders their name
# (server/graph_store.py:_is_connector_kind), and naming skips deriving
# names for them because that work could never be seen — ~105k edges of
# the citywide build (pipeline/graph/naming.py, skip added 2026-08-28).
CONNECTOR_KIND_MARKERS = ("crossing", "traffic_island")

# DENSITY_LENGTH_FLOOR_M IS DELETED, NOT UNSET. Do not reintroduce it.
#
# It was 20.0 in the centerline model, where it stopped 2m intersection stubs
# posting absurd densities after inheriting a cross street's trees through a
# buffer corridor. There is no corridor now, and block-face scoring removes
# the cause rather than the symptom: an edge takes a share of its block's
# trees proportional to its own length, so it cannot out-read its block.
#
# Measured on the citywide export, 2026-08-24, length-weighted median density
# by edge length:
#     0-5m 0.0093 | 5-20m 0.0060 | 20-50m 0.0059 | 50-100m 0.0084 | 100m+ 0.0110
# Short edges are LESS dense than long ones, not more. A floor exists solely
# to suppress inflated short-edge densities, and there are none to suppress.
#
# Restoring it would actively harm: under block scoring a short edge
# legitimately holds a small share, so dividing by a 20m floor deflates a
# correct value (a 2.6m edge holding 0.11 of its face reads 0.0055 instead of
# 0.042, 7.6x too low), and half of all sidewalk edges are under 5m.


# The per-meter tree density at which an edge counts as FULLY shaded for
# the /route response's shade_fraction stat: each edge contributes
# min(density / this, 1) of its length, so the stat is a continuous
# length-weighted average instead of a per-edge yes/no. Distinct from the
# cost formula's use of density above (which never saturates). Named for
# *shade* generally, not trees specifically -- Stage 4's building-shadow
# scoring (optional stretch goal) would feed the same field later without
# a schema change.
#
# Replaced SHADE_DENSITY_THRESHOLD (0.025, a binary shaded-or-not bar)
# on 2026-08-17 (FIXES item 2): the cliff-edge meant near-identical
# routes could read 0% vs 100% -- measured citywide 2026-07-31, 27% of
# edges sat within 50% of the bar. 0.05 is exactly 2x that retired bar,
# which itself was calibrated against real route distributions
# (2026-07-22..23) -- so "just barely counted as shaded" under the old
# definition now reads 50% credit. Chosen 2026-08-17 from three measured
# candidates over 24 real routes at all four presets (data/audits/
# 2026-08-17/shade_stat_candidates_results.json): saturating at the old
# bar itself inflates (midtown street routes read 42%) and still shows
# pre-clamp preset-monotonicity dips; saturating at the citywide p90
# (0.107) undersells the shadiest real walks (Central Park loop drops to
# 49-74%). At 0.05, park loops read 93-99%, ordinary midtown streets
# ~20%, and every sampled pair was monotonic across presets before the
# clamp even ran.
#
# The old definition also carried SHADE_CROSSING_GAP_M (a 6m deduction
# per shaded-shaded intersection, approximating crosswalk exposure).
# Deliberately dropped with the redesign, as decided in FIXES item 2:
# its boolean both-neighbors-shaded gate has no continuous equivalent,
# and it was a plain physical estimate, never calibrated.
# RE-DERIVED 2026-08-24 for the sidewalk model. Everything above this line
# describes the CENTERLINE model's 0.05 and is kept as provenance only -- a
# centerline edge carried two pavements' trees over one shared length, so its
# densities run ~4x ours and none of its calibration transfers.
#
# Measured citywide on routable pavement only (side L/R -- about a third of
# block faces carry no OSM sidewalk at all, and including them drags every
# figure toward zero, calibrating the app to places nobody can walk).
# Length-weighted, peak canopy:
#     p10 0.0001 | p25 0.0042 | p50 0.0100 | p75 0.0170 | p90 0.0246 | p99 0.0460
# The old 0.05 sits ABOVE our p99, which would make 0.7% of the city read as
# fully shaded -- it is miscalibrated for this model, not merely stale.
#
# Chosen against real places rather than percentiles. What a walk reports:
#                            0.02   0.03   0.05
#     Park Slope (leafy)      76%    57%    34%
#     Central Park perimeter  67%    50%    31%
#     Bushwick                49%    34%    20%
#     Midtown / Garment        9%     7%     4%
# 0.02 keeps the widest spread between leafy and bare (67 points vs 30 at
# 0.05) and puts the absolute numbers where they survive a plausibility
# check: a tree-lined Brooklyn street in July reading 76% matches walking it,
# where 34% would not, and a number people disbelieve is worse than none.
#
# NOTHING READS 100%. A route always includes crossings (zero by design),
# corner stubs and gaps between trees, so the length-weighted mean never
# approaches the cap even where individual blocks reach it. User's
# requirement, 2026-08-24: full shade should be effectively unreachable.
#
# SUPERSEDED 2026-08-26 by DENSITY_AT_FULL_COVERAGE below. Everything
# above is kept as provenance: it explains how 0.02 was chosen, and 0.02
# was chosen BLIND -- the leaf-cover exchange rate did not exist yet, so
# nobody could know that density 0.02 corresponds to ~65% real canopy
# coverage. The display was therefore calling 65%-covered blocks "100%
# shaded", and the believable route-level numbers (76% Park Slope) came
# from that inflation cancelling against dilution by zero-shade
# crossings. Two intermediate fixes were tried and killed by
# measurement before the replacement below; the account lives in
# server/graph_store.py's _edge_density() docstring.

# The density at which pavement is FULLY COVERED by canopy -- the single
# saturation point for BOTH routing cost and displayed shade_fraction, so
# the router optimizes exactly what the user is shown and shade_fraction
# IS measured coverage (a 65%-covered block displays 65%).
#
# 0.031 is the leaf-cover exchange rate measured 2026-08-25 on 25,000
# sidewalks (2m walker strip against the 2021 land-cover raster): density
# = 0.031 x covered fraction, three fitting methods agreeing 0.027-0.033,
# stable across strip widths 1-3m (+/-6%). Read forward it converts
# raster leaf cover to score (pipeline park-canopy work); read backward,
# density/0.031 is an edge's real covered fraction, capping at 1 because
# coverage cannot exceed "completely covered" -- above 0.031, score is
# trunk inventory, not shade (measured: at constant >=90% ground-truth
# cover, scores span 0.00-0.058; a blind Street View test could not
# distinguish 5-6.7x score gaps at matched coverage).
#
# Display consequence, previewed on 188 random routes + 8 named walks
# before adoption (user-approved 2026-08-26): every number drops ~12-15
# points; the leafiest end-to-end walks read ~70-74% instead of ~84-87%;
# leafy-vs-bare spread stays ~43 points. 100% now means literally
# unbroken canopy -- the user's "full shade effectively unreachable"
# requirement, strengthened.
#
# Routing consequence: only ~3-4% of pavement sits above the rate, so cost
# changes are surgical -- artifact faces (trunk pile-ups reading 0.10+)
# lose wormhole status, and raster-scored park paths (whose stored values
# are rate x coverage by construction) compete with streets on equal,
# physical terms. The earlier attempt to saturate cost at 0.02 instead
# was falsified at 120/188 routes losing real shade: the 0.02-0.031 band
# is the genuine 65%->100% coverage difference, not phantom credit.
#
# RE-FIT 2026-08-27, 0.031 -> 0.033, forced by TREE_ATTACH_MAX_M 5->8m:
# more attached trees means more density at the same physical coverage, so
# the rate HAS to move with the cap -- they are one calibration. Refit by
# tools/audit/fit_exchange_rate.py (the durable rebuild of the original
# scratchpad fit), which was first validated against the 5m export where
# the answer was known (0.0302-0.0327, reproducing 0.031) and then read
# 0.0325-0.0336 on the 8m export -- a TIGHTER three-method spread than the
# original's 0.027-0.033. 0.033 is the three-method center at the same
# rounding the original used.
DENSITY_AT_FULL_COVERAGE = 0.033

# How far a requested point may sit from the nearest graph node and still be
# considered "in coverage". Intersections along a real block are already
# 80-100 m apart, so this has to be generous enough not to reject a
# legitimate mid-block address — it's a backstop for genuine gaps (a point
# in the middle of the Gowanus Canal, say), not a precision check.
MAX_SNAP_DISTANCE_M = 200.0


# ── Park canopy (raster supplement) ─────────────────────────────────────────────

# 2021 NYC Land Cover raster (Zenodo record 14053441, TNC + UVM Spatial
# Analysis Lab) -- supplements the Forestry tree dataset for parks (see
# PLAN.md's Park-canopy section). 1.7GB, gitignored under data/* -- not
# fetched by any pipeline/fetch/ script; download manually from the Zenodo
# record.
#
# WHAT FORESTRY MISSES, corrected 2026-08-25. This comment used to say
# "Conservancy-managed trees Forestry doesn't cover". That is wrong and it
# predicts the wrong parks. Forestry inventories INDIVIDUALLY MANAGED
# trees, so the gap is (a) forest interior, which has no individual trees
# to record, and (b) particular institutional properties. Measured as
# canopy m2 per recorded tree, against the physical fact that a mature
# crown is ~50-200 m2:
#     Prospect 108, Fort Greene 109, Flushing Meadows 100, Bryant 93,
#     Riverside 93, Madison Square 69   <- all Conservancy/Alliance-run,
#                                          all recorded at a plausible density
#     Central Park 18,810, Brooklyn Botanic Garden 52,955,
#     The High Line and Wave Hill: zero trees on record
#     Pelham Bay 925, Forest Park 913, Van Cortlandt 868  <- park roads
#                                          recorded, forest interior not
# Across 459 parks >=1 ha, by area: a third plausible, a third thin, a
# third missing. Central Park is an outlier, not an instance of a rule.
#
# THE LIMIT OF THIS TEST: it separates "recorded" from "essentially absent"
# (108 vs 18,810 is 174x) but NOT "fully recorded" from "half recorded" -- a
# park missing half its trees still reads ~54 m2/tree, as plausible as 108.
# So the covered parks are "not demonstrably missing trees", not "verified
# complete". Upgrading that claim needs an independent count.
CANOPY_RASTER_PATH = RAW_DIR / "canopy" / "landcover_nyc_2021_6in.tif"

# The raster's own CRS (NAD83 State Plane Long Island, US survey feet) --
# confirmed directly off the file, matches its .xml sidecar. Vectors get
# reprojected into this for sampling; the raster itself is never resampled.
CANOPY_RASTER_CRS = "EPSG:2263"

# Land-cover class code for "tree canopy (crowns > 8ft)" in the raster's
# 8-class legend (2=grass/shrub, 3=bare, 4=water, 5=building, 6=road,
# 7=other impervious, 8=railroad). Confirmed via the raster's own colormap.
CANOPY_RASTER_TREE_CLASS = 1

# The walker strip sampled from the raster: a buffer of this radius (in
# metres, converted to the raster's survey feet at use) around a path's
# line. 2.0m is what a walker occupies -- the same width
# tools/audit/test_per_side_shade.py used to score real pavements -- and
# the exchange rate it feeds is insensitive to it: re-fit at 1m/1.5m/3m
# the rate moves under +/-7% (2026-08-26 sweep), so this is not a
# tunable that can drift scores the way TREE_BUFFER_M once did.
CANOPY_SAMPLE_STRIP_M = 2.0

# Fewer valid (non-nodata) raster pixels than this under a strip and the
# edge gets NO reading rather than a noisy one. At 6-inch pixels a 2m-wide
# strip along even a 5m edge holds hundreds, so this is hit only by
# slivers at the raster's border; measured citywide, exactly 1 edge of
# 85,708 target edges fails it.
CANOPY_MIN_VALID_PIXELS = 30

# A sidewalk edge whose block face came back TREELESS counts as
# park-interior -- and falls back to the raster (canopy.py:
# score_sidewalk_fallback) -- when at least this fraction of probes
# sampled along its WHOLE line (one per ~10m, min 3; never a midpoint,
# the trap this project has hit four times) lands inside the city park
# union. Why a majority and not "any probe": an edge straddling a park
# fence is mostly street, and its street part has an honest Forestry
# answer. Measured 2026-08-27 over all 38,721 treeless-face sidewalk
# edges: 300 edges / 11.0 km are majority-in-park (60% of that length in
# Central Park, 0.00 km in well-inventoried Prospect/Riverside/Flushing
# Meadows -- the empty face itself selects the Forestry-blind parks), 84
# edges straddle a boundary majority-outside and correctly stay street.
SIDEWALK_FALLBACK_PARK_FRACTION = 0.5

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

# CANOPY_FRACTION_TO_DENSITY_SLOPE and PARK_REACH_BUFFER_M were deleted on
# 2026-08-23 with the centerline scoring code. The slope was a linear fit of
# canopy fraction against CENTERLINE tree density over 750 streets; the
# buffer existed only because a centerline sits far from the park edge it
# borders (measured against Central Park: CPW median 16.1m, only 1% within
# 14m). A sidewalk on a park's perimeter IS the park edge, so the reach the
# buffer reached for is not there to cross. Both must be re-derived against
# real sidewalk geometry before park canopy is scored again -- that is
# PLAN.md's `park-canopy` step. Values and measurements:
# history/centerline-scoring-constants.md.


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
