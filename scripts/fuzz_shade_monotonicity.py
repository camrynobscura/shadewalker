"""Fuzz the shade-priority invariant across the full citywide graph.

Unlike the committed tests (seeded, bounded, CI-safe), this is a standalone
tool for occasional broad sweeps: it draws a different random sample each run,
so over time it exercises far more of the city than any one fixed seed. To
stay reproducible it prints the seed it used, and on any failure prints the
exact failing routes -- rerun with --seed <n> to replay a red result.

    uv run python scripts/fuzz_shade_monotonicity.py              # 500 routes, fresh random seed
    uv run python scripts/fuzz_shade_monotonicity.py --n 2000     # a bigger sweep
    uv run python scripts/fuzz_shade_monotonicity.py --seed 12345 # replay a specific run

Not part of pytest/CI: it needs the real (gitignored) data/export/
citywide data. Exits nonzero if it finds any route where raising Shade_priority lowers
shade_fraction -- which the clamp should make impossible.
"""
import argparse
import random
import sys
from pathlib import Path

# Make the script runnable as `python scripts/fuzz_...py` from anywhere:
# put the repo root (this file's grandparent) on the path so `pipeline`/
# `server` import, the same way pyproject's pythonpath=["."] does for pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config
from server.graph_store import GraphStore, _local_distance_m, clamp_shade_monotonic

WEIGHTS = [0.0, 5.0, 15.0, 40.0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--n", type=int, default=500, help="routes to sample (default 500)")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed; omit for a fresh random sample (the seed is printed either way)")
    parser.add_argument("--months", default="4,7,10",
                        help="comma-separated months to test each route in (default 4,7,10)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    seed = args.seed if args.seed is not None else random.randrange(2**31)
    months = [int(m) for m in args.months.split(",")]
    print(f"seed={seed}  n={args.n}  months={months}  (replay a red run with --seed {seed})")

    exports = sorted(config.EXPORT_DIR.glob("*.json.gz"))
    if not exports:
        print(
            f"ERROR: no citywide export in {config.EXPORT_DIR}. "
            "Run the pipeline first: `uv run python -m pipeline.build`.",
            file=sys.stderr,
        )
        return 2

    store = GraphStore()
    store.load()
    nodes = list(max(store._graph.connected_components(mode="weak"), key=len))
    rng = random.Random(seed)

    violations = []
    routed = 0
    attempts = 0
    while routed < args.n and attempts < args.n * 60:
        attempts += 1
        a, b = rng.choice(nodes), rng.choice(nodes)
        if a == b:
            continue
        lon_a, lat_a = store._node_lonlat[a]
        lon_b, lat_b = store._node_lonlat[b]
        if not (500.0 <= _local_distance_m(lat_a, lon_a, lat_b, lon_b) <= 2500.0):
            continue
        pair = store.snap_pair(lat_a, lon_a, lat_b, lon_b)
        if pair is None:
            continue
        start, end = pair
        routed_any = False
        for month in months:
            results = [store.route(start, end, tree_weight=w, month=month) for w in WEIGHTS]
            if any(result is None for result in results):
                continue
            routed_any = True
            shades = [r["shade_fraction"] for r in clamp_shade_monotonic(results, WEIGHTS)]
            for i in range(1, len(shades)):
                if shades[i] < shades[i - 1] - 1e-9:
                    violations.append((month, (lat_a, lon_a), (lat_b, lon_b), shades))
                    break
        if routed_any:
            routed += 1

    print(f"routed {routed} routes x {len(months)} months")
    if violations:
        print(f"\nFAIL: {len(violations)} route(s) where more shade priority reduced shade "
              f"(rerun with --seed {seed} to reproduce):")
        for month, a, b, shades in violations[:20]:
            print(f"  month={month} from={a[0]:.5f},{a[1]:.5f} to={b[0]:.5f},{b[1]:.5f} "
                  f"shades={[round(x, 3) for x in shades]}")
        return 1

    print("PASS: shade never decreased as Shade_priority rose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
