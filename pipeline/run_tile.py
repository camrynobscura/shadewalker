"""Process one tile end-to-end. Usage:

    uv run python -m pipeline.run_tile pilot
    uv run python -m pipeline.run_tile pilot --refresh-trees

Stages run in order and each is independently cached, so re-running after an
interruption (or a config tweak) redoes only the work whose inputs changed.
Stage 1 scope: fetch. M2 adds: graph → tree scoring → export.
"""

import argparse

from pipeline import config
from pipeline.fetch import streets, trees


def run_fetch_stage(tile_id: str, refresh_trees: bool) -> None:
    bbox = config.get_tile_bbox(tile_id)
    print(f"[fetch] tile {tile_id!r}: "
          f"lat {bbox.lat_min}–{bbox.lat_max}, lon {bbox.lon_min}–{bbox.lon_max}")

    tree_rows = trees.fetch_trees(bbox, tile_id, refresh=refresh_trees)
    street_graph = streets.fetch_streets(bbox, tile_id)

    print(f"[fetch] done: {len(tree_rows)} living trees, "
          f"{len(street_graph.nodes)} intersections, {len(street_graph.edges)} street edges")


def main() -> None:
    # argparse is Python's built-in CLI-argument parser (JS analogy: yargs/commander,
    # but in the standard library). It also generates --help for free.
    parser = argparse.ArgumentParser(description="Run the preprocessing pipeline for one tile.")
    parser.add_argument("tile_id", help="Tile to process (Stage 1: only 'pilot')")
    parser.add_argument(
        "--refresh-trees",
        action="store_true",  # makes it a boolean flag: present=True, absent=False
        help="Re-download tree data instead of using the cache (streets stay cached)",
    )
    args = parser.parse_args()

    run_fetch_stage(args.tile_id, refresh_trees=args.refresh_trees)


# This guard means "only run main() when executed as a script, not when imported."
# `python -m pipeline.run_tile` executes it; `import pipeline.run_tile` would not.
if __name__ == "__main__":
    main()
