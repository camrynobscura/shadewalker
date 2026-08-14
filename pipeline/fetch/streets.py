"""Fetch the walkable street network for a tile from OpenStreetMap via osmnx.

osmnx downloads OSM data and hands it back already graph-shaped: a networkx
MultiDiGraph whose nodes are intersections (keyed by global OSM node ids) and
whose edges are street segments with geometry, length, and street names.

WHY THE CUSTOM FILTER (instead of network_type="walk"):
NYC's OSM community maps sidewalks as separate, unnamed footway lines running
parallel to each street — and tags the streets themselves "use the sidewalk",
which makes osmnx's built-in walk network *drop the streets*. On the pilot
tile that produced 3,512 unnamed sidewalk fragments and only 9 real street
edges — no names for route descriptions, and inconsistent coverage across the
city. So we ask Overpass (OSM's query API) directly for the centerline model:
walkable *streets* + standalone park/plaza paths, minus the sidewalk
fragments (WALK_FILTER, below) -- plus a narrow second query
(CYCLEWAY_FILTER) for the shared foot+bike paths some bridge landings use
instead of a footway. One edge per block, carrying its street name.

Caching is two-layer:
  1. osmnx's own HTTP cache (raw Overpass responses) in data/raw/osmnx_cache
  2. our per-tile GraphML file in data/raw/streets/ — GraphML is a standard
     XML graph format; loading it back skips all network + assembly work.
"""

import json
import time
from pathlib import Path

import networkx as nx
import osmnx as ox
import requests
from pyproj import Transformer
from shapely import prepared
from shapely.geometry import LineString, Point, Polygon, box, shape
from shapely.ops import substring, transform, unary_union
from shapely.strtree import STRtree

from pipeline import config
from pipeline.config import Bbox
from pipeline.fetch import interior_sidewalks, park_trails
from pipeline.graph import vertical_audit
from pipeline.graph.centerline import METRIC_CRS

STREETS_DIR = config.RAW_DIR / "streets"

# Park-reach shapes are measured in meters (see _park_reach_sidewalks) --
# the one place this fetch module needs to leave lon/lat degrees.
_TO_METRIC_CRS = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform
# The inverse -- needed only for interior-sidewalk snapping, which invents
# new points in METRIC_CRS (splitting a street edge at a real meters-based
# distance) that then need to go back into the graph's own lon/lat degrees.
_FROM_METRIC_CRS = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True).transform

# Bump this whenever WALK_FILTER changes, or the fetch bbox logic changes
# (v3: run_tile.py started passing a FETCH_BUFFER_M-padded bbox instead of
# the tile's exact one; v4: retain_all=True -- see the comment at the
# graph_from_bbox call; v5: un-excluded footway=crossing and added the
# CYCLEWAY_FILTER union; v6: added the FOOT_OVERRIDES_ACCESS_FILTER union;
# v7: fetch all three filters unsimplified and simplify once after
# composing them, instead of each filter simplifying independently before
# the union -- see fetch_streets()'s docstring; v8: added the
# NAMED_SIDEWALK_FILTER union -- named interior park paths tagged
# footway=sidewalk were being dropped alongside real unnamed street
# sidewalks; v9: added the ANY_SIDEWALK_FILTER union, geometry-filtered to
# park reach -- the same park paths that are UNNAMED in OSM, which v8's
# name test can't rescue -- and added bridleway to WALK_FILTER's highway
# allowlist, both shipping in one re-fetch; v10: cemetery interior paths
# are no longer excluded from park_reach (config.PARK_EXCLUDED_TYPECATEGORIES)
# -- the cache filename doesn't vary with park_reach's actual polygon
# content, only whether one was supplied at all, so this needs its own
# bump or every already-cached tile keeps silently reusing its
# cemetery-excluding graph) -- it's baked into the cache
# filename, so old cached graphs are ignored rather than silently reused;
# v11: park_polygon() no longer excludes a property by typecategory alone
# for Buildings/Institutions, Triangle/Plaza, and Mall -- Theodore
# Roosevelt Park, Brooklyn Botanic Garden, Grand Army Plaza, and Ocean
# Parkway Malls were being wrongly excluded from park_reach under those
# labels (FIXES.md item 1d) -- same "baked into park_reach, not the cache
# filename" reasoning as v10, so this needs its own bump too;
# v12: added the PARKING_AISLE_FILTER union -- real through-path parking
# aisles (a lot connecting to the street network at 2+ points, not a
# dead-end spur) are no longer excluded outright (FIXES.md item 1b).
# _through_path_parking_aisles() decides real vs. dead-end from graph
# connectivity, baked into the fetched graph the same way park_reach is,
# so this needs its own bump too;
# v13: PARKING_AISLE_FILTER excludes tunnel=building_passage -- a private
# indoor/underground garage lane running through a building, not a real
# open-lot shortcut (38 ways citywide, confirmed near a Times Square
# hotel). Same "baked into the fetched graph" reasoning as v12.
# v14: added NYC's Interior Sidewalk Centerline data (pipeline.fetch.
# interior_sidewalks), snapped onto the street network wherever a real
# off-ROW walking path (park interior, NYCHA campus, hospital/school
# campus, ordinary residential complex) comes within
# INTERIOR_SIDEWALK_SNAP_MAX_M of it (FIXES.md item 1a). Also added the
# BARRIER_FILTER union used to veto a connection that would cross a real
# fence/wall. Same "baked into the fetched graph" reasoning as v10-v13.
# v15: FOOT_OVERRIDES_ACCESS_FILTER excludes golf-tagged ways -- real
# private golf-course cart paths were being admitted as if foot=designated
# meant public access, confirmed already live in production for one real
# Marine Park case (FIXES.md item 1e's follow-up).
# v16: interior-sidewalk edges (_build_interior_sidewalk_graph,
# _apply_edge_splits) now carry osmid and length -- both were missing
# entirely, which crashed centerline.build_edge_table() the first time
# the full pipeline ran end-to-end on a real tile with a real interior-
# sidewalk connection (confirmed live, Marine Park/r7c14, while validating
# v15 above). Same "baked into the fetched graph" reasoning as v10-v15.
# v17: added 'track' to WALK_FILTER's highway allowlist and
# FOOT_OVERRIDES_ACCESS_FILTER's (FIXES.md item 1f) -- a full citywide
# tag survey found 497 real NYC highway=track ways, OSM's own pedestrian-
# navigation guidelines classify track as a default pedestrian way (same
# category as footway/path/steps), and manual review of every real risk
# cluster in the newly-admitted set (Pelham Bay, Rockaway/Tilden Beach, a
# beach-path access=customers cluster, Ocean Breeze Park) confirmed real
# public paths. Also tightened WALK_FILTER's foot clause (foot!~"no" ->
# foot!~"no|private") -- closes a real gap found during the same survey
# where foot=private wasn't excluded by anything.
# v18: added NYC Parks' own Trails data (pipeline.fetch.park_trails), a
# real coverage gap rather than an exclusion bug (FIXES.md item 1g) --
# trails don't exist in OSM at all yet, not "OSM has them and something
# filtered them out". Only Class IV ("Highly Developed")/Class V ("Fully
# Developed") are admitted: a manual spot-check across three real parks
# (Alley Pond, Prospect, Van Cortlandt) found every Class IV/V segment
# checked was a real, obvious path, while Class III and below was
# unreliable -- no other field predicts which of those are real trails
# and which are nothing on the ground. Each trail is trimmed down to only
# the portion not already within PARK_TRAIL_OVERLAP_BUFFER_M of the walk
# graph built so far (most of a real trail typically duplicates a path
# OSM already has), then snapped on the same way interior sidewalks are.
# v19 (2026-08-14, one bundled re-fetch -- see HISTORY.md's v19 entry):
# (a) CYCLEWAY_FILTER widened foot=designated -> designated|yes (real
# shared-use greenways, ~95km citywide, were excluded; survey in
# data/audits/2026-08-14/); (b) closure zones -- imported synthetic
# segments inside a curated construction-closure polygon are clipped out
# (East River Park's ESCR ghost paths, FIXES item 0b; see
# CLOSURE_ZONES_PATH's comment for why tags can't infer this); (c) the
# `layer` tag is now retained on fetched ways (vertical_audit needs it);
# (d) snap connectors carry a weld=True attribute so the new
# vertical-suspects detector (pipeline/graph/vertical_audit.py) can audit
# exactly the edges the pipeline manufactured -- it REPORTS suspects per
# tile, it never removes anything (measured 2026-08-14: auto-removal
# can't reach the needed precision; removal stays evidence-gated in the
# server's coordinate blocklist). All four are baked into the fetched
# graph, hence one shared bump.
GRAPH_CACHE_VERSION = 19

# Overpass's public instance drops connections intermittently under sustained
# borough-scale querying -- observed three real ConnectionRefusedErrors during
# the Brooklyn run, each recovering within seconds (a plain curl right after
# succeeded every time), so this is transient flakiness worth retrying rather
# than a hard block. Backoff: 5s, 10s, 20s.
MAX_FETCH_RETRIES = 3
FETCH_RETRY_BACKOFF_S = 5

# Overpass QL tag filters, applied to every way ("way" = an OSM line feature).
# Each ["key"~"regex"] clause requires a match; ["key"!~"regex"] excludes
# (and also passes ways that lack the key entirely).
WALK_FILTER = (
    # street/path types a pedestrian can use — note no motorways/trunks.
    # bridleway is here rather than in a narrow foot=designated-only query
    # of its own (the CYCLEWAY_FILTER pattern) on the strength of a
    # citywide tag survey: of 32.45km of NYC bridleway across 113 ways,
    # ZERO is tagged foot=no -- nobody has marked a single one
    # pedestrian-prohibited -- while 37% (12.03km) carries no foot tag at
    # all. CYCLEWAY_FILTER is deliberately narrow because most cycleways
    # really are bike-only; bridleways here are the opposite, so narrowing
    # to the explicitly-tagged 58% would silently drop ~12km of paths New
    # Yorkers walk and run on daily (Central Park's reservoir loop,
    # Prospect Park's bridle path -- the latter being 100% of that park's
    # fixable coverage gap). The 1.74km that IS restricted is tagged
    # access=private, which the access clause below already excludes --
    # and deliberately NOT also added to FOOT_OVERRIDES_ACCESS_FILTER,
    # since that query needs an explicit foot=designated|yes to override
    # access, and this restricted mileage carries no foot tag at all.
    #
    # track is here on the same reasoning (FIXES.md item 1f, 2026-08-09):
    # OSM's own pedestrian-navigation guidelines list highway=track as a
    # default "pedestrian (distinct) way," the same category as footway/
    # path/steps, not something needing a special foot=yes override. A
    # full citywide tag survey found 497 real NYC track ways; 323 (65%)
    # already pass this filter's existing foot/access clauses unchanged.
    # Manually checked every real risk cluster in the admitted set
    # against satellite imagery (an untagged paved path network in Pelham
    # Bay, Rockaway/Tilden Beach's service roads, a beach-path
    # access=customers cluster, Ocean Breeze Park) -- all confirmed real
    # public paths, not private facilities slipping through on a missing
    # access tag.
    '["highway"~"primary|primary_link|secondary|secondary_link|tertiary|tertiary_link'
    '|unclassified|residential|living_street|pedestrian|footway|path|steps|service'
    '|bridleway|track"]'
    '["area"!~"yes"]'                                  # skip plaza *areas* (not lines)
    '["foot"!~"no|private"]'                           # explicitly closed to pedestrians
                                                        # ("private" added 2026-08-09,
                                                        # FIXES.md item 1f: 4 real track
                                                        # ways found tagged foot=private,
                                                        # which "no" alone never caught --
                                                        # applies to every highway type
                                                        # here, not just track)
    '["access"!~"private|no"]'                         # gated/private ways
    '["service"!~"private|driveway|parking_aisle"]'    # not real walking streets
    '["footway"!~"sidewalk"]'                          # the separately-mapped sidewalk
                                                       # fragments (unnamed) — we model
                                                       # streets as centerlines instead.
                                                       # NOT "crossing" here (that used to
                                                       # be excluded alongside sidewalk) --
                                                       # a marked crossing bridges a real
                                                       # gap rather than duplicating a
                                                       # street's own centerline, and
                                                       # excluding it was severing bridge
                                                       # promenades from the street grid
                                                       # at their landings (discovered on
                                                       # Manhattan Bridge/Brooklyn Bridge
                                                       # during Stage 2's Manhattan run --
                                                       # see PLAN.md)
)

# A second, narrower query, unioned into the main fetch (see fetch_streets):
# some NYC bridges model their pedestrian path as a shared foot+bike
# cycleway rather than a footway (Brooklyn Bridge's Brooklyn-side landing,
# confirmed real) -- WALK_FILTER's highway allowlist doesn't include
# cycleway at all, since most cycleways are bike-only. Scoped to an
# explicit foot allowance rather than broadening WALK_FILTER's own list,
# so ordinary bike-only cycleways stay excluded.
#
# v19 widened designated -> designated|yes: foot=designated alone was
# excluding real shared-use greenways tagged foot=yes (found on the
# Cunningham Park Greenway via an external-engine comparison, FIXES item
# 1). A citywide survey (data/audits/2026-08-14/cycleway_foot_yes_ways.
# json) measured the widened set: 398 ways / 95km, overwhelmingly named
# greenways (Bronx River, Mosholu-Pelham, Jamaica Bay, Shore Parkway,
# Kissena, Flushing Bay Promenade...). Admission risk for a mapped real
# way is access-tagging, and an explicit foot=yes settles that the same
# way foot=designated does.
CYCLEWAY_FILTER = '["highway"="cycleway"]["foot"~"^(designated|yes)$"]'

# A third query, also unioned into the main fetch: WALK_FILTER's
# ["access"!~"private|no"] clause excludes any way tagged access=private
# or access=no, but OSM's own tag hierarchy lets a more specific mode tag
# override that default -- foot=designated or foot=yes on a way still
# tagged access=no means "closed to general/vehicle access, but
# pedestrians are specifically permitted," not "closed to everyone."
# Confirmed real on Queensboro Bridge: its own pedestrian walkway is
# split across several segments of the same physical path, some tagged
# access=no + foot=designated, others with no access tag at all --
# WALK_FILTER was dropping exactly the access=no segments, fragmenting
# the path and cutting off both landings (see PLAN.md). Overpass QL
# can't express "exclude access=private|no UNLESS foot overrides it" as
# a single AND-chain of tag filters (each bracket is ANDed, and a
# regex can only inspect one tag), so this mirrors CYCLEWAY_FILTER's
# approach: a separate query for exactly the case WALK_FILTER's own
# allowlist can't express, unioned into the result instead.
#
# Excludes golf-tagged ways (FIXES.md item 1e's follow-up, 2026-08-09):
# checked every real citywide match (222 ways) and found 29 carry a
# "golf" tag (golf=path/cartpath) -- real cart paths on golf courses,
# where foot=designated/yes marks the walking lane of the path (as
# opposed to the cart lane), not "the general public may enter" the way
# it does on Queensboro Bridge. Confirmed one of these was already live
# in production (a Marine Park golf cart path, data/tiles/r7c14.json.gz)
# before this exclusion existed. The other 193 matches don't carry a golf
# tag and are real, correct overrides (the original Queensboro Bridge
# case, Columbia's College Walk, Fulton Mall, gated communities that
# block cars but explicitly admit pedestrians) -- narrowing to exclude
# only "golf" rather than dropping the whole foot-override mechanism.
#
# Adds track to the highway allowlist too (FIXES.md item 1f, 2026-08-09):
# one real case found during that item's citywide survey, way 1240381845
# (40.63868,-73.87592), is tagged access=no + foot=yes + horse=yes -- the
# exact Queensboro Bridge pattern -- but fell through untouched because
# track wasn't in this filter's own highway list, even after being added
# to WALK_FILTER's.
FOOT_OVERRIDES_ACCESS_FILTER = (
    '["highway"~"primary|primary_link|secondary|secondary_link|tertiary|tertiary_link'
    '|unclassified|residential|living_street|pedestrian|footway|path|steps|service|track"]'
    '["area"!~"yes"]'
    '["foot"~"designated|yes"]'
    '["access"~"private|no"]'
    '["footway"!~"sidewalk"]'
    '["golf"!~"."]'
)

# A fourth query, also unioned into the main fetch: WALK_FILTER's
# ["footway"!~"sidewalk"] clause is right for its stated purpose --
# dropping the thousands of unnamed sidewalk fragments mapped alongside
# ordinary streets, which duplicate a street centerline we already have
# -- but NYC parks' own named interior paths get tagged footway=sidewalk
# too (they ARE, technically, the "sidewalk" of the park's own internal
# drive road, in OSM's tagging convention), and dropping those isn't
# removing a redundant duplicate, it's removing the only representation
# of a real, named path that exists nowhere else in the data.
#
# Confirmed real and load-bearing, not cosmetic: "Central Park Outer
# Loop" (ways 153232754, 835983087, and others) is tagged
# footway=sidewalk right at the W65th/Central Park West entrance, and
# WALK_FILTER was dropping it there -- two points only 104m apart in
# reality came out 787m apart in our graph, because the router had no
# nearby way into the interior path network and had to detour to a
# different entrance. A real street sidewalk is essentially always
# unnamed (WALK_FILTER's own comment already says so); a footway=sidewalk
# way that DOES have a name is exactly the "actually a real, notable path"
# signal WALK_FILTER's allowlist structure can't express (Overpass QL
# brackets are ANDed -- "NOT sidewalk OR has-a-name" needs its own query,
# same as CYCLEWAY_FILTER/FOOT_OVERRIDES_ACCESS_FILTER above).
NAMED_SIDEWALK_FILTER = (
    '["highway"~"primary|primary_link|secondary|secondary_link|tertiary|tertiary_link'
    '|unclassified|residential|living_street|pedestrian|footway|path|steps|service"]'
    '["area"!~"yes"]'
    '["foot"!~"no"]'
    '["access"!~"private|no"]'
    '["service"!~"private|driveway|parking_aisle"]'
    '["footway"="sidewalk"]'
    '["name"]'
)

# A fifth query -- NAMED_SIDEWALK_FILTER without the ["name"] requirement,
# so it matches EVERY walkable footway=sidewalk way. On its own that would
# re-admit the thousands of duplicate street sidewalks WALK_FILTER exists
# to exclude, so it is never unioned in whole: _park_reach_sidewalks()
# keeps only the segments that actually reach a real park polygon (see
# fetch_streets()) and discards the rest.
#
# Why the name test alone wasn't enough: measured over a unified graph of
# Central Park's south end -- one graph, no tiling, so tile-boundary
# effects were impossible by construction -- the router still walked 1443m
# between two points the true network connects in 1287m. The missing
# stretch is the UNNAMED footway=sidewalk mesh linking the Outer Loop to
# the street grid at Grand Army Plaza; a park's entrance paths are exactly
# as likely to be unnamed in OSM as named, and NAMED_SIDEWALK_FILTER can
# only see the named half. Admitting all sidewalks citywide instead does
# fix the route, but cost +59% edges in a dense park-free stretch of the
# Upper East Side and pushed unnamed edges from 47% to 67% of the graph
# (route descriptions read from those names). Filtering to park reach hits
# the same 1286m route for +10% edges near parks and, because the filter
# runs BEFORE the single simplify pass, +0% -- byte-identical output --
# where there are no parks. See PLAN.md.
ANY_SIDEWALK_FILTER = NAMED_SIDEWALK_FILTER.replace('["name"]', '')

# A sixth query: every parking_aisle way, the one WALK_FILTER's own
# service clause excludes outright (FIXES.md item 1b). Most are real
# duplicates or dead-end spurs into a single row of parking spaces, but a
# real through-path across a large lot is common enough to matter --
# 646 substantial lots citywide save a real, measured detour. On its own
# this would re-admit every dead-end aisle too, so it's never unioned in
# whole: _through_path_parking_aisles() (see fetch_streets()) keeps only
# the clusters that connect to the surrounding street network at 2+
# distinct points and discards the rest.
#
# Deliberately no access clause, unlike every other filter above --
# checked directly against 8 real, confirmed through-path lots (Lowe's
# Gowanus, several Staten Island big-box stores, Aviator Sports/Floyd
# Bennett Field): 6 of 8 carry access=private/customers on at least some
# aisles, one (a Home Depot) on every single aisle way, despite being an
# obvious, heavily-used public shortcut. Gating on it would exclude most
# of the real cases this filter exists to find. foot=no is kept as an
# exclusion -- a much more direct pedestrian-specific signal than a
# general access restriction.
#
# tunnel=building_passage IS excluded, unlike access -- 38 ways citywide
# (confirmed 2026-08-08), real but rare. This tag means the aisle runs
# through or under a building: a private indoor/underground garage lane
# (confirmed example: a motor_vehicle-only lane tunneling under a Times
# Square hotel), categorically different from an open lot's through-path
# regardless of how many street connections it has -- it's almost always
# just a driveway into a building, not a public-feeling shortcut.
PARKING_AISLE_FILTER = (
    '["highway"="service"]["service"="parking_aisle"]'
    '["foot"!~"no"]'
    '["tunnel"!~"building_passage"]'
)

# Fence/wall/hedge ways, used to veto an interior-sidewalk connection
# whose straight line to the street would cross one (see
# _snap_interior_sidewalks, FIXES.md item 1a). Known incomplete -- OSM's
# barrier tagging is crowdsourced and nowhere near complete (confirmed:
# 103 real barrier features in a single small NYCHA sample area alone),
# and no professionally-surveyed dataset fills the gap either (checked
# the full NYC Planimetric catalog: only a narrow "Retaining Wall" layer
# exists, not general fencing) -- but a real, mapped barrier is still
# real evidence against a connection, worth checking rather than
# ignoring. Deliberately excludes "kerb" (blocks nobody) and
# "gate"/"lift_gate"/"bollard" (those mark an intentional passage point,
# not a permanent block).
BARRIER_FILTER = '["barrier"~"fence|wall|hedge|retaining_wall|chain|city_wall"]'

# How close two interior-sidewalk segments' endpoints need to be to count
# as the same real point when stitching a tile's segments together (see
# _build_interior_sidewalk_graph) -- independently digitized segments
# meeting at the same real point essentially never share an exact
# coordinate.
INTERIOR_SIDEWALK_MERGE_TOLERANCE_M = 1.0

# How close a loose end needs to be to the real street network to connect
# (see _snap_interior_sidewalks). Chosen from real data, not guessed:
# median real distance from a genuine loose end to the nearest street is
# 0.6m, 95% are within 5m (confirmed live, Holmes Towers, NYCHA,
# Manhattan). Deliberately on the small side rather than maximizing how
# many real connections get captured: since BARRIER_FILTER's own coverage
# is known incomplete, a shorter distance is the one lever available to
# keep the unverifiable "connects through an unmapped obstacle" risk
# small.
INTERIOR_SIDEWALK_SNAP_MAX_M = 5.0

# A split landing within this distance of an edge's existing endpoint is
# treated as landing ON that endpoint, not a fraction of a meter away --
# a real case (a loose end can snap right where the original edge already
# meets an intersection), and also avoids handing substring() a
# zero-length span, which can't build a valid LineString from it.
_MIN_SPLIT_GAP_M = 1e-6

# Real park-trail classification tiers (NYC Parks Trails' own `class`
# field) confirmed to mean an obvious, real, walkable path (FIXES.md item
# 1g) -- a manual spot-check across three real parks (Alley Pond,
# Prospect, Van Cortlandt) found every Class IV/V segment checked was a
# real path, while Class III and below was unreliable: no other field
# (surface, width, named vs. unnamed) predicts which of those are real
# trails and which are nothing on the ground. Checked live (2026-08-12)
# that these are the dataset's only two "developed" tiers and there's no
# spelling/spacing inconsistency to worry about: 5 distinct `class`
# values citywide, counts sum to the full 7,058-row dataset.
PARK_TRAIL_CLASSES = frozenset({
    "Class IV : Highly Developed",
    "Class V : Fully Developed",
})

# How far a real trail can sit from the walk graph built so far and still
# count as "already covered" (see _missing_park_trail_graph) -- the same
# tolerance used throughout FIXES.md item 1g's own three-park survey.
# Small enough that a trail running parallel to, but genuinely separate
# from, an existing path still counts as missing.
PARK_TRAIL_OVERLAP_BUFFER_M = 8.0

# Below this length, a leftover "missing" stretch of trail (after
# subtracting existing coverage) is digitization noise -- an 8m buffer
# against a real, slightly wavy path clips in and out of coverage by a
# meter or two in places that aren't a real gap (see
# _uncovered_trail_segments) -- not a real gap worth splicing into the
# graph.
PARK_TRAIL_MIN_GAP_M = 15.0

# Same reasoning as INTERIOR_SIDEWALK_MERGE_TOLERANCE_M, applied to trail
# data instead: independently-digitized segments meeting at the same real
# point essentially never share an exact coordinate. Kept as its own
# constant rather than reusing the interior-sidewalk one -- this is a
# different survey product, and there's no reason its own digitization
# precision has to match.
PARK_TRAIL_MERGE_TOLERANCE_M = 1.0

# How close a missing trail sub-segment's loose end needs to be to the
# real walk network to connect (see _snap_interior_sidewalks). Unlike
# INTERIOR_SIDEWALK_SNAP_MAX_M (chosen from how far a real loose end
# measures from the street network), this one is a structural lower
# bound, not a data-driven measurement: a missing sub-segment is built by
# cutting a trail exactly at the edge of PARK_TRAIL_OVERLAP_BUFFER_M's
# 8m buffer, so its own endpoint can legitimately sit close to 8m from
# the nearest covered edge by construction. Set comfortably above that
# (8m + margin) rather than reusing INTERIOR_SIDEWALK_SNAP_MAX_M's 5m,
# which would routinely reject a trail's own just-computed connection
# point.
PARK_TRAIL_SNAP_MAX_M = 10.0

# Curated closure zones (FIXES.md item 0b): polygons where the imported
# synthetic layers (interior sidewalks, park trails) must NOT be admitted
# because the real ground is a construction closure that the city's own
# datasets still show as open paths. This can't be inferred from OSM tags
# -- measured 2026-08-14 against the 32 confirmed East River Park ghost
# edges: 0/32 sit inside any landuse=construction polygon and only 8/32
# near a highway=construction way (OSM's mappers DELETED the paths
# instead), while proximity rules wrongly flag 215 fine paths elsewhere.
# From the pipeline's view, "closed and removed from OSM" is
# indistinguishable from "never mapped in OSM" -- and the latter is the
# entire reason these imports exist. So closures are recorded as
# hand-drawn, evidence-based polygons (pipeline/closure_zones.json, one
# entry per real-world closure, each with a note saying when to delete
# it), the same curated-evidence philosophy as the server's
# KNOWN_NODE_GAPS and phantom-connector blocklist. Only the imported
# layers are filtered -- OSM's own ways already reflect the closure.
CLOSURE_ZONES_PATH = Path(__file__).parent.parent / "closure_zones.json"

# A leftover piece of an imported segment shorter than this after
# clipping a closure zone out of it is a boundary sliver, not a path.
CLOSURE_ZONE_MIN_REMNANT_M = 10.0


def _load_closure_zones(path: Path = CLOSURE_ZONES_PATH) -> list:
    """Closure polygons in METRIC_CRS (buffering/measuring happens in
    meters throughout -- see the dual-CRS note in CLAUDE.md)."""
    if not path.exists():
        return []
    zones = []
    with open(path) as f:
        for zone in json.load(f)["zones"]:
            ring_m = [_TO_METRIC_CRS(lon, lat) for lon, lat in zone["polygon"]]
            zones.append(Polygon(ring_m))
    return zones


_CLOSURE_ZONES_M = _load_closure_zones()


def _clip_closure_zones(line: LineString) -> list[LineString]:
    """The portion(s) of an imported lon/lat segment OUTSIDE every closure
    zone -- same clip-and-keep-remnants shape as _uncovered_trail_segments.
    Returns [line] untouched when no zone intersects (the common case:
    zones are rare and tiny). Remnants shorter than
    CLOSURE_ZONE_MIN_REMNANT_M are dropped as boundary slivers."""
    if not _CLOSURE_ZONES_M:
        return [line]
    line_m = transform(_TO_METRIC_CRS, line)
    touched = False
    remainder = line_m
    for zone_m in _CLOSURE_ZONES_M:
        if remainder.is_empty or not remainder.intersects(zone_m):
            continue
        touched = True
        remainder = remainder.difference(zone_m)
    if not touched:
        return [line]
    parts = remainder.geoms if hasattr(remainder, "geoms") else [remainder]
    kept_m = [p for p in parts
              if isinstance(p, LineString) and p.length >= CLOSURE_ZONE_MIN_REMNANT_M]
    return [transform(_FROM_METRIC_CRS, p) for p in kept_m]


# osmnx's own HTTP-response cache is disabled -- it has no expiration and
# no connection to GRAPH_CACHE_VERSION below, so it can silently keep
# serving a stale or incomplete Overpass response forever, even after a
# deliberate version bump asks for a fresh refetch. Confirmed real: the
# Bronx East 147th Street bug (FIXES.md) was exactly this -- a one-off
# incomplete Overpass response, permanently frozen by this cache, that
# no amount of "re-fetch this tile" ever actually re-asked Overpass for.
# GRAPH_CACHE_VERSION's own .graphml cache below already does the caching
# we actually want (skip re-fetching a tile that hasn't changed), tied to
# a version we control -- this second, hidden, unversioned cache underneath
# it doesn't add anything in normal operation, only risk.
ox.settings.use_cache = False

# osmnx's default User-Agent/referer is the same generic string every
# osmnx user on the planet sends ('OSMnx Python package (...)') --
# Overpass's own usage policy explicitly asks apps to identify themselves
# uniquely rather than blend into that shared, un-attributable traffic.
# Set once, here, for every fetch this module makes.
ox.settings.http_user_agent = "Shadewalker (https://github.com/camrynobscura/shadewalker)"
ox.settings.http_referer = ox.settings.http_user_agent

# overpass-api.de is the default (osmnx's own default, left explicit
# here). It intermittently refuses connections under sustained
# borough-scale querying (see MAX_FETCH_RETRIES's comment) -- tried
# switching to overpass.private.coffee (formerly Kumi Systems), which
# states no rate limit for exactly this kind of one-time bulk fetch, but
# it was itself unresponsive (timed out on even a trivial status check)
# the night this was tried. Reverted rather than fail over automatically
# between them -- one bad night isn't enough evidence to trust either
# one over the other, and checking actual usage (268 queries, ~277MB in
# the day this was investigated) confirmed we were nowhere near
# overpass-api.de's own stated 10,000-query/1GB daily guidance anyway,
# so the connection drops weren't us tripping a real limit.
ox.settings.overpass_url = "https://overpass-api.de/api"

# v19: retain the `layer` tag on fetched ways (osmnx's default keeps
# bridge/tunnel but not layer). The vertical-suspects detector (see
# pipeline/graph/vertical_audit.py) needs it: plenty of real elevated
# pedestrian structures carry layer=N with no bridge tag at all (the
# High Line's viaduct sections, the Brooklyn Heights Promenade's
# layer=3), and 2026-08-14's offline audit measured those exact ways
# hosting confirmed phantom welds. Costs nothing beyond a slightly
# larger graphml; baked into the fetched graph, hence part of the v19
# cache bump.
if "layer" not in ox.settings.useful_tags_way:
    ox.settings.useful_tags_way = ox.settings.useful_tags_way + ["layer"]


def _fetch_with_retry(bbox: Bbox, custom_filter: str, tile_id: str, label: str) -> nx.MultiDiGraph | None:
    """One Overpass fetch (via osmnx), retrying on transient connection
    errors. None means osmnx found zero ways matching custom_filter here --
    real and unremarkable for either filter (the main WALK_FILTER on a
    mostly-open-water tile; CYCLEWAY_FILTER on the common case of a tile
    with no foot-designated cycleways at all), not a bug to retry.

    osmnx bbox order is (left, bottom, right, top) = (west, south, east, north).

    retain_all=True, NOT False, and the distinction cost a real
    neighborhood: retain_all=False keeps only the largest connected
    component -- decided per tile, on an internal working graph that
    extends ~500m past the tile, BEFORE the final clip -- so a
    neighborhood that reads as "disconnected" through one tile's peephole
    gets deleted at fetch time even when it connects fine through a
    neighboring tile's streets. Red Hook (walled off by the expressway
    trench + water on three sides) lost that contest in every tile that
    saw it and vanished from the data entirely. Keeping everything per
    tile is safe because the server prunes globally at load time (see
    graph_store.load()), with the whole merged picture in view -- that's
    the right scope for the keep-or-drop decision, and it's also what
    still protects the router from stray fragments (the original reason
    this was False).

    "No data here" surfaces as a plain ValueError from osmnx, not one
    consistent exception type -- observed two different real messages from
    two different internal code paths ("No data elements in server
    response" when Overpass itself returns nothing, "Found no graph nodes
    within the requested polygon" when Overpass returns something just
    outside the tile's exact edge but nothing survives clipping to it).
    Catching the shared ValueError base rather than either specific
    message/subclass is deliberate: bbox is already validated by
    get_tile_bbox() before this call, so any ValueError from osmnx here is
    effectively guaranteed to mean "empty area," not a real bug -- and a
    narrower catch would just mean discovering a third message the hard
    way, mid-borough-run, again.

    ConnectionError gets its own, separate handling (retry, not skip) --
    unlike ValueError, it says nothing about whether this tile has data,
    only that this one attempt to ask didn't reach the server.

    simplify=False, NOT True (osmnx's own default): each of our three
    queries only ever sees the ways ITS OWN filter matched, so simplifying
    right here -- before the three results are combined -- lets a real
    junction between two different filters' ways get mishandled. Real
    case (High Bridge, confirmed 2026-07-18): the node where its cycleway
    (matched only by CYCLEWAY_FILTER) meets University Avenue (matched
    only by WALK_FILTER) looks like a plain pass-through point to
    WALK_FILTER's isolated view, so WALK_FILTER's own simplification
    folds it into a longer street edge and drops that specific node id;
    CYCLEWAY_FILTER's isolated view keeps the node, but as a dead end,
    since it has no idea University Avenue exists. Compose ends up with
    the node, but not its real connection to the street grid. Confirmed
    the same mechanism drops a whole way outright when a filter's own
    match set is sparse and mostly disconnected (RFK/Triborough's
    "Randall's Island Connector": 128 raw edges simplified down to 2 in
    CYCLEWAY_FILTER's own isolated fetch, and that way wasn't one of the
    2). fetch_streets() simplifies once, after composing all three --
    see its own docstring.
    """
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            return ox.graph_from_bbox(
                bbox=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
                custom_filter=custom_filter,
                retain_all=True,
                simplify=False,
            )
        except ValueError:
            return None
        except requests.exceptions.ConnectionError:
            if attempt == MAX_FETCH_RETRIES:
                raise
            wait_s = FETCH_RETRY_BACKOFF_S * (2 ** (attempt - 1))
            print(f"  [streets] {tile_id}: connection error fetching {label} "
                  f"(attempt {attempt}/{MAX_FETCH_RETRIES}), retrying in {wait_s}s...")
            time.sleep(wait_s)


def _bbox_signature(bbox: Bbox) -> str:
    """A stable string identifying the exact area a cached graph covers.

    Stored inside the GraphML and re-checked on load, because the cache
    FILENAME is not a sufficient key: a tile id only means an area relative
    to the current grid definition, and that definition has moved. Commit
    a461e36 shifted CITY_BBOX.lat_min from 40.49 to 40.472 -- exactly
    TILE_SIZE_LAT_DEG, one whole row -- so every tile id silently came to
    mean the area one row south of what it had meant. GRAPH_CACHE_VERSION
    should have been bumped (its own comment says "or the fetch bbox logic
    changes," and redefining the grid changes it for every tile), but
    wasn't, so caches written before the shift kept matching their
    filenames while no longer matching their contents.

    Two tiles survived that way for twelve days: r17c14 was serving South
    Bronx streets and r19c13 Washington Heights, both from one row north.
    Everything else happened to get re-fetched after the shift. Discipline
    is what failed here, so this records the real bbox rather than relying
    on remembering to bump a constant.
    """
    return (f"{bbox.lat_min:.6f},{bbox.lat_max:.6f},"
            f"{bbox.lon_min:.6f},{bbox.lon_max:.6f}")


def _park_reach_sidewalks(
    graph: nx.MultiDiGraph | None, park_reach: prepared.PreparedGeometry, tile_id: str
) -> nx.MultiDiGraph | None:
    """Keep only the ANY_SIDEWALK_FILTER edges that reach a real park,
    dropping the duplicate-of-a-street-centerline majority. None (here or
    passed in) means nothing survived, which fetch_streets() treats the
    same as a filter matching nothing at all.

    Tests each edge's MIDPOINT for containment rather than measuring what
    fraction of its length falls inside, because a lone containment check is
    what prepared geometry can actually accelerate (prep() speeds up
    predicates, not .intersection()).

    An earlier version of this comment justified that by calling these
    segments "short enough that the midpoint and a length-fraction test
    agree." The first half of that is false and was never measured: they come
    from an unsimplified fetch, so each is one straight run between
    consecutive OSM nodes, and a straight 250m sidewalk with two nodes is one
    250m segment. Measured on r16c11: median 8.5m, but p90 87m, p99 249m, and
    24.6% longer than 60m -- against a 30m buffer.
    The conclusion happens to hold anyway, which is why this still uses the
    midpoint: running both rules over the same 6,266 segments, they disagreed
    on **12** (0.2%), all admitted by midpoint and rejected by fraction, and
    the resulting routes were indistinguishable. Long segments here are
    almost always wholly inside or wholly outside park reach, not straddling.
    So this is a cheap approximation that was verified rather than assumed --
    switching to the exact test would cost a full re-fetch (it changes graph
    content, so GRAPH_CACHE_VERSION would have to bump) to move 0.2% of
    admitted segments. That 12 is r16c11's count, not a citywide one; the
    citywide number was never measured, only the rate.

    park_reach is in METRIC_CRS, so midpoints reproject into it before
    testing -- the dual-CRS rule: the buffer that built park_reach is only
    meaningful in meters, and comparing a degree-space point against it
    would silently mean something else entirely.
    """
    if graph is None:
        return None

    keep_edges = []
    for u, v, key in graph.edges(keys=True):
        mid_lon = (graph.nodes[u]["x"] + graph.nodes[v]["x"]) / 2
        mid_lat = (graph.nodes[u]["y"] + graph.nodes[v]["y"]) / 2
        if park_reach.contains(Point(_TO_METRIC_CRS(mid_lon, mid_lat))):
            keep_edges.append((u, v, key))

    dropped = graph.number_of_edges() - len(keep_edges)
    print(f"  [streets] {tile_id}: park-reach sidewalks: kept {len(keep_edges)}, "
          f"dropped {dropped} duplicate-of-street segments")
    if not keep_edges:
        return None

    return graph.edge_subgraph(keep_edges).copy()


def _through_path_parking_aisles(
    graph: nx.MultiDiGraph, aisle_graph: nx.MultiDiGraph | None, tile_id: str
) -> nx.MultiDiGraph | None:
    """Keep only parking_aisle edges that form a real through-path,
    dropping dead-end aisles that only reach a single row of parking
    spaces (FIXES.md item 1b). None (here or passed in) means nothing
    survived, matching _park_reach_sidewalks()'s convention.

    A real through-path is a connectivity question, not a tag lookup --
    unlike 1a/1d, nothing in OSM or the NYC Planimetric Database marks an
    aisle as "this one goes somewhere." Instead: group aisle_graph's edges
    into connected clusters (undirected -- a parking aisle is walkable
    both ways regardless of its OSM digitized direction), and keep a whole
    cluster only if 2+ of its nodes already exist in `graph`. That means
    the cluster touches the real street network at two separate places --
    walk in one side, out the other -- not just one entrance you'd have to
    double back out of. Validated directly against 8 real lots (Lowe's
    Gowanus, several Staten Island big-box stores, Aviator Sports/Floyd
    Bennett Field): every one connects at 2+ points once composed with the
    surrounding streets.

    Deliberately ignores access=private/customers on the aisle ways
    themselves -- see PARKING_AISLE_FILTER's own comment for why gating on
    it would exclude most of the real cases this exists for.
    """
    if aisle_graph is None:
        return None

    undirected_aisles = aisle_graph.to_undirected(as_view=True)
    existing_nodes = set(graph.nodes)

    keep_edges = []
    dropped_clusters = 0
    for component in nx.connected_components(undirected_aisles):
        if len(component & existing_nodes) >= 2:
            keep_edges.extend(aisle_graph.subgraph(component).edges(keys=True))
        else:
            dropped_clusters += 1

    print(f"  [streets] {tile_id}: parking aisles: kept {len(keep_edges)} through-path edges, "
          f"dropped {dropped_clusters} dead-end clusters")
    if not keep_edges:
        return None

    return aisle_graph.edge_subgraph(keep_edges).copy()


def _interior_sidewalks_for_tile(geojson: dict, bbox: Bbox) -> list[list[tuple[float, float]]]:
    """Interior sidewalk centerline segments (as plain coordinate lists)
    from the citywide interior-sidewalk cache whose geometry intersects
    this tile's bbox (FIXES.md item 1a).
    interior_sidewalks.fetch_interior_sidewalks() always returns the whole
    city -- each tile filters its own slice out in memory here, same
    division of labor as parks.py's whole-city cache + per-tile canopy
    lookups."""
    tile_box = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    segments = []
    for feature in geojson["features"]:
        coords = [tuple(point) for point in feature["geometry"]["coordinates"]]
        line = LineString(coords)
        if not tile_box.intersects(line):
            continue
        # closure zones (FIXES.md item 0b): drop the portions of imported
        # paths inside a known construction closure -- see
        # CLOSURE_ZONES_PATH's comment for why this can't be tag-inferred
        for kept in _clip_closure_zones(line):
            segments.append([tuple(point) for point in kept.coords])
    return segments


def _park_trails_for_tile(geojson: dict, bbox: Bbox) -> list[LineString]:
    """Class IV/V ("Highly Developed"/"Fully Developed") park trail
    segments (FIXES.md item 1g) from the citywide NYC Parks Trails cache
    whose geometry intersects this tile's bbox -- same division of labor
    as _interior_sidewalks_for_tile: park_trails.fetch_park_trails()
    always returns the whole city, each tile filters its own slice out in
    memory here.

    Excludes Class I/II/III -- see PARK_TRAIL_CLASSES's own comment for
    why: a spot-check across three real parks found no other field
    reliably distinguishes a real trail from nothing on the ground within
    that lower tier.

    A trail's geometry can be a MultiLineString with more than one
    disconnected part -- each part becomes its own entry here, same as if
    it were a separate row.
    """
    tile_box = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    lines = []
    for feature in geojson["features"]:
        if feature["properties"].get("class") not in PARK_TRAIL_CLASSES:
            continue
        geometry = shape(feature["geometry"])
        parts = geometry.geoms if hasattr(geometry, "geoms") else [geometry]
        for part in parts:
            if not tile_box.intersects(part):
                continue
            # closure zones (FIXES.md item 0b) -- same clip as interior
            # sidewalks; see CLOSURE_ZONES_PATH's comment
            lines.extend(_clip_closure_zones(part))
    return lines


def _build_interior_sidewalk_graph(
    segments: list[list[tuple[float, float]]],
    tile_id: str,
    *,
    label: str = "interior sidewalks",
    merge_tolerance_m: float = INTERIOR_SIDEWALK_MERGE_TOLERANCE_M,
    start_id: int = -1,
    start_edge_osmid: int = -1,
) -> nx.MultiDiGraph | None:
    """Turn one tile's independently-digitized path segments into a small
    graph -- originally built for interior sidewalks (FIXES.md item 1a),
    now shared with park trails (FIXES.md item 1g), since both are "a
    real path from a non-OSM NYC dataset" with the same shape of problem.
    Unlike OSM ways, these segments arrive independently digitized -- two
    segments meeting at the same real-world point don't necessarily share
    a coordinate -- so endpoints within merge_tolerance_m of an
    already-placed point are merged into that same node instead of kept
    as separate touching points.

    Synthetic node ids are negative ints: real OSM node ids are always
    large positive ints (confirmed against real cached tiles), so nothing
    here can collide with a real osmid once composed with the street
    graph. start_id/start_edge_osmid default to -1 (this function's
    original, only behavior) but let a caller composing a SECOND
    synthetic source into the same tile (park trails, after interior
    sidewalks) continue numbering from wherever the first source's ids
    already ended, rather than restarting at -1 and silently colliding
    with an id that already means a different real-world point once both
    are composed into the same graph.

    None if segments is empty.
    """
    if not segments:
        return None

    graph = nx.MultiDiGraph()
    placed: list[tuple[int, Point]] = []
    next_id = start_id
    next_edge_osmid = start_edge_osmid

    def node_for(lon: float, lat: float) -> int:
        nonlocal next_id
        point_m = Point(_TO_METRIC_CRS(lon, lat))
        for node_id, placed_point_m in placed:
            if point_m.distance(placed_point_m) <= merge_tolerance_m:
                return node_id
        node_id = next_id
        next_id -= 1
        placed.append((node_id, point_m))
        graph.add_node(node_id, x=lon, y=lat)
        return node_id

    for coords in segments:
        node_ids = [node_for(lon, lat) for lon, lat in coords]
        for a, b in zip(node_ids, node_ids[1:]):
            if a != b:
                # These segments come from ArcGIS, not OSM, so there's no
                # real osmid to carry over -- centerline.build_edge_table()
                # requires one on every edge (real bug, 2026-08-09: a
                # missing one here crashed osmnx's own to_undirected() the
                # first time this ran on a real tile). Negative, same
                # reasoning as the node ids above. length is required too
                # (same function, one step further) -- real meters between
                # the two endpoints, not inherited from anything.
                graph.add_edge(
                    a, b, osmid=next_edge_osmid, length=_node_distance_m(graph, a, b)
                )
                next_edge_osmid -= 1

    print(f"  [streets] {tile_id}: {label}: {graph.number_of_nodes()} points "
          f"from {len(segments)} segments")
    return graph


def _edge_line_m(g: nx.MultiDiGraph, u, v, k) -> LineString:
    """A graph edge's real geometry (or a straight line between its two
    endpoint nodes, if it has none), reprojected into METRIC_CRS."""
    data = g.edges[u, v, k]
    if "geometry" in data:
        coords = list(data["geometry"].coords)
    else:
        coords = [(g.nodes[u]["x"], g.nodes[u]["y"]), (g.nodes[v]["x"], g.nodes[v]["y"])]
    return LineString([_TO_METRIC_CRS(lon, lat) for lon, lat in coords])


def _to_lonlat(line_m: LineString) -> LineString:
    return LineString([_FROM_METRIC_CRS(x, y) for x, y in line_m.coords])


def _node_distance_m(g: nx.MultiDiGraph, a, b) -> float:
    """Real straight-line distance between two of g's nodes, in meters --
    needed on every synthetic edge (centerline.build_edge_table() requires
    a real `length` on every edge, same as `osmid`; real bug, 2026-08-09:
    a missing one here crashed the pipeline one step past the osmid fix,
    on the exact same real tile)."""
    point_a = Point(_TO_METRIC_CRS(g.nodes[a]["x"], g.nodes[a]["y"]))
    point_b = Point(_TO_METRIC_CRS(g.nodes[b]["x"], g.nodes[b]["y"]))
    return point_a.distance(point_b)


def _apply_edge_splits(
    result: nx.MultiDiGraph,
    graph: nx.MultiDiGraph,
    u, v, k,
    splits: list[tuple[float, object, Point]],
    next_id: int,
    next_edge_osmid: int,
) -> tuple[int, int]:
    """Replace one street edge with a chain of sub-edges, one new node per
    real interior-sidewalk connection landing on it (FIXES.md item 1a).
    Applied for every loose end snapping onto the SAME original edge
    together, in one call, sorted by position along it -- splitting one at
    a time as each loose end is found would let a second split silently
    work from a stale picture of the edge the first one already cut in
    two.

    Every new sub-edge needs an osmid and a length (centerline.
    build_edge_table() requires both on every edge; real bug, 2026-08-09:
    missing ones here crashed the pipeline -- first on osmid, then, one
    line further, on length -- the first time this ran on a real tile). A
    geometry-bearing sub-segment is still genuinely part of the original
    street, so it keeps that street's real osmid, and its length is
    recomputed from its own (shorter) geometry, not inherited from the
    original edge's full length. The plain connector edges to a loose end
    never existed in OSM at all, so they each get their own synthetic
    osmid instead (same negative-int reasoning as the synthetic node ids)
    and a real length computed from their own two endpoints.

    Returns the next available (synthetic node id, synthetic edge osmid),
    so a caller splitting several different edges keeps one running
    counter of each across all of them.
    """
    splits = sorted(splits, key=lambda item: item[0])
    original_line_m = _edge_line_m(graph, u, v, k)
    original_osmid = graph.edges[u, v, k].get("osmid")

    result.remove_edge(u, v, k)

    chain_start = u
    prev_distance = 0.0
    for distance_along, loose_end_node, snap_point_m in splits:
        if distance_along <= prev_distance + _MIN_SPLIT_GAP_M:
            # The snap point lands right on top of chain_start itself (a
            # real case: a loose end can snap to right where the original
            # edge meets an existing intersection) -- connect directly
            # instead of inserting a zero-length duplicate node, which
            # substring() below can't build a valid LineString from anyway.
            result.add_edge(
                chain_start, loose_end_node, osmid=next_edge_osmid,
                length=_node_distance_m(result, chain_start, loose_end_node),
                weld=True,  # pipeline-manufactured connector -- see vertical_audit
            )
            next_edge_osmid -= 1
            continue

        split_node = next_id
        next_id -= 1
        lon, lat = _FROM_METRIC_CRS(snap_point_m.x, snap_point_m.y)
        result.add_node(split_node, x=lon, y=lat)

        sub_geometry = substring(original_line_m, prev_distance, distance_along)
        result.add_edge(
            chain_start, split_node, geometry=_to_lonlat(sub_geometry),
            osmid=original_osmid, length=sub_geometry.length,
        )
        result.add_edge(
            split_node, loose_end_node, osmid=next_edge_osmid,
            length=_node_distance_m(result, split_node, loose_end_node),
            weld=True,  # pipeline-manufactured connector -- see vertical_audit
        )
        next_edge_osmid -= 1

        chain_start = split_node
        prev_distance = distance_along

    if prev_distance >= original_line_m.length - _MIN_SPLIT_GAP_M:
        # Symmetric case: the last split landed right at v's own end of
        # the edge.
        if chain_start != v:
            result.add_edge(
                chain_start, v, osmid=next_edge_osmid,
                length=_node_distance_m(result, chain_start, v),
            )
            next_edge_osmid -= 1
    else:
        final_geometry = substring(original_line_m, prev_distance, original_line_m.length)
        result.add_edge(
            chain_start, v, geometry=_to_lonlat(final_geometry),
            osmid=original_osmid, length=final_geometry.length,
        )
    return next_id, next_edge_osmid


def _snap_interior_sidewalks(
    graph: nx.MultiDiGraph,
    interior_graph: nx.MultiDiGraph,
    barrier_graph: nx.MultiDiGraph | None,
    tile_id: str,
    *,
    snap_max_m: float = INTERIOR_SIDEWALK_SNAP_MAX_M,
    label: str = "interior sidewalks",
) -> nx.MultiDiGraph:
    """Attach interior_graph onto graph -- originally built for interior
    sidewalks (FIXES.md item 1a), now shared with park trails (FIXES.md
    item 1g) -- splitting the nearest street edge at the real snap point
    for each interior loose end (a node with no other connection within
    interior_graph) within snap_max_m. Confirmed live that snapping to the
    nearest existing NODE instead would misplace 63% of real connections
    by >3m, since these paths typically touch a street mid-block, not at
    an intersection -- so this always finds the nearest EDGE and inserts a
    new node there, never reuses an existing one.

    A loose end farther than snap_max_m, or whose straight-line connection
    crosses a real barrier_graph way, is left unconnected -- a real
    digitization gap or a genuine obstacle, not a bug to force-connect.
    barrier_graph is optional and known incomplete (see BARRIER_FILTER's
    own comment); None just means the barrier check is skipped entirely,
    same as if none matched.

    Unlike _park_reach_sidewalks/_through_path_parking_aisles, this can't
    just return edges for the caller to nx.compose() in: splitting an
    existing street edge mutates graph's own structure, not just adds to
    it. So this returns the whole resulting graph, and the caller
    reassigns rather than conditionally composes.

    New split-node ids and connector-edge osmids are numbered from
    result's OWN existing negative ids (graph composed with
    interior_graph), not just interior_graph's -- a second call snapping
    a different synthetic source into the same tile (park trails, called
    after interior sidewalks) would otherwise restart at -1 and silently
    collide with an id nx.compose would then merge into an unrelated,
    already-placed point instead of keeping the two apart.
    """
    result = nx.compose(graph, interior_graph)

    undirected_interior = interior_graph.to_undirected(as_view=True)
    loose_ends = [n for n in interior_graph.nodes if undirected_interior.degree(n) == 1]

    edge_keys = list(graph.edges(keys=True))
    if not edge_keys:
        print(f"  [streets] {tile_id}: {label}: no street edges to connect "
              f"{len(loose_ends)} loose ends to")
        return result

    edge_lines_m = [_edge_line_m(graph, u, v, k) for u, v, k in edge_keys]
    edge_tree = STRtree(edge_lines_m)

    barrier_lines_m = []
    if barrier_graph is not None:
        barrier_lines_m = [
            _edge_line_m(barrier_graph, u, v, k) for u, v, k in barrier_graph.edges(keys=True)
        ]

    splits_by_edge: dict[tuple, list[tuple[float, object, Point]]] = {}
    connected_count = 0
    for node in loose_ends:
        point_m = Point(_TO_METRIC_CRS(interior_graph.nodes[node]["x"], interior_graph.nodes[node]["y"]))
        edge_idx = edge_tree.nearest(point_m)
        nearest_line = edge_lines_m[edge_idx]
        if point_m.distance(nearest_line) > snap_max_m:
            continue

        distance_along = nearest_line.project(point_m)
        snap_point_m = nearest_line.interpolate(distance_along)

        connection = LineString([point_m, snap_point_m])
        if any(connection.crosses(barrier) for barrier in barrier_lines_m):
            continue

        splits_by_edge.setdefault(edge_keys[edge_idx], []).append((distance_along, node, snap_point_m))
        connected_count += 1

    existing_negative_ids = [n for n in result.nodes if isinstance(n, int) and n < 0]
    next_id = (min(existing_negative_ids) - 1) if existing_negative_ids else -1
    existing_negative_osmids = [
        data.get("osmid") for _, _, data in result.edges(data=True)
        if isinstance(data.get("osmid"), int) and data["osmid"] < 0
    ]
    next_edge_osmid = (min(existing_negative_osmids) - 1) if existing_negative_osmids else -1
    for (u, v, k), splits in splits_by_edge.items():
        next_id, next_edge_osmid = _apply_edge_splits(
            result, graph, u, v, k, splits, next_id, next_edge_osmid
        )

    dropped = len(loose_ends) - connected_count
    print(f"  [streets] {tile_id}: {label}: connected {connected_count} loose ends "
          f"to the street network, left {dropped} unconnected (too far or blocked by a barrier)")
    return result


def _uncovered_trail_segments(
    trail_line_m: LineString, covered_m, min_length_m: float
) -> list[LineString]:
    """The portion(s) of a real park-trail line (FIXES.md item 1g) not
    already covered by the walk graph built so far -- subtracting
    covered_m (the buffered union of every edge already in the graph)
    from the trail's own geometry leaves only genuinely missing stretches.

    A partially-covered trail doesn't always leave one clean remainder: an
    8m buffer against a real, slightly wavy path can clip in and out of
    coverage several times along its length, shattering the difference
    into a handful of disconnected pieces rather than one contiguous gap
    -- confirmed against the real three-park survey behind this fix, not
    just a hypothetical. Each piece shorter than min_length_m is dropped
    as digitization noise (a meter or two of buffer-edge slop), not a
    real gap worth adding to the graph.

    Both inputs and the result are in METRIC_CRS -- min_length_m is
    meaningless in lon/lat degrees.
    """
    remainder = trail_line_m.difference(covered_m)
    if remainder.is_empty:
        return []
    pieces = remainder.geoms if hasattr(remainder, "geoms") else [remainder]
    return [
        piece for piece in pieces
        if isinstance(piece, LineString) and piece.length >= min_length_m
    ]


def _missing_park_trail_graph(
    graph: nx.MultiDiGraph, trail_lines: list[LineString], tile_id: str
) -> nx.MultiDiGraph | None:
    """Build a small graph of the park-trail sub-segments genuinely
    missing from `graph` (FIXES.md item 1g) -- the portions of each real
    trail line not already within PARK_TRAIL_OVERLAP_BUFFER_M of an
    existing edge. None if nothing survives (the common case: a tile with
    no trails at all, or one where every trail is already fully covered).

    Buffers every edge CURRENTLY in `graph`, not some earlier snapshot --
    this runs after every other filter/source (interior sidewalks
    included) has already been composed in, so "already covered" reflects
    the complete walk network this tile will actually ship with.

    New synthetic node/edge ids continue from graph's own existing
    negative ids rather than restarting at -1 -- see
    _snap_interior_sidewalks' own comment on why a second synthetic
    source in the same tile has to avoid colliding with the first one's.
    """
    if not trail_lines:
        return None

    edge_lines_m = [_edge_line_m(graph, u, v, k) for u, v, k in graph.edges(keys=True)]
    covered_m = unary_union(edge_lines_m).buffer(PARK_TRAIL_OVERLAP_BUFFER_M)

    missing_segments_lonlat = []
    for trail_line in trail_lines:
        trail_line_m = transform(_TO_METRIC_CRS, trail_line)
        for piece_m in _uncovered_trail_segments(trail_line_m, covered_m, PARK_TRAIL_MIN_GAP_M):
            missing_segments_lonlat.append(list(_to_lonlat(piece_m).coords))

    if not missing_segments_lonlat:
        return None

    existing_negative_ids = [n for n in graph.nodes if isinstance(n, int) and n < 0]
    start_id = (min(existing_negative_ids) - 1) if existing_negative_ids else -1
    existing_negative_osmids = [
        data.get("osmid") for _, _, data in graph.edges(data=True)
        if isinstance(data.get("osmid"), int) and data["osmid"] < 0
    ]
    start_edge_osmid = (min(existing_negative_osmids) - 1) if existing_negative_osmids else -1

    return _build_interior_sidewalk_graph(
        missing_segments_lonlat, tile_id,
        label="park trails", merge_tolerance_m=PARK_TRAIL_MERGE_TOLERANCE_M,
        start_id=start_id, start_edge_osmid=start_edge_osmid,
    )


def fetch_streets(
    bbox: Bbox, tile_id: str, park_reach: prepared.PreparedGeometry | None = None
) -> nx.MultiDiGraph | None:
    """Return the walkable street graph for the bbox, cached per tile --
    the union of WALK_FILTER's main centerline query, CYCLEWAY_FILTER's
    narrower foot=designated-cycleway query,
    FOOT_OVERRIDES_ACCESS_FILTER's access=no/private-but-foot-designated
    query, NAMED_SIDEWALK_FILTER's named-park-path query,
    ANY_SIDEWALK_FILTER's any-sidewalk query narrowed to park reach,
    PARKING_AISLE_FILTER's every-parking-aisle query narrowed to
    through-paths, NYC's Interior Sidewalk Centerline data (a separate
    ArcGIS source, not an Overpass query) snapped onto the street network
    wherever it comes close enough, and NYC Parks' own Trails data (a
    separate Socrata source) trimmed down to whatever isn't already
    covered and snapped on the same way (see all six filter constants'
    comments, plus INTERIOR_SIDEWALK_SNAP_MAX_M, PARK_TRAIL_SNAP_MAX_M,
    and BARRIER_FILTER).

    Each is fetched unsimplified (see _fetch_with_retry's simplify=False
    comment for why) and composed into one graph BEFORE simplifying --
    once, here -- so osmnx's topology simplification sees every filter's
    ways together and can correctly tell a real intersection between two
    different filters' matches apart from a genuine dead end, instead of
    each filter's own simplification pass guessing from an incomplete
    picture. ANY_SIDEWALK_FILTER's park-reach narrowing likewise happens
    before that single simplify pass, not after: measured both ways, the
    same fix costs +0% edges in a park-free area when filtered first
    versus +14% when filtered afterwards, because sidewalks present at
    simplify time preserve intersection nodes that would otherwise
    collapse -- and deleting the edges later leaves those nodes stranded.

    park_reach (METRIC_CRS, from canopy.citywide_park_reach_m()) is
    optional: without it the ANY_SIDEWALK_FILTER query is skipped
    entirely, so the pilot tile and CI keep working with no parks dataset
    available. Whether it was supplied is part of the cache filename --
    the two results genuinely differ, and a graph built one way must never
    be silently reused for the other.

    None means the bbox has no OSM ways matching WALK_FILTER at all -- real
    for grid tiles that only clip a borough's real coastline at their
    edge (a tile can intersect a borough's polygon by a sliver that's
    still mostly open water; see PLAN.md), not a bug to retry.
    """
    variant = "" if park_reach is not None else "_noparkreach"
    graphml_path = STREETS_DIR / f"{tile_id}_v{GRAPH_CACHE_VERSION}{variant}.graphml"

    wanted = _bbox_signature(bbox)
    if graphml_path.exists():
        graph = ox.load_graphml(graphml_path)
        cached = graph.graph.get("fetch_bbox")
        if cached == wanted:
            print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (cached)")
            return graph
        # Deliberately a cache MISS, not an error: re-fetching self-heals,
        # and the alternative (trusting the filename) is what let two tiles
        # publish another neighbourhood's streets for twelve days. A cache
        # with no recorded bbox at all is equally untrustworthy -- it was
        # written before this check existed, so nothing verified it.
        print(f"  [streets] {tile_id}: cached graph covers {cached or 'an unrecorded area'}, "
              f"not {wanted} -- re-fetching")

    street_graph = _fetch_with_retry(bbox, WALK_FILTER, tile_id, "streets")
    if street_graph is None:
        print(f"  [streets] {tile_id}: no matching ways in this area (likely open water) -- skipping")
        return None

    # Neither extra query matching anything is the common case, not an
    # error -- most tiles have neither, and that's fine: street_graph
    # alone is a complete, valid result.
    graph = street_graph
    cycleway_graph = _fetch_with_retry(bbox, CYCLEWAY_FILTER, tile_id, "foot-designated cycleways")
    if cycleway_graph is not None:
        graph = nx.compose(graph, cycleway_graph)

    access_override_graph = _fetch_with_retry(
        bbox, FOOT_OVERRIDES_ACCESS_FILTER, tile_id, "foot-designated access=no/private ways"
    )
    if access_override_graph is not None:
        graph = nx.compose(graph, access_override_graph)

    named_sidewalk_graph = _fetch_with_retry(
        bbox, NAMED_SIDEWALK_FILTER, tile_id, "named park paths tagged footway=sidewalk"
    )
    if named_sidewalk_graph is not None:
        graph = nx.compose(graph, named_sidewalk_graph)

    # Kept past its own narrowing below: the vertical-suspects audit needs
    # the FULL sidewalk layer for reachability arbitration (ramps our
    # centerline filters exclude live in it) -- see vertical_audit's
    # docstring.
    any_sidewalk_graph = None
    if park_reach is not None:
        any_sidewalk_graph = _fetch_with_retry(
            bbox, ANY_SIDEWALK_FILTER, tile_id, "sidewalk-tagged ways near parks"
        )
        park_sidewalk_graph = _park_reach_sidewalks(any_sidewalk_graph, park_reach, tile_id)
        if park_sidewalk_graph is not None:
            graph = nx.compose(graph, park_sidewalk_graph)

    # Checked against `graph` as composed so far (every other filter
    # already unioned in), same "filter before the single simplify pass"
    # ordering as park-reach sidewalks above -- filtering afterwards would
    # strand intersection nodes the same way, see _park_reach_sidewalks()'s
    # own measured comment on this.
    parking_aisle_graph = _fetch_with_retry(bbox, PARKING_AISLE_FILTER, tile_id, "parking aisles")
    through_path_aisles = _through_path_parking_aisles(graph, parking_aisle_graph, tile_id)
    if through_path_aisles is not None:
        graph = nx.compose(graph, through_path_aisles)

    # Shared by both snapping passes below -- same bbox/filter either way,
    # so fetch it once and reuse rather than asking Overpass for identical
    # barrier data twice on every tile where both synthetic sources apply.
    # barrier_fetched (not just "barrier_graph is None") tracks whether the
    # fetch already happened, since _fetch_with_retry legitimately returns
    # None too (no barrier ways in this area) -- reusing None itself as the
    # "not fetched yet" sentinel would refetch on every tile with no real
    # barriers nearby, which is the common case.
    barrier_graph = None
    barrier_fetched = False

    # Not an Overpass query -- interior_sidewalks.fetch_interior_sidewalks()
    # fetches NYC's own ArcGIS-hosted survey data instead, cached whole
    # citywide (see that module) and filtered down to this tile here.
    # Same "before the single simplify pass" ordering as above.
    interior_geojson = interior_sidewalks.fetch_interior_sidewalks()
    interior_segments = _interior_sidewalks_for_tile(interior_geojson, bbox)
    interior_graph = _build_interior_sidewalk_graph(interior_segments, tile_id)
    if interior_graph is not None:
        barrier_graph = _fetch_with_retry(bbox, BARRIER_FILTER, tile_id, "barrier ways")
        barrier_fetched = True
        graph = _snap_interior_sidewalks(graph, interior_graph, barrier_graph, tile_id)

    # Also not an Overpass query -- park_trails.fetch_park_trails() fetches
    # NYC Parks' own Trails survey instead (FIXES.md item 1g), cached whole
    # citywide and filtered down to this tile here. Runs AFTER interior
    # sidewalks, not before: _missing_park_trail_graph's own "already
    # covered" check buffers whatever is CURRENTLY in `graph`, so this
    # ordering is what lets a trail running along an interior sidewalk
    # count as covered too, not just one running along a real OSM street.
    # Same "before the single simplify pass" ordering as every other
    # source above.
    trail_geojson = park_trails.fetch_park_trails()
    trail_lines = _park_trails_for_tile(trail_geojson, bbox)
    trail_graph = _missing_park_trail_graph(graph, trail_lines, tile_id)
    if trail_graph is not None:
        if not barrier_fetched:
            barrier_graph = _fetch_with_retry(bbox, BARRIER_FILTER, tile_id, "barrier ways")
            barrier_fetched = True
        graph = _snap_interior_sidewalks(
            graph, trail_graph, barrier_graph, tile_id,
            snap_max_m=PARK_TRAIL_SNAP_MAX_M, label="park trails",
        )

    # Audit the welds this build just manufactured, BEFORE simplification
    # (scalar osmids, weld=True intact). Report-only -- see
    # pipeline/graph/vertical_audit.py's docstring for why it never
    # removes anything.
    vertical_audit.report_vertical_suspects(graph, any_sidewalk_graph, tile_id)

    graph = ox.simplification.simplify_graph(graph)

    # Record what this graph actually covers, so a later run can tell
    # whether the filename still means the same area (see _bbox_signature).
    graph.graph["fetch_bbox"] = wanted

    STREETS_DIR.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, graphml_path)
    print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (downloaded + cached)")
    return graph
