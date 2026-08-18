"""Score trees and attach them to street edges.

Three steps:

1. VALUE EACH TREE: health × size, from the plan's formula
       tree_value = condition_score × min(dbh, 30) / 30
   split into deciduous vs evergreen columns (by genus) so routing can apply
   the monthly canopy factor to the deciduous part only.

2. JOIN TREES TO STREETS: draw a TREE_BUFFER_M corridor around every street
   edge (buffering, in the meter-based CRS) and spatially join: each tree
   lands in the corridor(s) containing it. Summing per edge gives the
   street's totals. A tree between two corridors counts toward both —
   acceptable when the corridors are genuinely different streets, since it
   really does shade both.

3. EXCEPT BETWEEN SIBLING CARRIAGEWAYS: a divided boulevard (Park Avenue,
   Queens Boulevard, Grand Concourse...) is two parallel same-named edges
   11–18m apart, so every median tree lands in both corridors and its full
   value gets booked twice — measured at 19% of those streets' tree value
   citywide, quietly inflating grand boulevards over ordinary streets. A
   tree shared between edges of one sibling group counts only toward the
   nearest one. (See PLAN.md's 2026-07-22/23 scoring investigation.)
"""

import logging
import math

import geopandas as gpd
from shapely.geometry import Point
from shapely.strtree import STRtree

from pipeline import config
from pipeline.graph.centerline import METRIC_CRS

logger = logging.getLogger(__name__)


# ── Sibling-carriageway detection thresholds ──────────────────────────────
# Validated citywide against the loaded graph (4,201 genuine pairs found;
# spot-checked against real medians): two same-named edges are the two
# sides of one divided street when they run near-parallel, close together,
# alongside each other for a real stretch, and don't simply share an
# endpoint (which would make them consecutive blocks of one street).
SIBLING_MAX_SEPARATION_M = 25.0   # real median separations cluster at 10-18m
SIBLING_MAX_BEARING_DIFF_DEG = 20.0
SIBLING_MIN_OVERLAP_FRACTION = 0.5  # of the shorter edge's along-track span
SIBLING_MIN_LENGTH_M = 15.0       # tiny stub edges match too noisily
SIBLING_MAX_LENGTH_RATIO = 3.0    # a 20m stub isn't a 200m block's sibling


def score_and_join(edges: gpd.GeoDataFrame, tree_rows: list[dict]) -> gpd.GeoDataFrame:
    """Return `edges` with tree_deciduous, tree_evergreen, tree_count columns."""
    trees = _build_tree_table(tree_rows)

    # Buffer: each street's line geometry grows into a TREE_BUFFER_M-wide
    # polygon corridor. Done on geometry_m (meters) — buffering in degrees
    # is the classic geospatial bug (14 "degrees" would swallow the East
    # Coast).
    corridors = gpd.GeoDataFrame(
        geometry=edges["geometry_m"].buffer(config.TREE_BUFFER_M).values,
        crs=METRIC_CRS,
    )  # fresh 0..N-1 index, one row per edge, in edge order

    # sjoin = spatial join: like a SQL JOIN, but the ON-condition is geometric
    # ("point is inside polygon") instead of key equality. Returns one row per
    # (tree, corridor) containment pair; index_right tells us which corridor.
    joined = gpd.sjoin(trees, corridors, how="inner", predicate="within")

    joined = _drop_far_sibling_credit(joined, edges, trees)

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
    # The park-canopy credit an edge later receives (apply_park_canopy
    # adds it into tree_deciduous AND records it here) -- kept as its own
    # column so the export can say which edges' shade comes from canopy
    # area rather than countable trees (FIXES.md item 4's option A needs
    # exactly this; the pipeline used to sum-and-forget the split).
    # Initialized here, where every edge table is born, so the pilot/CI
    # path (no canopy raster) exports a clean 0.0 rather than a missing
    # column.
    edges["tree_park_canopy"] = 0.0

    with_trees = (edges["tree_count"] > 0).sum()
    logger.info(f"  [scoring] {len(trees)} scoreable trees → "
          f"{with_trees} of {len(edges)} edges have trees")
    return edges


def _drop_far_sibling_credit(
    joined: gpd.GeoDataFrame, edges: gpd.GeoDataFrame, trees: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Step 3 of the module docstring: where a tree fell into the corridors
    of two-or-more edges of the SAME sibling group (the carriageways of one
    divided street), keep only its row for the nearest edge.

    Groups must be transitive, not pairwise — Queens Boulevard-style streets
    run a main roadway plus service roads, up to 7 near-parallel same-named
    edges abreast (511 of the 3,393 groups found citywide have 3+ members),
    and a tree in the middle can sit in several corridors at once.
    """
    group_of_edge = _sibling_groups(edges)
    if not group_of_edge or joined.empty:
        return joined

    lines = edges["geometry_m"].to_list()

    # Work per (tree, group): a tree appearing in one group's corridors
    # 2+ times keeps only its nearest row. Rows outside any group — and a
    # tree's rows in two DIFFERENT groups (a real corner between two
    # distinct divided streets) — are left exactly as they were.
    drop_labels = []
    tree_and_group = [
        (tree_idx, group_of_edge.get(edge_pos))
        for tree_idx, edge_pos in zip(joined.index, joined["index_right"])
    ]
    rows_by_tree_and_group: dict[tuple, list[int]] = {}
    for row_pos, (tree_idx, group_id) in enumerate(tree_and_group):
        if group_id is None:
            continue
        rows_by_tree_and_group.setdefault((tree_idx, group_id), []).append(row_pos)

    for (tree_idx, _group_id), row_positions in rows_by_tree_and_group.items():
        if len(row_positions) < 2:
            continue
        tree_point = trees.geometry.loc[tree_idx]
        nearest_row = min(
            row_positions,
            key=lambda row_pos: lines[joined["index_right"].iloc[row_pos]].distance(tree_point),
        )
        drop_labels.extend(row_pos for row_pos in row_positions if row_pos != nearest_row)

    if not drop_labels:
        return joined
    dropped = set(drop_labels)
    keep = [row_pos for row_pos in range(len(joined)) if row_pos not in dropped]
    return joined.iloc[keep]


def _sibling_groups(edges: gpd.GeoDataFrame) -> dict[int, int]:
    """Detect divided-street sibling carriageways: same non-empty name,
    near-parallel, close, running alongside each other, and not just two
    consecutive blocks of one street (those share an endpoint node).

    Returns {edge position -> group id} for grouped edges only; positions
    match the fresh 0..N-1 order score_and_join's corridors (and therefore
    sjoin's index_right) use. Empty dict when the table can't have siblings
    (no name column — some tests score bare geometry-only tables).
    """
    if "name" not in edges.columns:
        return {}

    names = edges["name"].fillna("").to_list()
    lines = edges["geometry_m"].to_list()
    lengths = [line.length for line in lines]

    # Node ids, for the shared-endpoint exclusion. The real edge table is
    # indexed by (u, v, key); anything else (plain test tables) gets
    # per-edge unique placeholders, which simply disables the exclusion.
    if edges.index.nlevels >= 2:
        node_pairs = [(index_value[0], index_value[1]) for index_value in edges.index]
    else:
        node_pairs = [(f"u{position}", f"v{position}") for position in range(len(edges))]

    # Candidate pairs: real line-to-line distance within the sibling
    # separation — a bulk STRtree query, in meters. Returns (left, right)
    # position pairs including self-pairs and both orders; filtered below.
    candidate_positions = [
        position for position in range(len(edges))
        if names[position] and lengths[position] >= SIBLING_MIN_LENGTH_M
    ]
    if len(candidate_positions) < 2:
        return {}
    candidate_lines = [lines[position] for position in candidate_positions]
    tree_index = STRtree(candidate_lines)
    left_ids, right_ids = tree_index.query(
        candidate_lines, predicate="dwithin", distance=SIBLING_MAX_SEPARATION_M
    )

    parent: dict[int, int] = {}

    def find(position: int) -> int:
        while parent.setdefault(position, position) != position:
            parent[position] = parent[parent[position]]
            position = parent[position]
        return position

    for left, right in zip(left_ids, right_ids):
        if left >= right:
            continue  # self-pair, or the mirror of a pair already seen
        position_a = candidate_positions[left]
        position_b = candidate_positions[right]
        if names[position_a] != names[position_b]:
            continue
        if set(node_pairs[position_a]) & set(node_pairs[position_b]):
            continue  # consecutive blocks of one street, not two carriageways
        long_length = max(lengths[position_a], lengths[position_b])
        short_length = min(lengths[position_a], lengths[position_b])
        if long_length > SIBLING_MAX_LENGTH_RATIO * short_length + 20.0:
            continue
        if _bearing_difference_deg(lines[position_a], lines[position_b]) > SIBLING_MAX_BEARING_DIFF_DEG:
            continue
        if _along_track_overlap_fraction(lines[position_a], lines[position_b]) < SIBLING_MIN_OVERLAP_FRACTION:
            continue
        root_a, root_b = find(position_a), find(position_b)
        if root_a != root_b:
            parent[root_a] = root_b

    group_of_edge = {position: find(position) for position in parent}
    # Singletons can appear in `parent` from find() calls without a union.
    group_sizes: dict[int, int] = {}
    for group_id in group_of_edge.values():
        group_sizes[group_id] = group_sizes.get(group_id, 0) + 1
    return {
        position: group_id
        for position, group_id in group_of_edge.items()
        if group_sizes[group_id] >= 2
    }


def _bearing_difference_deg(line_a, line_b) -> float:
    """Direction difference between two lines' end-to-end vectors, ignoring
    which way each happens to be digitized (a street has no true forward)."""
    def bearing(line) -> float:
        (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
        return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 180.0

    difference = abs(bearing(line_a) - bearing(line_b))
    return min(difference, 180.0 - difference)


def _along_track_overlap_fraction(line_a, line_b) -> float:
    """How much of the shorter line runs alongside the longer one: both
    lines' endpoints projected onto line_a's own direction give each a 1-D
    span; the overlap of the spans, as a fraction of the shorter span,
    separates true side-by-side carriageways from two same-named streets
    that merely pass near each other at an end."""
    (ax0, ay0), (ax1, ay1) = line_a.coords[0], line_a.coords[-1]
    direction_x, direction_y = ax1 - ax0, ay1 - ay0
    norm = (direction_x**2 + direction_y**2) ** 0.5
    if norm == 0:
        return 0.0

    def project(x: float, y: float) -> float:
        return ((x - ax0) * direction_x + (y - ay0) * direction_y) / norm

    span_a = sorted((project(ax0, ay0), project(ax1, ay1)))
    (bx0, by0), (bx1, by1) = line_b.coords[0], line_b.coords[-1]
    span_b = sorted((project(bx0, by0), project(bx1, by1)))
    overlap = min(span_a[1], span_b[1]) - max(span_a[0], span_b[0])
    shorter_span = min(span_a[1] - span_a[0], span_b[1] - span_b[0])
    if shorter_span <= 0:
        return 0.0
    return max(overlap, 0.0) / shorter_span


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
