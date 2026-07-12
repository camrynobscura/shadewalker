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

import geopandas as gpd
import networkx as nx
import osmnx as ox

METRIC_CRS = "EPSG:32618"  # UTM 18N — meter units, accurate for NYC


def build_edge_table(street_graph: nx.MultiDiGraph) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return (nodes, edges) GeoDataFrames for routing.

    nodes: indexed by OSM node id, columns x (lon) / y (lat).
    edges: indexed by (u, v, key), with length_m, name,
           geometry (lat/lon) and geometry_m (meters).
    """
    undirected = ox.convert.to_undirected(street_graph)

    # graph_to_gdfs unpacks a graph into two table-like GeoDataFrames.
    # fill_edge_geometry gives straight-line geometry to edges OSM stored
    # without an explicit shape.
    nodes, edges = ox.graph_to_gdfs(undirected, fill_edge_geometry=True)

    # Reproject a *copy* of the edge shapes into meters and keep both.
    edges["geometry_m"] = edges["geometry"].to_crs(METRIC_CRS)

    # osmnx precomputes each edge's real length in meters (even though the
    # geometry column is in degrees) — we just rename it to carry its unit.
    edges["length_m"] = edges["length"].round(1)

    edges["name"] = edges["name"].apply(_normalize_name) if "name" in edges.columns else ""

    # Keep only what downstream stages use.
    edges = edges[["length_m", "name", "geometry", "geometry_m"]]

    print(f"  [graph] {len(nodes)} nodes, {len(edges)} undirected edges")
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
