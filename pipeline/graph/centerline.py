"""Turn the raw OSM street graph into our edge table.

Two transformations happen here, both with important "why"s:

1. UNDIRECTED. osmnx returns a directed graph — every street appears twice,
   once per driving direction. Walkers don't care about one-way streets, so
   we collapse to undirected, halving the edge count.

2. TWO COORDINATE SYSTEMS, kept side by side. Raw OSM coordinates are
   lat/lon degrees (CRS "EPSG:4326") — right for drawing on a web map,
   useless for measuring, because 1° of longitude ≠ 1° of latitude in
   meters. So we also project every edge into a meter-based CRS
   (UTM zone 18N, "EPSG:32618", which is accurate around NYC) for all
   buffering/measuring. The #1 bug class in geospatial code is doing math
   in degrees or drawing in meters; keeping both geometries explicitly
   named (`geometry` = lat/lon, `geometry_m` = meters) makes each use
   grab the right one.
"""

import logging
import geopandas as gpd
import networkx as nx
import osmnx as ox

logger = logging.getLogger(__name__)


METRIC_CRS = "EPSG:32618"  # UTM 18N — meter units, accurate for NYC


def _require_osmid_on_every_edge(street_graph: nx.MultiDiGraph) -> None:
    """Fail fast, with a specific error, if any edge lacks an osmid.

    Real bug (2026-08-09): pipeline.fetch.streets's interior-sidewalk
    snapping (FIXES.md item 1a) was adding edges without one. osmnx's own
    to_undirected() only notices when it needs to compare two edges
    sharing a node pair (a true parallel edge, not an ordinary two-way
    street's forward/back pair) -- so it silently works most of the time
    and then crashes with a bare `KeyError: 'osmid'` deep in osmnx's own
    internals the moment a real tile happens to have one. Checking here
    instead, before that call, means a future mistake of the same shape
    (a new feature adding edges without osmid) fails immediately with a
    message naming the exact edge, not a cryptic trace through a third-
    party library.
    """
    for u, v, k, data in street_graph.edges(keys=True, data=True):
        if data.get("osmid") is None:
            raise ValueError(f"edge ({u!r}, {v!r}, key={k!r}) is missing 'osmid'")


def _require_node_ids_aligned(edges: gpd.GeoDataFrame) -> None:
    """Fail fast if node_ids is missing or not 1:1 with the geometry.

    node_ids (pipeline/fetch/streets.py's _annotate_node_ids, FIXES item
    13) is what lets the server rejoin cross-tile severed overlaps on node
    identity, and every downstream step assumes node_ids[i] names the node
    at geometry vertex i. A misaligned chain would split edges at the wrong
    place, so this is checked at build -- with the exact edge named -- not
    left to surface as a silent routing error at load.
    """
    if "node_ids" not in edges.columns:
        raise ValueError(
            "edge table has no 'node_ids' column -- rebuild the street graph "
            "(GRAPH_CACHE_VERSION must be >= 21)"
        )
    for idx, row in edges.iterrows():
        n_ids = len(row["node_ids"])
        n_pts = len(row["geometry"].coords)
        if n_ids != n_pts:
            raise ValueError(
                f"edge {idx!r}: node_ids has {n_ids} entries but geometry has "
                f"{n_pts} vertices -- they must align 1:1"
            )


def build_edge_table(street_graph: nx.MultiDiGraph) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (nodes, edges) GeoDataFrames for routing.

    nodes: indexed by OSM node id, columns x (lon) / y (lat).
    edges: indexed by (u, v, key), with length_m, name,
           geometry (lat/lon) and geometry_m (meters).
    """
    _require_osmid_on_every_edge(street_graph)
    undirected = ox.convert.to_undirected(street_graph)

    # graph_to_gdfs unpacks a graph into two table-like GeoDataFrames.
    # fill_edge_geometry gives straight-line geometry to edges OSM stored
    # without an explicit shape.
    nodes, edges = ox.graph_to_gdfs(undirected, fill_edge_geometry=True)

    # fill_edge_geometry just gave two-point edges a straight geometry, so
    # every edge now has coords to check node_ids against.
    _require_node_ids_aligned(edges)

    # Reproject a *copy* of the edge shapes into meters and keep both.
    edges["geometry_m"] = edges["geometry"].to_crs(METRIC_CRS)

    # osmnx precomputes each edge's real length in meters (even though the
    # geometry column is in degrees) — we just rename it to carry its unit.
    edges["length_m"] = edges["length"].round(1)

    edges["name"] = edges["name"].apply(_normalize_name) if "name" in edges.columns else ""

    # Keep only what downstream stages use. node_ids rides through to the
    # export so the server can reconcile severed overlaps (FIXES item 13).
    edges = edges[["length_m", "name", "geometry", "geometry_m", "node_ids"]]

    logger.info(f"  [graph] {len(nodes)} nodes, {len(edges)} undirected edges")
    return nodes, edges


def _normalize_name(raw) -> str:
    """OSM edge names can be a string, a list (merged ways), or missing (NaN).

    Flatten all cases to a plain string; unnamed paths become "".
    """
    if isinstance(raw, list):
        return str(raw[0])
    if isinstance(raw, str):
        return raw
    return ""  # NaN / None → unnamed (park paths, alleys)
