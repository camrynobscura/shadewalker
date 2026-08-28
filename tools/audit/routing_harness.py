"""The seeded citywide routing harness, as a durable tool.

WHAT THIS IS
------------
The instrument that falsified two shade fixes and verified the third
during `park-canopy` (history/park-canopy.md): draw N seeded random
walk-length pairs citywide, route each at every Shade_priority weight
THROUGH THE FULL BATCH (clamp_shade_monotonic needs the whole ladder --
querying one weight alone silently skips the clamp, this project's
best-documented trap), and record length / shade / sunny metres per
route. Two result files from two exports diff into a verdict.

The original lived in the session scratchpad and was wiped; this rebuild
uses the same seed (20260825) but the pair recipe was re-derived, so
numbers are comparable BETWEEN RUNS OF THIS TOOL, not against the
figures quoted in history/park-canopy.md. An A/B needs both runs from
this same script -- which is the only use it has.

USAGE
-----
    # against whatever the server would load (honors SHADEWALKER_EXPORT_DIR)
    uv run python tools/audit/routing_harness.py run --out before.json

    SHADEWALKER_EXPORT_DIR=/tmp/scratch \
        uv run python tools/audit/routing_harness.py run --out after.json

    uv run python tools/audit/routing_harness.py compare before.json after.json

Loading the graph takes ~10-60s and peaks ~1.1GB RAM (0.7GB steady,
measured in-process 2026-08-28); run and compare are separate processes
so two graphs never coexist.
"""

import argparse
import json
import logging
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402
from server.graph_store import (  # noqa: E402
    GraphStore, _local_distance_m, clamp_shade_monotonic,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("harness")

DEFAULT_SEED = 20260825
# 800, not the original 400: THIS recipe draws uniformly over CITY_BBOX,
# which includes water and out-of-city corners, and yields ~23% routable
# (measured 2026-08-27: 93/400). 800 draws restores roughly the ~188
# routable pairs the original harness worked with.
DEFAULT_PAIRS = 800
DEFAULT_WEIGHTS = [0.0, 5.0, 15.0, 40.0]
DEFAULT_MONTH = 7
# A realistic walk request, not a commute: the same band the external
# validation harness settled on. Draws outside it are discarded BEFORE
# any routing, and the discard count is recorded -- "say what you
# divided by" (CLAUDE.md).
MIN_PAIR_M, MAX_PAIR_M = 500.0, 5000.0


def draw_pairs(count: int, seed: int) -> tuple[list, int]:
    """Seeded random (lat, lon, lat, lon) pairs within the walk band."""
    rng = random.Random(seed)
    bbox = config.CITY_BBOX
    pairs, discarded = [], 0
    while len(pairs) < count:
        a_lat = rng.uniform(bbox.lat_min, bbox.lat_max)
        a_lon = rng.uniform(bbox.lon_min, bbox.lon_max)
        b_lat = rng.uniform(bbox.lat_min, bbox.lat_max)
        b_lon = rng.uniform(bbox.lon_min, bbox.lon_max)
        if MIN_PAIR_M <= _local_distance_m(a_lat, a_lon, b_lat, b_lon) <= MAX_PAIR_M:
            pairs.append((a_lat, a_lon, b_lat, b_lon))
        else:
            discarded += 1
    return pairs, discarded


def run(args) -> int:
    pairs, discarded = draw_pairs(args.pairs, args.seed)
    logger.info(f"[harness] {len(pairs)} pairs accepted "
                f"({discarded} draws discarded on distance)")

    store = GraphStore()
    started = time.monotonic()
    store.load()
    logger.info(f"[harness] graph loaded in {time.monotonic() - started:.0f}s "
                f"from {config.EXPORT_DIR}")

    results = []
    routable = clamp_fired = 0
    for position, (a_lat, a_lon, b_lat, b_lon) in enumerate(pairs):
        if position and position % 100 == 0:
            logger.info(f"[harness] {position} pairs routed")
        snapped = store.snap_pair(a_lat, a_lon, b_lat, b_lon)
        entry = {"pair": [a_lat, a_lon, b_lat, b_lon], "routes": None}
        if snapped is not None:
            start, end = snapped
            routes = [store.route(start, end, w, args.month)
                      for w in args.weights]
            if all(r is not None for r in routes):
                before = [r["shade_fraction"] for r in routes]
                routes = clamp_shade_monotonic(routes, args.weights)
                if [r["shade_fraction"] for r in routes] != before:
                    clamp_fired += 1
                routable += 1
                entry["routes"] = [
                    {"weight": w,
                     "length_m": round(r["length_m"], 1),
                     "shade_fraction": r["shade_fraction"],
                     "sunny_m": round(
                         r["length_m"] * (1.0 - r["shade_fraction"]), 1)}
                    for w, r in zip(args.weights, routes)]
        results.append(entry)

    payload = {
        "meta": {"seed": args.seed, "pairs": len(pairs),
                 "discarded_on_distance": discarded,
                 "weights": args.weights, "month": args.month,
                 "routable": routable, "clamp_fired": clamp_fired,
                 "export_dir": str(config.EXPORT_DIR),
                 "measured": time.strftime("%Y-%m-%d %H:%M:%S")},
        "results": results,
    }
    with open(args.out, "w") as fh:
        json.dump(payload, fh)
    logger.info(f"[harness] {routable}/{len(pairs)} routable, clamp fired "
                f"on {clamp_fired}; wrote {args.out}")
    return 0


def compare(args) -> int:
    with open(args.before) as fh:
        before = json.load(fh)
    with open(args.after) as fh:
        after = json.load(fh)
    if before["meta"]["seed"] != after["meta"]["seed"] or (
            before["meta"]["weights"] != after["meta"]["weights"]):
        print("Refusing to compare: seed or weights differ between files.")
        return 1

    weights = before["meta"]["weights"]
    print(f"routable: {before['meta']['routable']} -> "
          f"{after['meta']['routable']}   clamp fired: "
          f"{before['meta']['clamp_fired']} -> {after['meta']['clamp_fired']}")

    both = [(b, a) for b, a in zip(before["results"], after["results"])
            if b["routes"] is not None and a["routes"] is not None]
    print(f"pairs routable in both: {len(both)}\n")
    print(f"{'weight':>7s} {'med shade b->a':>18s} {'gain>.5pt':>10s} "
          f"{'lose>.5pt':>10s} {'med len delta':>14s} {'med sunny delta':>16s}")
    for i, w in enumerate(weights):
        shade_b = sorted(b["routes"][i]["shade_fraction"] for b, _ in both)
        shade_a = sorted(a["routes"][i]["shade_fraction"] for _, a in both)
        deltas = [(a["routes"][i]["shade_fraction"]
                   - b["routes"][i]["shade_fraction"]) for b, a in both]
        len_d = sorted(a["routes"][i]["length_m"] - b["routes"][i]["length_m"]
                       for b, a in both)
        sun_d = sorted(a["routes"][i]["sunny_m"] - b["routes"][i]["sunny_m"]
                       for b, a in both)
        mid = len(both) // 2
        print(f"{w:>7.0f} "
              f"{shade_b[mid]:>8.3f} -> {shade_a[mid]:.3f} "
              f"{sum(1 for d in deltas if d > 0.005):>10,} "
              f"{sum(1 for d in deltas if d < -0.005):>10,} "
              f"{len_d[mid]:>+13.1f}m {sun_d[mid]:>+15.1f}m")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--pairs", type=int, default=DEFAULT_PAIRS)
    p_run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p_run.add_argument("--month", type=int, default=DEFAULT_MONTH)
    p_run.add_argument("--weights", type=lambda s: [float(x) for x in s.split(",")],
                       default=DEFAULT_WEIGHTS)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")
    args = parser.parse_args()
    return run(args) if args.mode == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
