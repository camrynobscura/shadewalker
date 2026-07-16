"""Process one tile, or every tile in a borough, end-to-end. Usage:

    uv run python -m pipeline.run_tile pilot
    uv run python -m pipeline.run_tile pilot --refresh-trees
    uv run python -m pipeline.run_tile brooklyn

Stages: fetch → graph → scoring → export. The fetch stage is disk-cached
(the slow, network-bound part); the compute stages are fast enough to
re-run every time, which keeps them always consistent with config tweaks.
A borough name runs every tile that covers it, one at a time, reusing the
same per-tile cache -- an interrupted borough run picks back up without
re-fetching tiles it already finished.
"""

import argparse

from pipeline import config, export
from pipeline.fetch import streets, trees
from pipeline.graph import centerline
from pipeline.scoring import trees as tree_scoring


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
        return

    tree_rows = trees.fetch_trees(fetch_bbox, tile_id, refresh=refresh_trees)

    # graph → scoring → export
    nodes, edges = centerline.build_edge_table(street_graph)
    edges = tree_scoring.score_and_join(edges, tree_rows)
    export.write_tile(tile_id, nodes, edges)

    print(f"[{tile_id}] done")


def run_borough(borough: str, refresh_trees: bool = False) -> None:
    """Run every grid tile covering a borough, one at a time."""
    tile_ids = config.get_tile_ids_for_bbox(config.BOROUGH_BBOXES[borough])
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
    if args.tile_id in config.BOROUGH_BBOXES:
        run_borough(args.tile_id, refresh_trees=args.refresh_trees)
    else:
        run(args.tile_id, refresh_trees=args.refresh_trees)


# "Only run main() when executed as a script, not when imported."
# (JS analogy: require.main === module)
if __name__ == "__main__":
    main()
