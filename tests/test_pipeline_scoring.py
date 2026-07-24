"""Tests for pipeline/scoring/trees.py's score_and_join() -- the buffer/
spatial-join/aggregation logic that turns raw tree rows into per-edge
tree_deciduous/tree_evergreen/tree_count columns.

Trees are placed at *exact* meter offsets from a test edge by round-tripping
through METRIC_CRS with the same pyproj transform the pipeline itself uses
(see _offset_m) -- this makes buffer-boundary assertions (TREE_BUFFER_M=12m)
exact instead of approximate lon/lat deltas.
"""

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from pipeline import config
from pipeline.graph.centerline import METRIC_CRS
from pipeline.scoring.trees import score_and_join

# A real point in the pilot tile area -- arbitrary, just needs to be a
# plausible NYC lon/lat so the UTM zone (18N) is the right one.
BASE_LON, BASE_LAT = -73.99, 40.68

_to_m = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
_to_deg = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)


def _offset_m(lon: float, lat: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Return (lon, lat) exactly dx_m/dy_m meters from (lon, lat)."""
    x, y = _to_m.transform(lon, lat)
    return _to_deg.transform(x + dx_m, y + dy_m)


def _edge(dx_m: float = 0.0, dy_m: float = 0.0, length_m: float = 100.0) -> gpd.GeoDataFrame:
    """A single straight edge, offset by (dx_m, dy_m) from BASE_LON/BASE_LAT,
    running length_m east. Only geometry_m is populated -- score_and_join
    never reads the lon/lat geometry column."""
    base_x, base_y = _to_m.transform(BASE_LON, BASE_LAT)
    base_x += dx_m
    base_y += dy_m
    line_m = LineString([(base_x, base_y), (base_x + length_m, base_y)])
    return gpd.GeoDataFrame({"geometry_m": [line_m]}, geometry="geometry_m", crs=METRIC_CRS)


def _edges(*rows: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    import pandas as pd

    combined = gpd.GeoDataFrame(pd.concat(rows, ignore_index=True), geometry="geometry_m", crs=METRIC_CRS)
    return combined


def _tree_row(
    lon: float,
    lat: float,
    condition: str = "Excellent",
    dbh: str = "20",
    genus: str = "Quercus bicolor - swamp white oak",
) -> dict:
    row = {
        "tpcondition": condition,
        "genusspecies": genus,
        "location": {"coordinates": [lon, lat]},
    }
    if dbh is not None:
        row["dbh"] = dbh
    return row


def test_tree_within_buffer_counts_tree_outside_buffer_does_not():
    edges = _edge()
    near_lon, near_lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)  # 5m off the line
    far_lon, far_lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 30.0)  # 30m off the line

    result = score_and_join(edges, [_tree_row(near_lon, near_lat)])
    assert result.iloc[0]["tree_count"] == 1

    result = score_and_join(edges, [_tree_row(far_lon, far_lat)])
    assert result.iloc[0]["tree_count"] == 0
    assert result.iloc[0]["tree_deciduous"] == 0


def test_dead_trees_are_excluded():
    edges = _edge()
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)

    result = score_and_join(edges, [_tree_row(lon, lat, condition="Dead")])
    assert result.iloc[0]["tree_count"] == 0
    assert result.iloc[0]["tree_deciduous"] == 0


def test_dbh_is_capped_so_oversized_trunks_dont_dominate():
    edges = _edge()
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)

    capped = score_and_join(edges, [_tree_row(lon, lat, dbh="60")])
    at_cap = score_and_join(edges, [_tree_row(lon, lat, dbh="30")])
    assert capped.iloc[0]["tree_deciduous"] == at_cap.iloc[0]["tree_deciduous"]


def test_missing_or_garbage_dbh_excludes_the_tree():
    edges = _edge()
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)

    missing = score_and_join(edges, [_tree_row(lon, lat, dbh=None)])
    assert missing.iloc[0]["tree_count"] == 0

    garbage = score_and_join(edges, [_tree_row(lon, lat, dbh="n/a")])
    assert garbage.iloc[0]["tree_count"] == 0


def test_evergreen_genus_splits_into_the_evergreen_column():
    edges = _edge()
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)

    evergreen = score_and_join(edges, [_tree_row(lon, lat, genus="Pinus strobus - eastern white pine")])
    assert evergreen.iloc[0]["tree_evergreen"] > 0
    assert evergreen.iloc[0]["tree_deciduous"] == 0

    deciduous = score_and_join(edges, [_tree_row(lon, lat, genus="Quercus bicolor - swamp white oak")])
    assert deciduous.iloc[0]["tree_deciduous"] > 0
    assert deciduous.iloc[0]["tree_evergreen"] == 0


def test_unknown_condition_defaults_to_the_condition_midpoint():
    edges = _edge()
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)

    missing_condition = _tree_row(lon, lat)
    del missing_condition["tpcondition"]
    result_missing = score_and_join(edges, [missing_condition])

    result_unknown = score_and_join(edges, [_tree_row(lon, lat, condition="Unknown")])

    assert result_missing.iloc[0]["tree_deciduous"] == result_unknown.iloc[0]["tree_deciduous"]
    assert config.CONDITION_DEFAULT == config.CONDITION_SCORES["Unknown"]


def test_multiple_trees_on_one_edge_sum_into_the_edge_totals():
    edges = _edge()
    lon1, lat1 = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)
    lon2, lat2 = _offset_m(BASE_LON, BASE_LAT, 20.0, -5.0)

    single = score_and_join(edges, [_tree_row(lon1, lat1, dbh="15")])
    both = score_and_join(
        edges,
        [_tree_row(lon1, lat1, dbh="15"), _tree_row(lon2, lat2, dbh="15")],
    )

    assert both.iloc[0]["tree_count"] == 2
    assert both.iloc[0]["tree_deciduous"] == pytest.approx(2 * single.iloc[0]["tree_deciduous"])


def test_a_tree_between_two_corridors_counts_toward_both():
    # Two parallel edges 10m apart -- well within each one's buffer of the
    # midpoint, so a tree exactly between them falls in both corridors.
    # These edges carry no names, so they can't be sibling carriageways --
    # this is the legitimate shared-credit case (e.g. a corner tree between
    # two genuinely different streets), and it must keep counting for both.
    edges = _edges(_edge(dy_m=0.0), _edge(dy_m=10.0))
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 5.0, 5.0)  # midpoint between the two lines

    result = score_and_join(edges, [_tree_row(lon, lat)])
    assert result.iloc[0]["tree_count"] == 1
    assert result.iloc[1]["tree_count"] == 1


def _named_edges(*rows_with_names: tuple[gpd.GeoDataFrame, str],
                 node_pairs: list[tuple[str, str]] | None = None) -> gpd.GeoDataFrame:
    """Like _edges, but with a name column and a (u, v, key) MultiIndex --
    the shape the real pipeline edge table has, which the sibling-
    carriageway detection reads."""
    import pandas as pd

    frames = [frame for frame, _name in rows_with_names]
    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry_m", crs=METRIC_CRS
    )
    combined["name"] = [name for _frame, name in rows_with_names]
    if node_pairs is None:
        node_pairs = [(f"u{i}", f"v{i}") for i in range(len(combined))]
    combined.index = pd.MultiIndex.from_tuples(
        [(u, v, 0) for u, v in node_pairs], names=["u", "v", "key"]
    )
    return combined


def test_a_median_tree_between_sibling_carriageways_counts_only_toward_the_nearer():
    # The divided-boulevard case: two same-named carriageways 12m apart
    # (Park Avenue's measured range is 11-18m). A median tree 5m from one
    # side and 7m from the other falls in both corridors; it must count
    # once, toward the nearer carriageway -- full credit to both sides
    # inflated those streets' tree value by a measured 19% citywide.
    edges = _named_edges(
        (_edge(dy_m=0.0), "Divided Boulevard"),
        (_edge(dy_m=12.0), "Divided Boulevard"),
    )
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 50.0, 5.0)  # 5m from south, 7m from north

    result = score_and_join(edges, [_tree_row(lon, lat)])
    assert result.iloc[0]["tree_count"] == 1  # the nearer (south) carriageway
    assert result.iloc[1]["tree_count"] == 0


def test_differently_named_parallel_streets_both_keep_a_shared_tree():
    # Same geometry as the sibling case, but the two streets genuinely
    # differ -- shared credit is correct and must survive the sibling rule.
    edges = _named_edges(
        (_edge(dy_m=0.0), "Alpha Street"),
        (_edge(dy_m=12.0), "Beta Street"),
    )
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 50.0, 5.0)

    result = score_and_join(edges, [_tree_row(lon, lat)])
    assert result.iloc[0]["tree_count"] == 1
    assert result.iloc[1]["tree_count"] == 1


def test_consecutive_blocks_sharing_a_node_are_not_siblings():
    # Two same-named edges that SHARE an endpoint are consecutive blocks of
    # one street, not two carriageways -- a tree near the shared corner
    # legitimately sits in both blocks' corridors and keeps both credits.
    first_block = _edge(dx_m=0.0, length_m=100.0)
    second_block = _edge(dx_m=100.0, length_m=100.0)  # continues where the first ends
    edges = _named_edges(
        (first_block, "Continuous Street"),
        (second_block, "Continuous Street"),
        node_pairs=[("a", "b"), ("b", "c")],  # "b" shared: consecutive, not parallel
    )
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 100.0, 5.0)  # at the shared corner

    result = score_and_join(edges, [_tree_row(lon, lat)])
    assert result.iloc[0]["tree_count"] == 1
    assert result.iloc[1]["tree_count"] == 1


def test_three_carriageway_groups_are_handled_transitively():
    # Queens Boulevard-style: main road plus a service road on each side,
    # three same-named near-parallel edges abreast (511 of the 3,393 groups
    # found citywide have 3+ members). A tree in the first gap sits in the
    # corridors of all three at a 14m buffer; only the single nearest
    # carriageway may keep it, which requires the grouping to be
    # transitive -- pairwise-only logic would let the far side keep a copy.
    edges = _named_edges(
        (_edge(dy_m=0.0), "Queens Boulevard"),
        (_edge(dy_m=13.0), "Queens Boulevard"),
        (_edge(dy_m=26.0), "Queens Boulevard"),
    )
    lon, lat = _offset_m(BASE_LON, BASE_LAT, 50.0, 6.0)  # 6m, 7m, 20m from the three lines

    result = score_and_join(edges, [_tree_row(lon, lat)])
    assert result.iloc[0]["tree_count"] == 1
    assert result.iloc[1]["tree_count"] == 0
    assert result.iloc[2]["tree_count"] == 0
