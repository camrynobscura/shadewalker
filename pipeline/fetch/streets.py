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

import time

import networkx as nx
import osmnx as ox
import requests

from pipeline import config
from pipeline.config import Bbox

STREETS_DIR = config.RAW_DIR / "streets"

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
# sidewalks) -- it's baked into the cache filename, so old cached graphs
# are ignored rather than silently reused.
GRAPH_CACHE_VERSION = 8

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
    # street/path types a pedestrian can use — note no motorways/trunks
    '["highway"~"primary|primary_link|secondary|secondary_link|tertiary|tertiary_link'
    '|unclassified|residential|living_street|pedestrian|footway|path|steps|service"]'
    '["area"!~"yes"]'                                  # skip plaza *areas* (not lines)
    '["foot"!~"no"]'                                   # explicitly closed to pedestrians
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
# cycleway at all, since most cycleways are bike-only. Scoped tightly to
# foot=designated (explicitly shared-use) rather than broadening
# WALK_FILTER's own list, so ordinary bike-only cycleways stay excluded.
CYCLEWAY_FILTER = '["highway"="cycleway"]["foot"="designated"]'

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
FOOT_OVERRIDES_ACCESS_FILTER = (
    '["highway"~"primary|primary_link|secondary|secondary_link|tertiary|tertiary_link'
    '|unclassified|residential|living_street|pedestrian|footway|path|steps|service"]'
    '["area"!~"yes"]'
    '["foot"~"designated|yes"]'
    '["access"~"private|no"]'
    '["footway"!~"sidewalk"]'
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

# Point osmnx's internal HTTP cache into our data/ tree so everything the
# pipeline ever downloads lives under one gitignored roof.
ox.settings.cache_folder = config.RAW_DIR / "osmnx_cache"

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


def fetch_streets(bbox: Bbox, tile_id: str) -> nx.MultiDiGraph | None:
    """Return the walkable street graph for the bbox, cached per tile --
    the union of WALK_FILTER's main centerline query, CYCLEWAY_FILTER's
    narrower foot=designated-cycleway query,
    FOOT_OVERRIDES_ACCESS_FILTER's access=no/private-but-foot-designated
    query, and NAMED_SIDEWALK_FILTER's named-park-path query (see all
    four constants' comments).

    Each of the four is fetched unsimplified (see _fetch_with_retry's
    simplify=False comment for why) and composed into one graph BEFORE
    simplifying -- once, here -- so osmnx's topology simplification sees
    every filter's ways together and can correctly tell a real
    intersection between two different filters' matches apart from a
    genuine dead end, instead of each filter's own simplification pass
    guessing from an incomplete picture.

    None means the bbox has no OSM ways matching WALK_FILTER at all -- real
    for grid tiles that only clip a borough's real coastline at their
    edge (a tile can intersect a borough's polygon by a sliver that's
    still mostly open water; see PLAN.md), not a bug to retry.
    """
    graphml_path = STREETS_DIR / f"{tile_id}_v{GRAPH_CACHE_VERSION}.graphml"

    if graphml_path.exists():
        graph = ox.load_graphml(graphml_path)
        print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (cached)")
        return graph

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

    graph = ox.simplification.simplify_graph(graph)

    STREETS_DIR.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, graphml_path)
    print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (downloaded + cached)")
    return graph
