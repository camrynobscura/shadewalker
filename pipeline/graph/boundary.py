"""Turn NYC's real borough-boundary polygons into one shape: the union of
all five boroughs. Used to filter fetched OSM data down to genuine NYC
territory (see run_tile.py), replacing the old rectangle-overreach
approach -- a hand-picked bbox for Brooklyn swept in Jersey City, a
Manhattan sliver, and the Rockaways, which server/graph_store.py then had
to prune away with a blunt largest-component-only rule (see PLAN.md).

One deliberate design choice: this is a SINGLE unified polygon across all
five boroughs, not filtered per-borough at fetch time. A grid tile
straddling e.g. the Brooklyn/Queens line would otherwise fetch different
content depending on which borough's run_tile.py invocation triggered it,
breaking the per-tile GraphML cache's assumption that a tile's content
depends only on its own id, not on who asked for it.
"""

import networkx as nx
import shapely
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry

from pipeline import config


def nyc_boundary(geojson: dict) -> BaseGeometry:
    """The union of every borough's real polygon, including NYC's own
    water jurisdiction (a bridge's midspan needs to count as in-bounds --
    see pipeline/fetch/boundaries.py for why the water-included dataset
    is the right one here). Pure function over the raw GeoJSON
    FeatureCollection -- see pipeline/fetch/boundaries.py for the fetch."""
    borough_shapes = [shape(feature["geometry"]) for feature in geojson["features"]]
    return shapely.union_all(borough_shapes)


def borough_polygon(geojson: dict, borough: str) -> BaseGeometry:
    """One borough's own real polygon (not unioned with the others) --
    matched case-insensitively against the dataset's `boroname` (e.g.
    "manhattan" matches "Manhattan"), so it takes the same lowercase
    borough names config.BOROUGH_BBOXES's keys already use."""
    for feature in geojson["features"]:
        if feature["properties"]["boroname"].lower() == borough.lower():
            return shape(feature["geometry"])
    valid_names = sorted(feature["properties"]["boroname"] for feature in geojson["features"])
    raise ValueError(f"No borough named {borough!r} in the dataset -- valid names: {valid_names}")


def _tile_box(tile_id: str) -> BaseGeometry:
    bbox = config.get_tile_bbox(tile_id)
    return box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)


def tile_ids_for_polygon(polygon: BaseGeometry) -> list[str]:
    """Every citywide-grid tile id whose box genuinely intersects the
    polygon -- unlike config.get_tile_ids_for_bbox(), which only checks
    a rectangle. A cheap bounding-box pass finds the candidates first
    (reusing get_tile_ids_for_bbox on the polygon's own bounds), then
    each candidate's real tile box is checked against the actual polygon
    shape. For a shape as non-rectangular as Manhattan, this is the
    difference between the tiles that matter and dozens more swept in
    from NJ/Queens by the bounding rectangle alone."""
    lon_min, lat_min, lon_max, lat_max = polygon.bounds
    candidate_bbox = config.Bbox(lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max)
    candidates = config.get_tile_ids_for_bbox(candidate_bbox)
    return [tile_id for tile_id in candidates if _tile_box(tile_id).intersects(polygon)]


def clip_to_nyc(street_graph: nx.MultiDiGraph, nyc_shape: BaseGeometry) -> nx.MultiDiGraph:
    """Drop every node -- and its incident edges, via networkx's own
    cascade on removal -- that falls outside NYC's real boundary. This is
    what actually keeps foreign territory (Jersey City, Bayonne, swept in
    by a tile's buffered fetch bbox overreaching past the real coastline)
    out of the pipeline's data, rather than leaving
    server/graph_store.py's load-time pruning to catch it after the fact
    (see PLAN.md's borough-boundary polygon plan). Does not mutate
    street_graph -- works on a copy, matching centerline.py's own
    non-mutating convention.

    Pure containment check in raw lon/lat degrees (osmnx's raw node
    attributes, `x` = lon / `y` = lat) -- no measuring involved, so no
    need for the meters-based CRS centerline.py uses for buffering."""
    outside_nyc = [
        node for node, data in street_graph.nodes(data=True)
        if not nyc_shape.contains(Point(data["x"], data["y"]))
    ]
    clipped = street_graph.copy()
    clipped.remove_nodes_from(outside_nyc)
    print(f"  [boundary] dropped {len(outside_nyc)}/{street_graph.number_of_nodes()} nodes outside NYC")
    return clipped
