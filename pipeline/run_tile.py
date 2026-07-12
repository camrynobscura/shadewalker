"""Process one tile end-to-end. Usage:

    uv run python -m pipeline.run_tile pilot
    uv run python -m pipeline.run_tile pilot --refresh-trees

Stages: fetch → graph → scoring → export. The fetch stage is disk-cached
(the slow, network-bound part); the compute stages are fast enough to
re-run every time, which keeps them always consistent with config tweaks.
"""

import argparse

from pipeline import config, export
from pipeline.fetch import streets, trees
from pipeline.graph import centerline
from pipeline.scoring import trees as tree_scoring


def run(tile_id: str, refresh_trees: bool = False) -> None:
    bbox = config.get_tile_bbox(tile_id)
    print(f"[{tile_id}] lat {bbox.lat_min}–{bbox.lat_max}, lon {bbox.lon_min}–{bbox.lon_max}")

    # fetch (cached)
    tree_rows = trees.fetch_trees(bbox, tile_id, refresh=refresh_trees)
    street_graph = streets.fetch_streets(bbox, tile_id)

    # graph → scoring → export
    nodes, edges = centerline.build_edge_table(street_graph)
    edges = tree_scoring.score_and_join(edges, tree_rows)
    export.write_tile(tile_id, nodes, edges)

    print(f"[{tile_id}] done")


def main() -> None:
    # argparse is Python's built-in CLI-argument parser (JS analogy: commander,
    # but in the standard library). It also generates --help for free.
    parser = argparse.ArgumentParser(description="Run the preprocessing pipeline for one tile.")
    parser.add_argument("tile_id", help="Tile to process (Stage 1: only 'pilot')")
    parser.add_argument(
        "--refresh-trees",
        action="store_true",  # boolean flag: present=True, absent=False
        help="Re-download tree data instead of using the cache (streets stay cached)",
    )
    args = parser.parse_args()
    run(args.tile_id, refresh_trees=args.refresh_trees)


# "Only run main() when executed as a script, not when imported."
# (JS analogy: require.main === module)
if __name__ == "__main__":
    main()
