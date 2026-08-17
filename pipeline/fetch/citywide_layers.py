"""One-shot citywide Overpass layer fetches (FIXES item 6b).

Measured across the v19 refetch (278 fresh tile fetches, 714 minutes of
total Overpass wait): every Overpass query costs ~23-30s regardless of
result size -- per-request overhead dominates -- and each tile made 7.
The six auxiliary queries were 78.4% of all fetch wait while returning
tiny results: measured citywide 2026-08-15, the whole city has only
1,666 foot-designated cycleway ways, 195 foot-override ways, 1,832 named
sidewalk-path ways, 28,839 parking-aisle ways, and 14,619 barrier ways;
even the biggest layer (154,678 any-sidewalk ways / 604,573 nodes) fits
comfortably in one response. So each auxiliary layer is fetched ONCE for
the whole city here, cached as GraphML, and every tile slices its own
bbox out in memory (streets._aux_layer_graph) -- the same division of
labor as interior_sidewalks' and park_trails' citywide caches. A full
citywide refetch drops from ~13.5h to ~4h.

The main WALK_FILTER query deliberately stays per-tile in streets.py:
it's the one query whose result is actually tile-sized (the whole city's
walkable street network in one response would dwarf every layer above).

Cache keying (FIXES item 11, 2026-08-17): every raw Overpass cache --
these six layers AND streets.py's per-tile raw WALK_FILTER snapshots --
is keyed on raw_fetch_key(), a hash of the only things that change what
Overpass hands back (the filter string, which way tags osmnx retains,
and the osmnx version whose truncation semantics baked the graph).
Deliberately NOT keyed on streets.GRAPH_CACHE_VERSION: that number now
means "how the raw data is processed," and bumping it for a
processing-only change must rebuild from these snapshots instead of
re-downloading the city (the v20 refetch re-downloaded ~5h of identical
data for a purely-local weld pass). A filter edit still invalidates
exactly the caches it affects, because the filter string is in the key.

Freshness: a cached layer is as old as the last time its file was
fetched. Pulling fresh OSM is an explicit action now -- run_tile's
--refresh-raw -- never a side effect of a logic change, so before/after
route comparisons only ever move one variable at a time. Within one
process a refresh only re-asks Overpass once per layer (_FRESHENED), or
a borough run would re-download every layer per tile.
"""

import hashlib
import time
from pathlib import Path

import networkx as nx
import osmnx as ox
import requests

from pipeline import config
from pipeline.config import Bbox

CACHE_DIR = config.RAW_DIR / "citywide_layers"

# The citywide fetch bbox is padded by the same FETCH_BUFFER_M each
# tile's own fetch bbox is padded by (run_tile.py) -- an edge tile's
# padded bbox extends up to FETCH_BUFFER_M past CITY_BBOX itself, and a
# slice can only hand back nodes the citywide fetch actually covered, so
# fetching exactly CITY_BBOX would silently shave the city's outermost
# fringe off every edge tile.
def _layer_fetch_bbox() -> Bbox:
    return config.buffered_bbox(config.CITY_BBOX, config.FETCH_BUFFER_M)


# The biggest layer (any-sidewalks, ~600k nodes) takes minutes of server
# time, not the ~180s osmnx defaults assume for a tile-sized query --
# raised only around the citywide fetch, then restored.
LAYER_FETCH_TIMEOUT_S = 600

# Same transient-connection-flakiness handling as streets.py's per-tile
# fetch (see streets.MAX_FETCH_RETRIES's comment for the observed
# failures) -- a citywide layer query holds a connection open longer, so
# it's MORE exposed to mid-response drops, not less.
MAX_FETCH_RETRIES = 3
FETCH_RETRY_BACKOFF_S = 5

# One graph per layer name per process: a borough run calls
# streets.fetch_streets() up to 155 times, and re-parsing a 600k-node
# GraphML from disk per tile would eat the fetch savings this module
# exists for.
_MEMO: dict[str, nx.MultiDiGraph | None] = {}

# Layers this process has already pulled fresh from Overpass. refresh=True
# means "this RUN wants fresh OSM data," not "every call does" -- without
# this, a --refresh-raw borough run would re-download every layer once per
# tile, exactly the per-tile repetition item 6b removed.
_FRESHENED: set[str] = set()


def raw_fetch_key(custom_filter: str) -> str:
    """A short stable key identifying what a raw Overpass fetch would
    return -- the cache key for every raw (unprocessed) snapshot, both
    the citywide layers here and streets.py's per-tile WALK_FILTER ones.

    Three ingredients, because exactly three things change the graph
    osmnx hands back for a given bbox: the Overpass filter string itself,
    ox.settings.useful_tags_way (which way tags survive onto the edges --
    v19 added `layer` this way, a real content change with no filter
    edit), and the osmnx version (its bbox-truncation semantics are baked
    into the saved graph; an upgrade must invalidate rather than mix two
    versions' semantics in one cache). The bbox is deliberately NOT in
    the key -- it's recorded inside each file and re-checked on load
    (_bbox_signature), the discipline that caught the r17c14 grid shift.

    Read at call time, not import time: streets.py appends to
    useful_tags_way when it's imported, and hashing before that would key
    on a settings state no fetch actually runs under.
    """
    material = "|".join([
        custom_filter,
        ",".join(sorted(ox.settings.useful_tags_way)),
        ox.__version__,
    ])
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def _bbox_signature(bbox: Bbox) -> str:
    """Same format and same reasoning as streets._bbox_signature: the
    cache FILENAME can't be trusted to still mean the same area after a
    grid/bbox redefinition, so the real fetched bbox is recorded inside
    the GraphML and re-checked on load."""
    return (f"{bbox.lat_min:.6f},{bbox.lat_max:.6f},"
            f"{bbox.lon_min:.6f},{bbox.lon_max:.6f}")


def _fetch_citywide(custom_filter: str, name: str) -> nx.MultiDiGraph | None:
    """One citywide Overpass fetch via osmnx, unsimplified and
    retain_all=True for exactly the reasons streets._fetch_with_retry
    documents (per-filter simplification mishandles cross-filter
    junctions; per-area component pruning deletes real neighborhoods) --
    the slices this graph feeds must be drop-in identical to the per-tile
    queries they replaced. None means the filter matched nothing citywide
    (surfaced by osmnx as a ValueError, same as per-tile).

    osmnx's request timeout is raised for the duration of this one call
    and restored afterward -- per-tile fetches elsewhere in the same
    process keep the default.
    """
    bbox = _layer_fetch_bbox()
    saved_timeout = ox.settings.requests_timeout
    ox.settings.requests_timeout = LAYER_FETCH_TIMEOUT_S
    try:
        for attempt in range(1, MAX_FETCH_RETRIES + 1):
            started = time.monotonic()
            try:
                graph = ox.graph_from_bbox(
                    bbox=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
                    custom_filter=custom_filter,
                    retain_all=True,
                    simplify=False,
                )
                print(f"  [citywide_layers] {name}: {time.monotonic() - started:.1f}s, "
                      f"{len(graph.nodes)} nodes, {len(graph.edges)} edges (downloaded)")
                return graph
            except ValueError:
                print(f"  [citywide_layers] {name}: {time.monotonic() - started:.1f}s, "
                      f"nothing matched citywide")
                return None
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError):
                if attempt == MAX_FETCH_RETRIES:
                    raise
                wait_s = FETCH_RETRY_BACKOFF_S * (2 ** (attempt - 1))
                print(f"  [citywide_layers] {name}: connection error "
                      f"(attempt {attempt}/{MAX_FETCH_RETRIES}), retrying in {wait_s}s...")
                time.sleep(wait_s)
    finally:
        ox.settings.requests_timeout = saved_timeout
    return None


def citywide_layer(
    name: str, custom_filter: str, refresh: bool = False
) -> nx.MultiDiGraph | None:
    """The citywide graph for one auxiliary layer, memoized per process
    and cached on disk as GraphML.

    The cache is keyed on raw_fetch_key(custom_filter) -- see its
    docstring, and the module docstring's FIXES item 11 paragraph, for
    why it's the filter's own identity rather than
    streets.GRAPH_CACHE_VERSION: a filter change still can never
    silently reuse a layer fetched under the old definition (the filter
    string is in the key), while a processing-only version bump no
    longer throws away raw data that would come back byte-identical.

    refresh=True pulls fresh from Overpass -- once per layer per process
    (see _FRESHENED), so a --refresh-raw borough run doesn't repeat the
    download per tile.

    An empty layer (None) is memoized and cached too -- via a marker
    graph on disk, since "the filter matched nothing citywide" is a real
    answer, not a failure to get one.
    """
    refresh = refresh and name not in _FRESHENED
    if not refresh and name in _MEMO:
        return _MEMO[name]

    cache_path = CACHE_DIR / f"{name}_{raw_fetch_key(custom_filter)}.graphml"
    wanted = _bbox_signature(_layer_fetch_bbox())

    if cache_path.exists() and not refresh:
        graph = ox.load_graphml(cache_path)
        if graph.graph.get("fetch_bbox") == wanted:
            if graph.graph.get("empty_layer"):
                _MEMO[name] = None
                print(f"  [citywide_layers] {name}: empty citywide (cached)")
                return None
            _MEMO[name] = graph
            print(f"  [citywide_layers] {name}: {len(graph.nodes)} nodes, "
                  f"{len(graph.edges)} edges (cached)")
            return graph
        print(f"  [citywide_layers] {name}: cached layer covers "
              f"{graph.graph.get('fetch_bbox') or 'an unrecorded area'}, not {wanted} "
              f"-- re-fetching")

    graph = _fetch_citywide(custom_filter, name)
    _FRESHENED.add(name)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if graph is None:
        # A marker file, so "nothing matched citywide" doesn't re-ask
        # Overpass on every process start. Real for no current filter,
        # but cheap insurance against a future very narrow one.
        marker = nx.MultiDiGraph(crs="epsg:4326")
        marker.graph["fetch_bbox"] = wanted
        marker.graph["empty_layer"] = True
        ox.save_graphml(marker, cache_path)
    else:
        graph.graph["fetch_bbox"] = wanted
        ox.save_graphml(graph, cache_path)

    _MEMO[name] = graph
    return graph
