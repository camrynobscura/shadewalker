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
walkable *streets* + standalone park/plaza paths, minus the sidewalk and
crossing fragments. One edge per block, carrying its street name.

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
# graph_from_bbox call) -- it's baked into the cache filename, so old
# cached graphs are ignored rather than silently reused.
GRAPH_CACHE_VERSION = 4

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
    '["footway"!~"sidewalk|crossing"]'                 # the separately-mapped sidewalk
                                                       # fragments (unnamed) — we model
                                                       # streets as centerlines instead
)

# Point osmnx's internal HTTP cache into our data/ tree so everything the
# pipeline ever downloads lives under one gitignored roof.
ox.settings.cache_folder = config.RAW_DIR / "osmnx_cache"


def fetch_streets(bbox: Bbox, tile_id: str) -> nx.MultiDiGraph | None:
    """Return the walkable street graph for the bbox, cached per tile.

    None means the bbox has no OSM ways matching WALK_FILTER at all -- real
    for grid tiles that land mostly on open water (BROOKLYN_BBOX is a
    rectangle, so it overreaches past the real coastline at its edges; see
    PLAN.md), not a bug to retry.
    """
    graphml_path = STREETS_DIR / f"{tile_id}_v{GRAPH_CACHE_VERSION}.graphml"

    if graphml_path.exists():
        graph = ox.load_graphml(graphml_path)
        print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (cached)")
        return graph

    # osmnx bbox order is (left, bottom, right, top) = (west, south, east, north).
    #
    # retain_all=True, NOT False, and the distinction cost a real
    # neighborhood: retain_all=False keeps only the largest connected
    # component -- decided per tile, on an internal working graph that
    # extends ~500m past the tile, BEFORE the final clip -- so a
    # neighborhood that reads as "disconnected" through one tile's
    # peephole gets deleted at fetch time even when it connects fine
    # through a neighboring tile's streets. Red Hook (walled off by the
    # expressway trench + water on three sides) lost that contest in
    # every tile that saw it and vanished from the data entirely.
    # Keeping everything per tile is safe because the server prunes
    # globally at load time (see graph_store.load()), with the whole
    # merged picture in view -- that's the right scope for the
    # keep-or-drop decision, and it's also what still protects the
    # router from stray fragments (the original reason this was False).
    #
    # "No data here" surfaces as a plain ValueError from osmnx, not one
    # consistent exception type -- observed two different real messages from
    # two different internal code paths ("No data elements in server
    # response" when Overpass itself returns nothing, "Found no graph nodes
    # within the requested polygon" when Overpass returns something just
    # outside the tile's exact edge but nothing survives clipping to it).
    # Catching the shared ValueError base rather than either specific
    # message/subclass is deliberate: bbox is already validated by
    # get_tile_bbox() before this call, so any ValueError from osmnx here is
    # effectively guaranteed to mean "empty area," not a real bug -- and a
    # narrower catch would just mean discovering a third message the hard
    # way, mid-borough-run, again.
    #
    # ConnectionError gets its own, separate handling (retry, not skip) --
    # unlike ValueError, it says nothing about whether this tile has data,
    # only that this one attempt to ask didn't reach the server.
    graph = None
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            graph = ox.graph_from_bbox(
                bbox=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
                custom_filter=WALK_FILTER,
                retain_all=True,
            )
            break
        except ValueError:
            print(f"  [streets] {tile_id}: no matching ways in this area (likely open water) -- skipping")
            return None
        except requests.exceptions.ConnectionError:
            if attempt == MAX_FETCH_RETRIES:
                raise
            wait_s = FETCH_RETRY_BACKOFF_S * (2 ** (attempt - 1))
            print(f"  [streets] {tile_id}: connection error (attempt {attempt}/{MAX_FETCH_RETRIES}), "
                  f"retrying in {wait_s}s...")
            time.sleep(wait_s)

    STREETS_DIR.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, graphml_path)
    print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (downloaded + cached)")
    return graph
