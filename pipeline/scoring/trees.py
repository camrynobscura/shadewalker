"""Score trees and attach them to street edges.

Two steps:

1. VALUE EACH TREE: health × size, from the plan's formula
       tree_value = condition_score × min(dbh, 30) / 30
   split into deciduous vs evergreen columns (by genus) so routing can apply
   the monthly canopy factor to the deciduous part only.

2. JOIN TREES TO STREETS: draw a 12 m corridor around every street edge
   (buffering, in the meter-based CRS) and spatially join: each tree lands in
   the corridor(s) containing it. Summing per edge gives the street's totals.
   A tree between two corridors counts toward both — acceptable, since it
   really does shade both.
"""

import geopandas as gpd
from shapely.geometry import Point

from pipeline import config
from pipeline.graph.centerline import METRIC_CRS


def score_and_join(edges: gpd.GeoDataFrame, tree_rows: list[dict]) -> gpd.GeoDataFrame:
    """Return `edges` with tree_deciduous, tree_evergreen, tree_count columns."""
    trees = _build_tree_table(tree_rows)

    # Buffer: each street's line geometry grows into a 12m-wide polygon
    # corridor. Done on geometry_m (meters) — buffering in degrees is the
    # classic geospatial bug (12 "degrees" would swallow the East Coast).
    corridors = gpd.GeoDataFrame(
        geometry=edges["geometry_m"].buffer(config.TREE_BUFFER_M).values,
        crs=METRIC_CRS,
    )  # fresh 0..N-1 index, one row per edge, in edge order

    # sjoin = spatial join: like a SQL JOIN, but the ON-condition is geometric
    # ("point is inside polygon") instead of key equality. Returns one row per
    # (tree, corridor) containment pair; index_right tells us which corridor.
    joined = gpd.sjoin(trees, corridors, how="inner", predicate="within")

    # groupby-sum per corridor (SQL: GROUP BY corridor), then align the
    # aggregates back onto the edge table by position; edges with no trees
    # get NaN from reindex, which fillna(0) turns into a clean zero.
    per_edge = joined.groupby("index_right").agg(
        tree_deciduous=("value_deciduous", "sum"),
        tree_evergreen=("value_evergreen", "sum"),
        tree_count=("value_deciduous", "size"),  # any column works for a row count
    )
    per_edge = per_edge.reindex(range(len(edges))).fillna(0)

    edges = edges.copy()
    edges["tree_deciduous"] = per_edge["tree_deciduous"].round(3).values
    edges["tree_evergreen"] = per_edge["tree_evergreen"].round(3).values
    edges["tree_count"] = per_edge["tree_count"].astype(int).values

    with_trees = (edges["tree_count"] > 0).sum()
    print(f"  [scoring] {len(trees)} scoreable trees → "
          f"{with_trees} of {len(edges)} edges have trees")
    return edges


def _build_tree_table(tree_rows: list[dict]) -> gpd.GeoDataFrame:
    """Raw API dicts → GeoDataFrame with a point geometry and value columns."""
    records = []
    for row in tree_rows:
        condition = row.get("tpcondition", "Unknown")
        if condition == "Dead":
            continue  # standing dead trees give no shade

        # dbh arrives as a string ("22"); missing/garbage becomes 0 (no value).
        try:
            dbh = float(row.get("dbh", 0))
        except ValueError:
            dbh = 0.0

        condition_score = config.CONDITION_SCORES.get(condition, config.CONDITION_DEFAULT)
        value = condition_score * min(dbh, config.DBH_CAP_IN) / config.DBH_CAP_IN
        if value == 0:
            continue  # no dbh recorded → nothing to contribute

        # genusspecies looks like "Quercus bicolor - swamp white oak";
        # the genus is the first word. Unknown species default to deciduous
        # (the overwhelmingly right guess for NYC streets).
        genus = row.get("genusspecies", "").split(" ")[0]
        is_evergreen = genus in config.EVERGREEN_GENERA

        lon, lat = row["location"]["coordinates"]
        records.append({
            "value_deciduous": 0.0 if is_evergreen else value,
            "value_evergreen": value if is_evergreen else 0.0,
            "geometry": Point(lon, lat),
        })

    if not records:
        # Every row got filtered out above (all Dead, or none with a usable
        # dbh) -- gpd.GeoDataFrame([], crs=...) has no "geometry" column at
        # all in that case and raises, so build the empty table explicitly
        # instead of letting score_and_join crash on a tile/edge with zero
        # scoreable trees.
        return gpd.GeoDataFrame(
            {"value_deciduous": [], "value_evergreen": [], "geometry": []},
            crs=METRIC_CRS,
        )

    trees = gpd.GeoDataFrame(records, crs="EPSG:4326")  # raw coords are lat/lon
    return trees.to_crs(METRIC_CRS)  # → meters, to match the buffered corridors
