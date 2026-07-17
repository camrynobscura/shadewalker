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

import shapely
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


def nyc_boundary(geojson: dict) -> BaseGeometry:
    """The union of every borough's real polygon (water areas already
    excluded by the source dataset). Pure function over the raw GeoJSON
    FeatureCollection -- see pipeline/fetch/boundaries.py for the fetch."""
    borough_shapes = [shape(feature["geometry"]) for feature in geojson["features"]]
    return shapely.union_all(borough_shapes)
