"""Process one tile, or every tile in a borough, end-to-end. Usage:

    uv run python -m pipeline.run_tile pilot
    uv run python -m pipeline.run_tile pilot --refresh-trees
    uv run python -m pipeline.run_tile brooklyn

Stages: fetch → boundary clip → graph → scoring → export. The fetch stage is disk-cached
(the slow, network-bound part); the compute stages are fast enough to
re-run every time, which keeps them always consistent with config tweaks.
A borough name runs every tile that covers it, one at a time, reusing the
same per-tile cache -- an interrupted borough run picks back up without
re-fetching tiles it already finished.
"""

import argparse

from pipeline import config, export
from pipeline.fetch import boundaries, streets, trees
from pipeline.graph import boundary, centerline
from pipeline.scoring import trees as tree_scoring


def _remove_stale_export(tile_id: str) -> None:
    """Delete tile_id's exported file, if one exists from a previous run.

    A tile that used to have real data can legitimately go empty on a
    later run -- most concretely, a tile that was 100% foreign territory
    still exported under the old rectangle-overreach pipeline (before
    clip_to_nyc existed) now correctly clips down to zero nodes. Without
    this, its old export would sit in data/tiles/ forever: GraphStore's
    load() merges every *.json.gz file it finds there regardless of when
    it was written, so a stale export keeps re-polluting every future
    load with exactly the foreign-territory garbage this whole body of
    work exists to remove.
    """
    stale_path = config.TILES_DIR / f"{tile_id}.json.gz"
    if stale_path.exists():
        stale_path.unlink()
        print(f"[{tile_id}] removed stale export from a previous run")


def run(tile_id: str, refresh_trees: bool = False) -> None:
    bbox = config.get_tile_bbox(tile_id)
    print(f"[{tile_id}] lat {bbox.lat_min}–{bbox.lat_max}, lon {bbox.lon_min}–{bbox.lon_max}")

    # Fetch bbox is padded past the tile's true edges (FETCH_BUFFER_M) so
    # neighboring tiles' data genuinely overlaps at their shared border --
    # required for GraphStore's load()-time merge to have a matching OSM
    # node id to stitch on. The exported tile still covers this whole
    # padded area (not clipped back to `bbox`); the overlap is deliberate,
    # and duplicate nodes/edges between adjacent tiles get deduplicated at
    # server load time, not here.
    fetch_bbox = config.buffered_bbox(bbox, config.FETCH_BUFFER_M)

    # fetch (cached). Streets first: a tile with no matching streets (open
    # water) needs no tree data either, so checking this first skips a
    # pointless Socrata call on top of the Overpass one.
    street_graph = streets.fetch_streets(fetch_bbox, tile_id)
    if street_graph is None:
        print(f"[{tile_id}] skipped -- no walkable streets in this area")
        _remove_stale_export(tile_id)
        return

    # Drop anything fetch_bbox's overreach swept in from outside NYC
    # (Jersey City, Bayonne, open water past the real coastline) before it
    # ever reaches scoring/export -- the real fix for the foreign-territory
    # problem server/graph_store.py's load-time pruning used to paper over
    # after the fact (see PLAN.md's borough-boundary polygon plan).
    nyc_shape = boundary.nyc_boundary(boundaries.fetch_borough_boundaries())
    street_graph = boundary.clip_to_nyc(street_graph, nyc_shape)
    if street_graph.number_of_edges() == 0:
        # Real case (Bronx r22c19): a few isolated nodes can survive
        # clipping -- each one's own edges all led to a node that got
        # dropped, so networkx's cascade-delete on removal leaves these
        # as edgeless stragglers, not a fully-empty graph. Checking edges
        # rather than nodes catches this; centerline.build_edge_table()
        # crashes on an edgeless graph (osmnx's own to_undirected() raises
        # "Graph contains no edges" via graph_to_gdfs()).
        print(f"[{tile_id}] skipped -- no edges remain inside NYC after boundary clipping")
        _remove_stale_export(tile_id)
        return

    # Build the edge table BEFORE fetching trees, so the tree fetch can
    # cover the edges' real extent instead of the tile's nominal padded
    # bbox. The two are usually the same area -- but osmnx's simplification
    # can hand back edges reaching well past the fetched bbox (a merged-
    # chain edge keeps its full geometry), and scoring such an edge against
    # a nominal-bbox tree fetch silently produces a zero-tree copy of a
    # street that another tile scores correctly. Real case: a 1.7km Harlem
    # River Drive Greenway edge, 0 trees in r19c13's export vs 95 in
    # r20c13's, resolved at server load time by arbitrary filename order.
    nodes, edges = centerline.build_edge_table(street_graph)

    # total_bounds is (minx, miny, maxx, maxy) on the LAT/LON geometry --
    # the tree fetch queries Socrata in lat/lon, so the meter-based
    # geometry_m column is the wrong one here (the dual-CRS rule).
    lon_min, lat_min, lon_max, lat_max = edges["geometry"].total_bounds
    edges_bbox = config.Bbox(lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max)
    tree_bbox = config.buffered_bbox(edges_bbox, config.TREE_FETCH_MARGIN_M)
    tree_rows = trees.fetch_trees(tree_bbox, tile_id, refresh=refresh_trees)

    edges = tree_scoring.score_and_join(edges, tree_rows)
    export.write_tile(tile_id, nodes, edges)

    print(f"[{tile_id}] done")


def run_borough(borough: str, refresh_trees: bool = False) -> None:
    """Run every grid tile covering a borough, one at a time.

    Every borough resolves through the real NYC Open Data borough-boundary
    polygon (pipeline/graph/boundary.py), not a hand-picked rectangle --
    Brooklyn used a rectangle (config.BOROUGH_BBOXES) until this leaked
    real Queens/Staten Island territory into what was supposed to be
    Brooklyn-only data (a rectangle can't hug a non-rectangular coastline;
    see PLAN.md). An unrecognized borough name surfaces as a ValueError
    from boundary.borough_polygon() rather than a silent empty tile list.
    """
    geojson = boundaries.fetch_borough_boundaries()
    polygon = boundary.borough_polygon(geojson, borough)
    tile_ids = boundary.tile_ids_for_polygon(polygon)

    print(f"[{borough}] {len(tile_ids)} tiles to process: {', '.join(tile_ids)}")
    for i, tile_id in enumerate(tile_ids, start=1):
        print(f"[{borough}] tile {i}/{len(tile_ids)}")
        run(tile_id, refresh_trees=refresh_trees)
    print(f"[{borough}] done -- {len(tile_ids)} tiles")


def main() -> None:
    # argparse is Python's built-in CLI-argument parser (JS analogy: commander,
    # but in the standard library). It also generates --help for free.
    parser = argparse.ArgumentParser(description="Run the preprocessing pipeline for one tile or borough.")
    parser.add_argument(
        "tile_id",
        help="'pilot', a grid id like 'r12c07', or a borough name like 'brooklyn'",
    )
    parser.add_argument(
        "--refresh-trees",
        action="store_true",  # boolean flag: present=True, absent=False
        help="Re-download tree data instead of using the cache (streets stay cached)",
    )
    args = parser.parse_args()
    if config.is_grid_tile_id(args.tile_id):
        run(args.tile_id, refresh_trees=args.refresh_trees)
    else:
        run_borough(args.tile_id, refresh_trees=args.refresh_trees)


# "Only run main() when executed as a script, not when imported."
# (JS analogy: require.main === module)
if __name__ == "__main__":
    main()
