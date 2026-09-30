"""Seeded route-latency instrument: the same pairs, timed before and after.

WHAT THIS IS
------------
The speed counterpart of routing_harness.py, which records WHAT routes
come back and deliberately records no timing. This one records only
timing, on a fixed seeded draw, so two runs on two builds compare the
same pairs. It exists because the latency canary times four fixed
routes (a tripwire, not a benchmark) and the harness's 500m-5km walk
band never draws the long cross-borough routes where the cost lives.

Pairs are drawn uniformly over CITY_BBOX and bucketed by straight-line
distance into bands from a short walk to a cross-city trek; each band
fills to the same count of ROUTABLE pairs, so the long end is measured
with the same sample as the short end rather than as a tail. Each pair
is timed as the production request shape -- one snap_pair() plus the
full four-preset ladder through route() -- best of REPS repeats, so a
busy laptop lands in the spread, not in the number.

Numbers are dev-machine numbers: they prove an algorithm got faster or
slower, never what the live site does (the Mac has under-predicted the
droplet by more than the measured core ratio).

USAGE
-----
    uv run python tools/audit/route_timing.py run --out before.json
    # ...change the code...
    uv run python tools/audit/route_timing.py run --out after.json
    uv run python tools/audit/route_timing.py compare before.json after.json

Run it ALONE: another graph-loading process on the same machine (a
harness run, a dev server starting) skews every band. ~20-30 minutes
per run at the default 120 pairs per band (measured 2026-09-09).
"""

import argparse
import json
import logging
import os
import random
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402
from server.graph_store import GraphStore, _local_distance_m  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("timing")

DEFAULT_SEED = 20260909
DEFAULT_PER_BAND = 120
DEFAULT_REPS = 3
DEFAULT_WEIGHTS = [0.0, 5.0, 15.0, 40.0]
DEFAULT_MONTH = 7
# Straight-line metres between the two draws. The top band is bounded
# by the bbox itself (its diagonal is ~55km, and both ends must land
# on routable land), so it fills slowly -- MAX_DRAWS keeps an
# unfillable band from spinning forever.
BANDS = [(500.0, 2_000.0), (2_000.0, 5_000.0), (5_000.0, 10_000.0),
         (10_000.0, 20_000.0), (20_000.0, 35_000.0)]
MAX_DRAWS = 200_000


def band_of(straight_m: float) -> int | None:
    for index, (low, high) in enumerate(BANDS):
        if low <= straight_m < high:
            return index
    return None


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
            text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def time_ladder(store, pair, weights, month) -> float | None:
    """Wall seconds for one production-shaped request: snap + ladder.
    None when the pair doesn't route (the timing of a refusal is not
    what this measures)."""
    a_lat, a_lon, b_lat, b_lon = pair
    started = time.perf_counter()
    snapped = store.snap_pair(a_lat, a_lon, b_lat, b_lon)
    if snapped is None:
        return None
    start, end = snapped
    for weight in weights:
        if store.route(start, end, weight, month) is None:
            return None
    return time.perf_counter() - started


def run(args) -> int:
    store = GraphStore()
    started = time.monotonic()
    store.load()
    logger.info(f"[timing] graph loaded in {time.monotonic() - started:.0f}s "
                f"from {config.EXPORT_DIR}")

    rng = random.Random(args.seed)
    bbox = config.CITY_BBOX
    filled = [0] * len(BANDS)
    unroutable = [0] * len(BANDS)
    results = []
    draws = 0
    warmed_up = False
    while min(filled) < args.per_band and draws < MAX_DRAWS:
        draws += 1
        pair = (rng.uniform(bbox.lat_min, bbox.lat_max),
                rng.uniform(bbox.lon_min, bbox.lon_max),
                rng.uniform(bbox.lat_min, bbox.lat_max),
                rng.uniform(bbox.lon_min, bbox.lon_max))
        straight_m = _local_distance_m(*pair)
        band = band_of(straight_m)
        if band is None or filled[band] >= args.per_band:
            continue
        if not warmed_up:
            # First-touch costs (index warm-up, page-in of the freshly
            # loaded graph) belong to no pair.
            time_ladder(store, pair, args.weights, args.month)
            warmed_up = True
        samples = []
        for _ in range(args.reps):
            seconds = time_ladder(store, pair, args.weights, args.month)
            if seconds is None:
                break
            samples.append(seconds)
        if len(samples) < args.reps:
            unroutable[band] += 1
            continue
        filled[band] += 1
        results.append({"band": band, "pair": list(pair),
                        "straight_m": round(straight_m, 1),
                        "ladder_ms": round(min(samples) * 1000, 1),
                        "samples_ms": [round(s * 1000, 1) for s in samples]})
        if sum(filled) % 50 == 0:
            logger.info(f"[timing] {sum(filled)} pairs timed, per band {filled}")

    if min(filled) < args.per_band:
        logger.warning(f"[timing] gave up after {MAX_DRAWS} draws with bands "
                       f"at {filled} -- a band did not fill")

    payload = {
        "meta": {"seed": args.seed, "per_band": args.per_band,
                 "reps": args.reps, "bands_m": BANDS,
                 "weights": args.weights, "month": args.month,
                 "draws": draws, "unroutable_per_band": unroutable,
                 "export_dir": str(config.EXPORT_DIR),
                 "git_head": git_head(),
                 "measured": time.strftime("%Y-%m-%d %H:%M:%S")},
        "results": results,
    }
    with open(args.out, "w") as fh:
        json.dump(payload, fh)
    logger.info(f"[timing] {len(results)} pairs timed over {draws} draws, "
                f"unroutable per band {unroutable}; wrote {args.out}")
    print_summary({"before": payload})
    return 0


def band_label(index: int) -> str:
    low, high = BANDS[index]
    return f"{low / 1000:g}-{high / 1000:g}km"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[position]


def print_summary(files: dict) -> None:
    """One row per band; with two files, before -> after and the ratio
    of medians (>1 means after is faster)."""
    names = list(files)
    print(f"\n{'band':>10s} {'n':>4s}", end="")
    for name in names:
        print(f" {name + ' med':>12s} {name + ' p90':>12s} {name + ' max':>12s}", end="")
    if len(names) == 2:
        print(f" {'med ratio':>10s}", end="")
    print()
    for index in range(len(BANDS)):
        per_file = []
        for name in names:
            per_file.append([r["ladder_ms"] for r in files[name]["results"]
                             if r["band"] == index])
        if not per_file[0]:
            continue
        print(f"{band_label(index):>10s} {len(per_file[0]):>4d}", end="")
        medians = []
        for values in per_file:
            medians.append(statistics.median(values))
            print(f" {medians[-1]:>10.0f}ms {percentile(values, 0.9):>10.0f}ms "
                  f"{max(values):>10.0f}ms", end="")
        if len(names) == 2:
            print(f" {medians[0] / medians[1]:>9.2f}x", end="")
        print()
    print()


def compare(args) -> int:
    with open(args.before) as fh:
        before = json.load(fh)
    with open(args.after) as fh:
        after = json.load(fh)
    for key in ("seed", "per_band", "reps", "weights", "month"):
        if before["meta"][key] != after["meta"][key]:
            print(f"Refusing to compare: {key} differs between files.")
            return 1
    before_pairs = [r["pair"] for r in before["results"]]
    after_pairs = [r["pair"] for r in after["results"]]
    if before_pairs != after_pairs:
        # Same seed, so the draws are identical; the timed SET differs
        # only if routability changed -- which a pure speed change must
        # never do. Say so loudly and still print what overlaps.
        print(f"WARNING: timed pairs differ ({len(before_pairs)} before, "
              f"{len(after_pairs)} after) -- routability changed between "
              "builds. Comparing the overlap only.")
        shared = set(map(tuple, before_pairs)) & set(map(tuple, after_pairs))
        before["results"] = [r for r in before["results"] if tuple(r["pair"]) in shared]
        after["results"] = [r for r in after["results"] if tuple(r["pair"]) in shared]
    print(f"before: {before['meta']['git_head']} ({before['meta']['measured']})  "
          f"after: {after['meta']['git_head']} ({after['meta']['measured']})")
    print_summary({"before": before, "after": after})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--per-band", type=int, default=DEFAULT_PER_BAND)
    p_run.add_argument("--reps", type=int, default=DEFAULT_REPS)
    p_run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p_run.add_argument("--month", type=int, default=DEFAULT_MONTH)
    p_run.add_argument("--weights", type=lambda s: [float(x) for x in s.split(",")],
                       default=DEFAULT_WEIGHTS)
    p_run.set_defaults(func=run)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")
    p_cmp.set_defaults(func=compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
