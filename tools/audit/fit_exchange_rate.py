"""Fit the leaf-cover exchange rate: density = slope x covered fraction.

WHAT THIS DERIVES
-----------------
config.DENSITY_AT_FULL_COVERAGE -- the Forestry density at which pavement
is fully covered by canopy, the single saturation point for routing cost
AND displayed shade. Originally fit 2026-08-25 (25,000 sidewalks, 2m
walker strip, three methods agreeing 0.027-0.033 -> 0.031); that script
was scratchpad-era and wiped. This is the durable rebuild, needed
whenever anything upstream of density changes -- e.g. TREE_ATTACH_MAX_M,
whose 5->8m widening (2026-08-27) is why this file exists.

VALIDATE BEFORE TRUSTING (the instrument rule): run it against the
CURRENT production export first. It must reproduce ~0.031 within the
0.027-0.033 band on data whose answer is known, or the script is wrong
and its new number is worthless.

    uv run python tools/audit/fit_exchange_rate.py                  # validate
    SHADEWALKER_TILES_DIR=/tmp/scratch \\
        uv run python tools/audit/fit_exchange_rate.py              # new fit

METHOD
------
Sample sidewalk edges (the one pavement kind carrying BOTH signals),
compute each edge's peak-season Forestry density and its measured leaf
fraction over the same 2m walker strip production uses
(pipeline/scoring/canopy.py:leaf_fraction), then fit the zero-intercept
slope three ways. Three METHODS, one dataset -- agreement checks the
estimator, not the data (convergence-is-not-correctness, CLAUDE.md);
the independent check is validation against the known 0.031.

Edges with density > 0.2 (6x saturation) are excluded: those are the
fragment-inflation artifacts (a block's trees piled on a metre-scale
drawn scrap -- measured 2026-08-27, 0.4% of drawn length), which are
score pathology, not a density/coverage relationship.
"""

import argparse
import gzip
import json
import logging
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.fetch.boundaries import fetch_borough_boundaries  # noqa: E402
from pipeline.graph import pedestrian  # noqa: E402
from pipeline.graph.boundary import nyc_boundary  # noqa: E402
from pipeline.scoring import canopy  # noqa: E402
from pipeline.scoring.blocks import SHADED_KINDS  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("fit")

DENSITY_OUTLIER = 0.2      # see module docstring
RATIO_FLOOR = 0.2          # density/fraction is unstable below this fraction
AGREEMENT_BAND = (0.027, 0.033)   # the 2026-08-25 three-method spread


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=25_000)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--min-edge-m", type=float, default=20.0)
    args = parser.parse_args()

    started = time.monotonic()
    export_path = config.TILES_DIR / "citywide.json.gz"
    logger.info(f"[fit] export: {export_path}")
    with gzip.open(export_path, "rt") as fh:
        payload = json.load(fh)
    scores = {(e["u"], e["v"], e["key"]): e for e in payload["edges"]}

    logger.info("[fit] OSM read (the export drops `kind`)")
    nyc_shape = nyc_boundary(fetch_borough_boundaries())
    ped_ways, _ = pedestrian.read_ways(config.OSM_EXTRACT_PATH, nyc_shape)
    _, edges = pedestrian.build_graph(ped_ways)
    sidewalks = []
    for edge in edges:
        if edge.get("kind") not in SHADED_KINDS:
            continue
        rec = scores.get((edge["u"], edge["v"], int(edge["key"])))
        if rec is not None and rec["length_m"] >= args.min_edge_m:
            sidewalks.append(rec)
    logger.info(f"[fit] {len(sidewalks):,} sidewalk edges >= "
                f"{args.min_edge_m}m")

    rng = random.Random(args.seed)
    sample = rng.sample(sidewalks, min(args.sample, len(sidewalks)))

    logger.info(f"[fit] raster pass over {len(sample):,} edges")
    to_raster = Transformer.from_crs(
        "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform
    points = []
    outliers = no_reading = 0
    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        for rec in sample:
            fraction = canopy.leaf_fraction(src, rec["coords"], to_raster)
            if fraction is None:
                no_reading += 1
                continue
            density = ((rec["tree_deciduous"] + rec["tree_evergreen"])
                       / rec["length_m"])
            if density > DENSITY_OUTLIER:
                outliers += 1
                continue
            points.append((density, fraction))
    logger.info(f"[fit] {len(points):,} usable ({no_reading:,} no raster "
                f"reading, {outliers:,} fragment-artifact outliers dropped)")

    # Method 1: zero-intercept least squares.
    ols = (sum(d * f for d, f in points)
           / sum(f * f for d, f in points))
    # Method 2: median per-edge ratio, on edges with enough cover for the
    # ratio to be stable.
    ratios = [d / f for d, f in points if f >= RATIO_FLOOR]
    med_ratio = statistics.median(ratios)
    # Method 3: zero-intercept fit through binned medians -- robust to
    # both axes' tails.
    bins: dict[int, list] = {}
    for d, f in points:
        bins.setdefault(min(int(f * 10), 9), []).append(d)
    bin_points = [((b + 0.5) / 10, statistics.median(ds))
                  for b, ds in sorted(bins.items()) if len(ds) >= 50]
    binned = (sum(f * d for f, d in bin_points)
              / sum(f * f for f, d in bin_points))

    print(f"\nzero-intercept OLS      {ols:.4f}   (n={len(points):,})")
    print(f"median ratio (f>={RATIO_FLOOR})   {med_ratio:.4f}   "
          f"(n={len(ratios):,})")
    print(f"binned-median slope     {binned:.4f}   "
          f"({len(bin_points)} bins)")
    lo, hi = min(ols, med_ratio, binned), max(ols, med_ratio, binned)
    print(f"\nspread {lo:.4f}-{hi:.4f}; original-band check "
          f"{AGREEMENT_BAND[0]}-{AGREEMENT_BAND[1]}: "
          f"{'INSIDE' if lo >= AGREEMENT_BAND[0] and hi <= AGREEMENT_BAND[1] else 'OUTSIDE'}")
    print(f"(validation mode: against the production export this must "
          f"reproduce ~0.031 or the script is wrong)")
    logger.info(f"[fit] done in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
