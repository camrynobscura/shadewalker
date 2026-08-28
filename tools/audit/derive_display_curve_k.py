"""Lane-profile measurement behind the display curve's k.

WHAT THIS DERIVES
-----------------
The exponent in the display-only transform  displayed = 1-(1-coverage)^k
(the `display-curve` branch; brief in history/display-curve.md).
Displayed shade for a sidewalk is calibrated to the AVERAGE canopy
coverage of the 2m walker strip (config.CANOPY_SAMPLE_STRIP_M, via
fit_exchange_rate.py) -- but a walker drifts to the shadiest walking
line the pavement offers. This measures, citywide: production strip
average vs best 1m-wide lane within a lateral band, per edge, and fits
the k that maps average -> best, length-weighted.

INSTRUMENT VALIDATION (runs first, gate on it): the Mall promenade
(40.77204,-73.97172 -> 40.77020,-73.97242) was measured 2026-08-27 at
centerline ~74.2% / best lane +10m ~91.3%, with a U-shaped lane profile
(elms arch in from the sides; sky-holes over the middle). The lane
reader here must reproduce those within ~4 points on the live route's
geometry (needs the routing server on :8000) or nothing else it says
can be trusted.

BASELINE (2026-08-27 run, seed 20260827, production export; a re-run on
the same data must reproduce these):

  SIDEWALKS (4,000 of 129,073 eligible >= 20m edges, 425.5 km):
    band +/-1.0m  LSQ k = 1.19   weighted median 1.16
    band +/-1.5m  LSQ k = 1.23   weighted median 1.19
    implied k near-constant across coverage deciles (1.15-1.25 above
    20% coverage) -- the functional FORM validated, not just the value.
  PATH-KIND (1,500 of 36,890, 88.6 km):
    band +/-2.0m  k = 1.46      band +/-3.0m  k = 1.72
    (the Mall at +/-10m works out to ~1.79: lane choice grows with
    pavement width; one global k under-serves wide paths by design.)

SAMPLING RULES (project traps baked in): coverage read along WHOLE
lines, never midpoints; all buffering/offsetting in EPSG:2263 (survey
feet), never degrees; length-weighted fits; denominators printed.

    uv run python tools/audit/derive_display_curve_k.py --mall-only
    uv run python tools/audit/derive_display_curve_k.py
"""

import argparse
import gzip
import json
import logging
import math
import os
import random
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.features import geometry_mask  # noqa: E402
from shapely.geometry import LineString  # noqa: E402
from shapely.ops import transform as shp_transform  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.scoring import canopy  # noqa: E402
from pipeline.scoring.blocks import SHADED_KINDS  # noqa: E402
from pipeline.scoring.canopy import CROSSING_KINDS, _M_PER_SURVEY_FT  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("laneprof")

LANE_RADIUS_M = 0.5          # a 1m-wide walking line
SIDEWALK_OFFSETS_M = [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5]
PATH_OFFSETS_M = [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
MALL_OFFSETS_M = [-10.0, -8.0, -6.0, -4.0, -2.0, 0.0,
                  2.0, 4.0, 6.0, 8.0, 10.0]
MIN_EDGE_M = 20.0            # same floor fit_exchange_rate uses
SEED = 20260827

_to_raster = Transformer.from_crs(
    "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform


def strip_fraction(src, geom_ft, radius_m):
    """Canopy fraction of a strip around an ALREADY-PROJECTED line.

    Same window/mask/nodata mechanics as production leaf_fraction, but
    takes EPSG:2263 geometry directly so offset lanes (which only exist
    projected) can be read. None = no reading, distinct from 0.0.
    """
    if geom_ft.is_empty:
        return None
    strip = geom_ft.buffer(radius_m / _M_PER_SURVEY_FT)
    minx, miny, maxx, maxy = strip.bounds
    window = rasterio.windows.from_bounds(minx, miny, maxx, maxy,
                                          src.transform)
    window = window.round_offsets().round_lengths()
    if window.width < 2 or window.height < 2:
        return None
    data = src.read(1, window=window)
    if data.size == 0:
        return None
    covered = geometry_mask([strip], out_shape=data.shape,
                            transform=src.window_transform(window),
                            invert=True)
    values = data[covered]
    values = values[values != 0]
    if values.size < config.CANOPY_MIN_VALID_PIXELS:
        return None
    return float((values == config.CANOPY_RASTER_TREE_CLASS).mean())


def lane_profile(src, coords, offsets_m):
    """{offset_m: canopy fraction of the 1m lane there}, None kept."""
    line_ft = shp_transform(_to_raster, LineString(coords))
    profile = {}
    for off in offsets_m:
        geom = line_ft if off == 0.0 else line_ft.offset_curve(
            off / _M_PER_SURVEY_FT)
        profile[off] = strip_fraction(src, geom, LANE_RADIUS_M)
    return profile


def validate_on_mall(src):
    url = ("http://localhost:8000/route?from_lat=40.77204&from_lon=-73.97172"
           "&to_lat=40.77020&to_lon=-73.97242")
    with urllib.request.urlopen(url, timeout=30) as fh:
        payload = json.load(fh)
    coords = payload["routes"][0]["geometry"]["coordinates"]
    profile = lane_profile(src, coords, MALL_OFFSETS_M)
    print("\n=== MALL VALIDATION (expect center ~74.2, best ~91.3) ===")
    for off in MALL_OFFSETS_M:
        v = profile[off]
        print(f"  offset {off:+5.1f}m  "
              f"{'no reading' if v is None else f'{v * 100:5.1f}%'}")
    center = profile[0.0]
    best = max(v for v in profile.values() if v is not None)
    print(f"  center {center * 100:.1f}%  best {best * 100:.1f}%")
    ok = abs(center - 0.742) < 0.04 and abs(best - 0.913) < 0.04
    print(f"  {'VALIDATED' if ok else 'HARD STOP - does not reproduce'}")
    return ok


def load_edges():
    export_path = config.EXPORT_DIR / "citywide.json.gz"
    logger.info(f"[load] export: {export_path}")
    with gzip.open(export_path, "rt") as fh:
        payload = json.load(fh)
    scores = {(e["u"], e["v"], e["key"]): e for e in payload["edges"]}

    logger.info("[load] OSM read for kind (~2.5 min; the export drops it)")
    from pipeline.fetch.boundaries import fetch_borough_boundaries
    from pipeline.graph import pedestrian
    from pipeline.graph.boundary import nyc_boundary
    nyc_shape = nyc_boundary(fetch_borough_boundaries())
    ped_ways, _ = pedestrian.read_ways(config.OSM_EXTRACT_PATH, nyc_shape)
    _, graph_edges = pedestrian.build_graph(ped_ways)

    sidewalks, paths = [], []
    for edge in graph_edges:
        kind = edge.get("kind")
        rec = scores.get((edge["u"], edge["v"], int(edge["key"])))
        if rec is None or rec["length_m"] < MIN_EDGE_M:
            continue
        if kind in SHADED_KINDS:
            sidewalks.append(rec)
        elif kind not in CROSSING_KINDS:
            paths.append(rec)
    return sidewalks, paths


def measure(src, sample, offsets_m, label):
    rows = []
    skipped_avg = 0
    t0 = time.monotonic()
    for i, rec in enumerate(sample):
        if i and i % 500 == 0:
            logger.info(f"[{label}] {i}/{len(sample)} "
                        f"({time.monotonic() - t0:.0f}s)")
        avg = canopy.leaf_fraction(src, rec["coords"], _to_raster)
        if avg is None:
            skipped_avg += 1
            continue
        profile = lane_profile(src, rec["coords"], offsets_m)
        rows.append({"length_m": rec["length_m"], "avg": avg,
                     "lanes": {str(k): v for k, v in profile.items()}})
    logger.info(f"[{label}] {len(rows)} measured, {skipped_avg} no "
                f"production reading, {time.monotonic() - t0:.0f}s")
    return rows


def best_in_band(row, band_m):
    vals = [v for off, v in row["lanes"].items()
            if v is not None and abs(float(off)) <= band_m + 1e-9]
    return max(vals) if vals else None


def fit_k(rows, band_m):
    """Length-weighted least squares for k on best = 1-(1-avg)^k."""
    pts = []
    for r in rows:
        best = best_in_band(r, band_m)
        if best is None:
            continue
        pts.append((min(r["avg"], 0.999), min(best, 0.999), r["length_m"]))
    best_k, best_err = None, None
    k = 1.0
    while k <= 3.0001:
        err = sum(w * (b - (1 - (1 - a) ** k)) ** 2 for a, b, w in pts)
        if best_err is None or err < best_err:
            best_k, best_err = k, err
        k += 0.01
    return round(best_k, 2), len(pts)


def summarize(rows, band_m, label):
    total_len = sum(r["length_m"] for r in rows)
    k_fit, n_fit = fit_k(rows, band_m)

    weighted = []
    n_below = len_below = 0
    for r in rows:
        best = best_in_band(r, band_m)
        if best is None or not (0.05 <= r["avg"] <= 0.95):
            continue
        if best < r["avg"]:
            n_below += 1
            len_below += r["length_m"]
        kk = math.log(1 - min(best, 0.995)) / math.log(1 - r["avg"])
        weighted.append((kk, r["length_m"]))
    med_k = None
    if weighted:
        weighted.sort()
        half = sum(w for _, w in weighted) / 2
        acc = 0.0
        for kk, w in weighted:
            acc += w
            if acc >= half:
                med_k = kk
                break

    print(f"\n=== {label}, band +/-{band_m}m ===")
    print(f"  edges {len(rows)} ({total_len / 1000:.1f} km); "
          f"fit over {n_fit} with a lane reading")
    print(f"  length-weighted LSQ k = {k_fit}")
    print(f"  length-weighted median per-edge k = "
          f"{med_k:.2f} (n={len(weighted)}, stable-coverage 5-95% only)")
    print(f"  best<avg on {n_below} edges / {len_below / 1000:.1f} km "
          f"(lane band narrower than the 2m strip; expected sometimes)")

    print("  decile  n     km    mean avg  mean best  implied k")
    for lo in range(0, 10):
        sub = [(r, best_in_band(r, band_m)) for r in rows
               if lo / 10 <= r["avg"] < (lo + 1) / 10]
        sub = [(r, b) for r, b in sub if b is not None]
        if not sub:
            continue
        km = sum(r["length_m"] for r, _ in sub) / 1000
        wl = sum(r["length_m"] for r, _ in sub)
        m_avg = sum(r["avg"] * r["length_m"] for r, _ in sub) / wl
        m_best = sum(b * r["length_m"] for r, b in sub) / wl
        ik = ""
        if 0.01 < m_avg < 0.99 and m_best < 0.999:
            ik = f"{math.log(1 - m_best) / math.log(1 - m_avg):9.2f}"
        print(f"  {lo / 10:.1f}-{(lo + 1) / 10:.1f} {len(sub):5d} {km:6.1f}"
              f"    {m_avg * 100:5.1f}%    {m_best * 100:5.1f}%  {ik}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mall-only", action="store_true")
    parser.add_argument("--sidewalk-sample", type=int, default=4000)
    parser.add_argument("--path-sample", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        ok = validate_on_mall(src)
        if args.mall_only:
            return 0 if ok else 1
        if not ok:
            print("instrument failed validation; not proceeding")
            return 1

        sidewalks, paths = load_edges()
        print(f"\neligible: {len(sidewalks):,} sidewalk edges "
              f"({sum(r['length_m'] for r in sidewalks) / 1000:,.0f} km), "
              f"{len(paths):,} path-kind edges "
              f"({sum(r['length_m'] for r in paths) / 1000:,.0f} km), "
              f"both >= {MIN_EDGE_M}m")

        rng = random.Random(args.seed)
        sw_sample = rng.sample(sidewalks, min(args.sidewalk_sample,
                                              len(sidewalks)))
        pa_sample = rng.sample(paths, min(args.path_sample, len(paths)))

        sw_rows = measure(src, sw_sample, SIDEWALK_OFFSETS_M, "sidewalk")
        pa_rows = measure(src, pa_sample, PATH_OFFSETS_M, "path")

    summarize(sw_rows, 1.0, "SIDEWALKS")
    summarize(sw_rows, 1.5, "SIDEWALKS")
    summarize(pa_rows, 2.0, "PATH-KIND")
    summarize(pa_rows, 3.0, "PATH-KIND")
    return 0


if __name__ == "__main__":
    sys.exit(main())
