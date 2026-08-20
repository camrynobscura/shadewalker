"""Rebuild citywide tiles from LOCAL caches with parallel workers. Usage:

    uv run python -m pipeline.rebuild_citywide                  # all 276 tiles
    uv run python -m pipeline.rebuild_citywide --workers 5
    uv run python -m pipeline.rebuild_citywide --tiles r20c15 r16c12

Built (2026-08-20) for GRAPH_CACHE_VERSION bumps: the sequential borough
loop took ~2.5h for a full recompose that never touches the network --
every worker here rebuilds tiles from the raw WALK_FILTER snapshots and
citywide layer caches already on disk. Measured 1.3GB RSS per worker
process (the citywide layer caches, loaded once per worker and reused
across its tiles), so the default of 4 workers fits a 16GB machine with
room for a running dev server.

Deliberately NO --refresh-trees / --refresh-raw: fresh fetching stays in
pipeline.run_tile, one tile or borough at a time. Two reasons. First,
Overpass politeness -- parallel workers would multiply request rate
against a public instance whose usage policy this project already
respects (see streets.py's user-agent comment). Second, cache-write
races: a refresh rewrites the SHARED citywide layer caches, and
concurrent workers would fetch and write the same files over each other.
Rebuild-from-cache has neither problem: tile-keyed outputs (graphml,
exports, tree caches) are written by exactly one worker each (the tile
list is deduplicated), and the shared citywide caches are only read --
after the warmup step below guarantees they exist.

--tiles doubles as the bounded-rebuild tool: when a version bump's blast
radius is enumerable (v25's fee-gated zones touched exactly 17 tiles),
rebuilding just those and carrying the rest forward is ~8x cheaper than
a full pass. The burden of proof for "the rest are unaffected" is on the
caller -- see HISTORY 2026-08-20 for the v25 worked example, including
the run-to-run tie-break noise (a handful of resegmented edges per tile,
present between ANY two runs) that a naive byte-diff will flag.
"""

import argparse
import logging
import multiprocessing
import sys
import time
import traceback

logger = logging.getLogger(__name__)

# The dataset's five boroughs, resolved through the same real boundary
# polygons run_tile's borough mode uses. Order is cosmetic (the pool
# balances tile-by-tile); the dedup below matters because a tile whose
# bbox straddles a borough line appears in both boroughs' lists.
BOROUGHS = ("manhattan", "bronx", "brooklyn", "queens", "staten island")

DEFAULT_WORKERS = 4  # 4 x 1.3GB measured RSS + driver + dev server < 16GB


def citywide_tile_ids() -> list[str]:
    """Every borough's tile list, deduplicated, first occurrence wins."""
    from pipeline.fetch import boundaries
    from pipeline.graph import boundary

    geojson = boundaries.fetch_borough_boundaries()
    seen: dict[str, None] = {}
    for borough in BOROUGHS:
        polygon = boundary.borough_polygon(geojson, borough)
        for tile_id in boundary.tile_ids_for_polygon(polygon):
            seen.setdefault(tile_id)
    return list(seen)


def _init_worker() -> None:
    """Per-worker logging setup -- macOS uses the spawn start method, so
    workers don't inherit the driver's logging config. The PID in the
    format keeps interleaved worker lines attributable."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(process)d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _warm_shared_caches() -> str:
    """Touch every SHARED cache once, inside one pool worker, before any
    tiles are distributed -- so no two workers ever race to CREATE the
    same citywide file. Tile-keyed caches don't need this (one writer
    each); this covers the citywide ones. The worker that ran this keeps
    the caches in memory and goes on to process tiles like any other."""
    from pipeline.fetch import boundaries, interior_sidewalks, park_trails
    from pipeline.fetch import citywide_layers, streets
    from pipeline.scoring import canopy

    boundaries.fetch_borough_boundaries()
    for layer_name, (custom_filter, _label) in streets.CITYWIDE_LAYERS.items():
        citywide_layers.citywide_layer(layer_name, custom_filter)
    interior_sidewalks.fetch_interior_sidewalks()
    park_trails.fetch_park_trails()
    canopy.citywide_park_reach_m()
    return "shared caches warm"


def _rebuild_one(tile_id: str) -> tuple[str, str | None]:
    """(tile_id, None) on success, (tile_id, traceback text) on failure --
    exceptions can't cross the pool boundary usefully, and one bad tile
    must not abort the other 275."""
    from pipeline import run_tile

    try:
        run_tile.run(tile_id)
        return tile_id, None
    except Exception:  # noqa: BLE001 -- reported per tile, run continues
        return tile_id, traceback.format_exc()


def main() -> None:
    _init_worker()  # driver logging, same shape as the workers'
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--tiles", nargs="+", default=None,
                        help="explicit tile ids instead of the full citywide list")
    args = parser.parse_args()

    tiles = args.tiles if args.tiles else citywide_tile_ids()
    logger.info(f"[rebuild] {len(tiles)} tiles, {args.workers} workers")

    started = time.monotonic()
    failures: list[tuple[str, str]] = []
    with multiprocessing.Pool(args.workers, initializer=_init_worker) as pool:
        # Warmup runs to completion in ONE worker before any tile is
        # handed out -- see _warm_shared_caches.
        logger.info(f"[rebuild] {pool.apply(_warm_shared_caches)}")
        done = 0
        for tile_id, error in pool.imap_unordered(_rebuild_one, tiles, chunksize=1):
            done += 1
            if error is not None:
                failures.append((tile_id, error))
                logger.error(f"[rebuild] {done}/{len(tiles)} {tile_id} FAILED:\n{error}")
            else:
                elapsed = time.monotonic() - started
                logger.info(f"[rebuild] {done}/{len(tiles)} {tile_id} ok "
                            f"({elapsed / 60:.1f} min elapsed)")

    elapsed_min = (time.monotonic() - started) / 60
    if failures:
        names = ", ".join(t for t, _ in failures)
        logger.error(f"[rebuild] DONE WITH FAILURES in {elapsed_min:.1f} min -- "
                     f"{len(failures)} tile(s) failed: {names}")
        sys.exit(1)
    logger.info(f"[rebuild] all {len(tiles)} tiles done in {elapsed_min:.1f} min")


if __name__ == "__main__":
    main()
