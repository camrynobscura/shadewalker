"""Fetch living street trees for a tile from the NYC Tree Map dataset.

Dataset: Forestry Tree Points (hn5i-inap) — the live data behind the NYC Tree
Map, continuously updated by NYC Parks. We filter server-side to living trees
(`tpstructure = 'Full'`, which excludes removed trees, stumps, and shafts) so
we don't download ~200k rows we'd immediately throw away. Condition-based
filtering (dropping Dead) happens later in scoring, so all scoring decisions
live in one place.
"""

from pipeline import config
from pipeline.config import Bbox
from pipeline.fetch import socrata

# Only the columns scoring needs — keeps the cache files small.
TREE_COLUMNS = "globalid, dbh, tpcondition, genusspecies, location"

# Bump whenever the query (bbox logic, columns, filters) changes -- baked
# into the cache filename so old cached rows are ignored rather than
# silently reused.
#
# v2 and v3 were both centerline-era, and the constants they named
# (FETCH_BUFFER_M, TREE_FETCH_MARGIN_M) and the caller they described
# (run_tile.py) have all since been deleted. What the versions were FOR
# still matters, so, without the dead identifiers: v2 padded the fetch bbox
# beyond the tile's exact bounds; v3 derived the bbox from the built edge
# table's real extent rather than the tile's nominal one, because simplified
# graphs carried edges well past the nominal bbox and scoring those against
# a nominal-bbox tree fetch produced provably-wrong zero-tree copies (a
# 1.7km Harlem River Drive Greenway edge scored 0 in one tile, 95 in its
# neighbor).
#
# Note this function was never really tile-bound: tile_id only names the
# cache file, and the real parameter is the bbox. A citywide fetch is a
# bbox and a cache name, not a rewrite.
TREE_CACHE_VERSION = 3


def fetch_trees(bbox: Bbox, tile_id: str, refresh: bool = False) -> list[dict]:
    """Return raw tree records (list of dicts) within the bbox, cached per tile."""
    # within_box is SoQL's spatial filter for point columns. Its argument
    # order is unusual: (column, NW corner lat, NW lon, SE lat, SE lon).
    where = (
        f"within_box(location, {bbox.lat_max}, {bbox.lon_min}, "
        f"{bbox.lat_min}, {bbox.lon_max}) "
        "AND tpstructure = 'Full'"
    )
    return socrata.fetch_all_rows(
        dataset_id=config.TREES_DATASET_ID,
        where=where,
        select=TREE_COLUMNS,
        order="globalid",  # any unique column works; needed for stable paging
        cache_name=f"trees_{tile_id}_v{TREE_CACHE_VERSION}",
        refresh=refresh,
    )
