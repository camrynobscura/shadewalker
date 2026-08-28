"""Turn NYC's real borough-boundary polygons into one shape: the union of
all five boroughs. pipeline/graph/pedestrian.py clips the pinned OSM
extract against it way-by-way (the statewide file reaches Buffalo), and
the park-canopy scoring builds its park mask from park_polygon() below.
Real polygons, not rectangles, on purpose: a hand-picked bbox for
Brooklyn once swept in Jersey City, a Manhattan sliver, and the
Rockaways, which server/graph_store.py then had to prune away with a
blunt largest-component-only rule.

The tile-grid helpers (_tile_box, tile_ids_for_polygon) and the
centerline-era clip_to_nyc were deleted 2026-08-28 with the tile grid
itself (PLAN's `citywide-guards`); by then nothing but their own tests
called them.
"""

import shapely
from shapely.geometry import shape
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
    borough names run_tile.py's CLI argument already uses."""
    for feature in geojson["features"]:
        if feature["properties"]["boroname"].lower() == borough.lower():
            return shape(feature["geometry"])
    valid_names = sorted(feature["properties"]["boroname"] for feature in geojson["features"])
    raise ValueError(f"No borough named {borough!r} in the dataset -- valid names: {valid_names}")


def _is_real_park(properties: dict) -> bool:
    """Whether a single Parks Properties feature is real, walkable park
    land -- see config.PARK_EXCLUDED_TYPECATEGORIES and its neighboring
    constants for the two-tier reasoning (FIXES.md item 1d). A
    typecategory alone can't tell real park land apart from genuinely
    non-park land filed under the same label, but checking `subcategory`
    everywhere is equally wrong (it would rescue Belt Parkway's median as
    "Large Park") -- so the per-property check only applies to the three
    typecategories where a real bundling error was actually found."""
    typecategory = properties.get("typecategory")
    if typecategory in config.PARK_EXCLUDED_TYPECATEGORIES:
        return False
    if typecategory in config.PARK_TYPECATEGORIES_NEEDING_SUBCATEGORY_CHECK:
        return properties.get("subcategory") in config.PARK_LIKE_SUBCATEGORIES
    return True


def park_polygon(geojson: dict) -> BaseGeometry:
    """The union of every real park's polygon -- excludes roadside/median
    slivers and non-park land (_is_real_park(), config.
    PARK_EXCLUDED_TYPECATEGORIES) so the park canopy mask only covers
    actual park interiors, not traffic triangles or parking lots.
    Cemeteries are included (as of 2026-07-31): no external routing engine
    special-cases them, so keeping them out was a data gap, not a real
    distinction. Pure function over the raw GeoJSON FeatureCollection --
    see pipeline/fetch/parks.py for the fetch.

    Repairs each feature with shapely.make_valid() before unioning -- 9 of
    the real dataset's polygons (Flagship Parks down to a Nature Area,
    including John V. Lindsay East River Park) have self-intersecting
    rings, a genuine defect in NYC's own GIS data, not a fetch/parsing
    bug. A SECOND make_valid() runs on the union_all() result too:
    confirmed live (citywide park-canopy re-score, see PLAN.md) that
    unioning ~1,500 individually-valid polygons can still produce an
    is_valid=False result -- floating-point robustness in the union
    algorithm itself, not a leftover input defect -- and a later
    .intersection() against that invalid union hit GEOS's
    'TopologyException: side location conflict' near East River Park.
    Validating only the inputs was NOT sufficient; the output needs its
    own check.
    """
    park_shapes = [
        shapely.make_valid(shape(feature["geometry"]))
        for feature in geojson["features"]
        if _is_real_park(feature["properties"])
    ]
    return shapely.make_valid(shapely.union_all(park_shapes))
