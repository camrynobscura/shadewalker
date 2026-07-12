"""Fetch the walkable street network for a tile from OpenStreetMap via osmnx.

osmnx downloads OSM data and hands it back already graph-shaped: a networkx
MultiDiGraph whose nodes are intersections (keyed by global OSM node ids) and
whose edges are street segments with geometry, length, and street names.

Caching is two-layer:
  1. osmnx's own HTTP cache (raw Overpass API responses) in data/raw/osmnx_cache
  2. our per-tile GraphML file in data/raw/streets/ — GraphML is a standard
     XML format for graphs; loading it back skips all network + assembly work.
"""

import networkx as nx
import osmnx as ox

from pipeline import config
from pipeline.config import Bbox

STREETS_DIR = config.RAW_DIR / "streets"

# Point osmnx's internal HTTP cache into our data/ tree so everything the
# pipeline ever downloads lives under one gitignored roof.
ox.settings.cache_folder = config.RAW_DIR / "osmnx_cache"


def fetch_streets(bbox: Bbox, tile_id: str) -> nx.MultiDiGraph:
    """Return the walkable street graph for the bbox, cached per tile."""
    graphml_path = STREETS_DIR / f"{tile_id}.graphml"

    if graphml_path.exists():
        graph = ox.load_graphml(graphml_path)
        print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (cached)")
        return graph

    # osmnx bbox order is (left, bottom, right, top) = (west, south, east, north).
    # network_type="walk" keeps ways pedestrians can use (streets, paths, steps)
    # and drops car-only infrastructure like highways.
    graph = ox.graph_from_bbox(
        bbox=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
        network_type="walk",
    )

    STREETS_DIR.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, graphml_path)
    print(f"  [streets] {tile_id}: {len(graph.nodes)} nodes, {len(graph.edges)} edges (downloaded + cached)")
    return graph
