"""Gate 1 instrument for the building-shade engine: is it right, and what
does it cost citywide?

WHY THIS EXISTS
---------------
pipeline/scoring/shadows.py answers "what share of this edge is in a
building's shadow" for 288 (month, hour) slots. Before it runs on 488k
edges and 1.08M footprints, the user decides (PLAN `building-shadows`,
Gate 1) on MEASUREMENTS: engine accepted or not, sample step, grid cell,
slot set, reach, build-time budget. This produces them on five test
areas, one per borough (the boroughs are too different for one pilot --
BUILDING-SHADOWS.md section 4.6 has their crude verdicts).

VALIDATE FIRST (the instrument rule)
------------------------------------
The 2026-09-08 numbers were produced by a crude engine: all-vector, 5 m
slices, ONE point per slice, sidewalk points only, hand-computed sun,
buildings FULLY INSIDE the area bbox only (Socrata within_box), rays no
longer than the area's tallest building / tan(el). Part 1 reproduces
exactly that computation with today's exact sweep engine. If those
numbers do not come back, the instrument is wrong and nothing after it
can be believed. (First run, 2026-09-24: four areas within 0.7 points;
Midtown read 2-4.5 points HIGH until the building set was matched --
the crude run had dropped every tower straddling or outside its 1 km
box, which is what shades Midtown at low sun. Matched, all five
reproduce to 0.1 point. The production run uses buildings within
SHADOW_MAX_REACH_M, the right set.)

WHAT IT MEASURES (per area)
---------------------------
  2. The production run (score_building_shade, every daylight slot, the
     real sun table) at each --config "cell/hop": wall time, seconds per
     slot, peak RSS growth. The design's 2/2 and the chosen 1/0.5 by
     default.
  3. Raster-vs-exact agreement per point at three slots, per config: the
     positional error of the march, as a share of points and as the
     shaded fraction each engine reports.
  4. Shaded fraction by edge kind at those slots (crossings included).
  5. Known-normal checks: edges with no wall within 30 m read ~0 at noon;
     at solar noon the sidewalk on the SOUTH side of an E-W street (whose
     buildings stand to its south) is shaded more than the north side's;
     inside-footprint share.
  6. Extrapolation to the citywide edge set, from the measured seconds
     per slot per point and the exact citywide point count at sample
     steps 2/5/10 m -- an ESTIMATE, labelled so, single-threaded.

--dump writes per-edge records (probe point, kind, side, values at the
three slots) for a Street View batch.

    uv run python tools/audit/measure_shadow_feasibility.py
    uv run python tools/audit/measure_shadow_feasibility.py --areas pilot,midtown --configs 1/0.5
"""

import argparse
import collections
import datetime as dt
import gzip
import json
import logging
import math
import os
import resource
import sys
import time

import numpy as np
import shapely
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config, sun                       # noqa: E402
from pipeline.config import Bbox                       # noqa: E402
from pipeline.fetch import buildings                   # noqa: E402
from pipeline.graph.naming import _to_m                # noqa: E402
from pipeline.scoring import shadows                   # noqa: E402

logger = logging.getLogger("measure_shadow_feasibility")

# The five areas of BUILDING-SHADOWS.md section 4.6, and the crude verdicts
# recorded there (sidewalk points shaded, %; crossings where recorded).
AREAS = {
    "midtown": Bbox(lat_min=40.750, lat_max=40.760, lon_min=-73.990, lon_max=-73.978),
    "bronx": Bbox(lat_min=40.845, lat_max=40.855, lon_min=-73.910, lon_max=-73.898),
    "queens": Bbox(lat_min=40.744, lat_max=40.756, lon_min=-73.896, lon_max=-73.880),
    "pilot": config.PILOT_BBOX,
    "staten": Bbox(lat_min=40.635, lat_max=40.645, lon_min=-74.085, lon_max=-74.070),
}
# (label, hand-computed azimuth, elevation, {area: crude sidewalk %}, {area: crude crossing %})
CRUDE_SLOTS = [
    ("July noon", 180.0, 70.8,
     {"midtown": 62.4, "bronx": 16.0, "queens": 6.8, "pilot": 20.4, "staten": 6.6},
     {"pilot": 3.5, "midtown": 39.2}),
    ("July ~4pm", 257.0, 47.6,
     {"midtown": 87.8, "bronx": 38.4, "queens": 31.3, "pilot": 37.9, "staten": 19.6},
     {"pilot": 18.1, "midtown": 77.6}),
    ("Jan noon", 180.0, 28.6,
     {"midtown": 92.4, "bronx": 45.6, "queens": 34.5, "pilot": 57.9, "staten": 33.1},
     {"pilot": 43.7, "midtown": 87.8}),
]
# The anchor slots nearest those three, for the production-table checks.
CHECK_SLOTS = [("July 13:00", 7, 13), ("July 16:00", 7, 16), ("Jan 12:00", 1, 12)]

CRUDE_STEP_M = 5.0
NO_WALL_M = 30.0


def _rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def _bearing_deg(edge) -> float:
    (x0, y0), (x1, y1) = _to_m(*edge["coords"][0]), _to_m(*edge["coords"][-1])
    return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 180.0


def _in_bbox(coords, b: Bbox) -> bool:
    return all(b.lon_min <= lon <= b.lon_max and b.lat_min <= lat <= b.lat_max
               for lon, lat in coords)


def _near_bbox(row, b: Bbox, pad_deg: float) -> bool:
    lon, lat = row["the_geom"]["coordinates"][0][0][0]
    return (b.lon_min - pad_deg <= lon <= b.lon_max + pad_deg
            and b.lat_min - pad_deg <= lat <= b.lat_max + pad_deg)


def citywide_points(all_edges, step_m: float, across: int) -> int:
    """Exactly what sample_points would produce citywide at this step."""
    return sum(max(1, math.ceil(e["length_m"] / step_m)) for e in all_edges if e["length_m"] > 0) * across


def _mean_pct(values) -> str:
    return f"{100 * float(np.mean(values)):.1f}%" if len(values) else "n/a"


# ── one area ─────────────────────────────────────────────────────────────────

def measure_area(name, bbox, all_edges, usable_rows, table, configs, dump_dir, report):
    edges = [e for e in all_edges if _in_bbox(e["coords"], bbox)]
    pad_deg = config.SHADOW_MAX_REACH_M / 80_000    # ~1 km, generous
    rows = [r for r in usable_rows if _near_bbox(r, bbox, pad_deg)]
    kinds = collections.Counter(e["kind"] for e in edges)
    report.append(f"## {name} -- {len(edges):,} edges, {len(rows):,} buildings within reach\n")
    report.append("kinds: " + ", ".join(f"{k} {n:,}" for k, n in kinds.most_common(6)) + "\n")

    t0 = time.perf_counter()
    geoms, heights = shadows.prepare_buildings(rows)
    prep_s = time.perf_counter() - t0
    building_tree = STRtree(geoms)
    sidewalk = np.array([e["kind"] == "footway/sidewalk" for e in edges])
    crossing = np.array([e["kind"] == "footway/crossing" for e in edges])

    # ── 1. reproduce the crude engine ──────────────────────────────────────
    bbox_geom = shapely.box(*_to_m(bbox.lon_min, bbox.lat_min), *_to_m(bbox.lon_max, bbox.lat_max))
    crude_sel = np.array([bbox_geom.contains(g) for g in geoms])
    crude_geoms, crude_heights = geoms[crude_sel], heights[crude_sel]
    crude_tree = STRtree(crude_geoms)
    pts1, owner1 = shadows.sample_points(edges, CRUDE_STEP_M, (0.0,))
    pg1 = shapely.points(pts1)
    inside1 = np.zeros(len(pts1), bool)
    inside1[crude_tree.query(pg1, predicate="within")[0]] = True
    ptree1 = STRtree(pg1)
    report.append("### 1. Validation -- the 2026-09-08 crude computation, reproduced\n")
    report.append(f"Crude building set: {int(crude_sel.sum()):,} footprints fully inside the bbox "
                  f"(production uses {len(geoms):,} within reach).\n")
    report.append("| slot | crude sidewalks | now | crude crossings | now |")
    report.append("|---|---|---|---|---|")
    worst = 0.0
    for label, az, el, crude_sw, crude_cx in CRUDE_SLOTS:
        crude_reach = float(crude_heights.max()) / math.tan(math.radians(el))
        shaded = shadows.sweep_shaded(ptree1, len(pts1), crude_geoms, crude_heights, az, el, crude_reach)
        sw = shaded[sidewalk[owner1] & ~inside1]
        cx = shaded[crossing[owner1] & ~inside1]
        now_sw, now_cx = 100 * sw.mean(), 100 * cx.mean()
        worst = max(worst, abs(now_sw - crude_sw[name]))
        cx_crude = f"{crude_cx[name]:.1f}%" if name in crude_cx else "-"
        report.append(f"| {label} | {crude_sw[name]:.1f}% | {now_sw:.1f}% | {cx_crude} | {now_cx:.1f}% |")
    report.append(f"\nWorst sidewalk difference {worst:.1f} points "
                  f"({'REPRODUCED' if worst <= 1.5 else 'NOT REPRODUCED -- stop here'}).\n")

    # ── production points, exact reference at the check slots ─────────────
    pts, owner = shadows.sample_points(edges)
    pg = shapely.points(pts)
    inside = np.zeros(len(pts), bool)
    inside[building_tree.query(pg, predicate="within")[0]] = True
    ok = ~inside
    ptree = STRtree(pg)
    exact = {}
    t_exact = 0.0
    for label, m, h in CHECK_SLOTS:
        az, el = table[m - 1][h]
        t0 = time.perf_counter()
        exact[label] = shadows.sweep_shaded(ptree, len(pts), geoms, heights, az, el,
                                            config.SHADOW_MAX_REACH_M)
        t_exact += time.perf_counter() - t0
    t_exact /= len(CHECK_SLOTS)

    # ── 2 + 3. production runs per config, agreement vs exact ──────────────
    report.append("### 2. Production run (all daylight slots, real sun table) per grid config\n")
    report.append("| cell / hop | wall s | s per slot | grid MB (this area) | edges with shade | inside footprints |")
    report.append("|---|---|---|---|---|---|")
    per_slot = {}
    tallies = {}
    x0, y0 = pts[:, 0].min() - config.SHADOW_MAX_REACH_M, pts[:, 1].min() - config.SHADOW_MAX_REACH_M
    x1, y1 = pts[:, 0].max() + config.SHADOW_MAX_REACH_M, pts[:, 1].max() + config.SHADOW_MAX_REACH_M
    agreement_rows = []
    for cell, hop in configs:
        t0 = time.perf_counter()
        tally = shadows.score_building_shade(edges, (geoms, heights), table, cell_m=cell, march_step_m=hop)
        wall = time.perf_counter() - t0
        per_slot[(cell, hop)] = tally["seconds_per_slot"]
        tallies[(cell, hop)] = tally
        grid_mb = ((x1 - x0) / cell) * ((y1 - y0) / cell) * 4 / 1e6
        report.append(f"| {cell:g} / {hop:g} | {wall:.0f} | {tally['seconds_per_slot']:.3f} | "
                      f"{grid_mb:.0f} | {tally['edges_with_shade']:,} / {tally['edges']:,} | "
                      f"{tally['points_inside_footprints']:,} / {tally['points']:,} "
                      f"({100 * tally['points_inside_footprints'] / max(tally['points'], 1):.2f}%) |")
        grid, gx0, gy = shadows.build_height_grid(geoms, heights, (x0, y0, x1, y1), cell)
        tall = heights > config.SHADOW_RASTER_HEIGHT_CAP_M
        for label, m, h in CHECK_SLOTS:
            az, el = table[m - 1][h]
            r = shadows.raster_shaded(pts, grid, gx0, gy, cell, az, el, hop,
                                      config.SHADOW_RASTER_HEIGHT_CAP_M, config.SHADOW_MAX_REACH_M)
            if tall.any():
                r |= shadows.sweep_shaded(ptree, len(pts), geoms[tall], heights[tall], az, el,
                                          config.SHADOW_MAX_REACH_M)
            e = exact[label]
            agreement_rows.append(
                f"| {cell:g} / {hop:g} | {label} | {100 * (r[ok] == e[ok]).mean():.1f}% | "
                f"{_mean_pct(e[ok & sidewalk[owner]])} | {_mean_pct(r[ok & sidewalk[owner]])} | "
                f"{int((e[ok] & ~r[ok]).sum()):,} | {int((r[ok] & ~e[ok]).sum()):,} |")
        del grid
    report.append(f"\nprepare_buildings {prep_s:.1f}s for {len(rows):,} footprints; exact sweep "
                  f"{t_exact:.1f}s per slot on this area (engine V's cost).\n")
    report.append("### 3. Raster march vs exact sweep, per point, three-across production points\n")
    report.append("| cell / hop | slot | points agree | sidewalks exact | sidewalks raster | under | over |")
    report.append("|---|---|---|---|---|---|---|")
    report.extend(agreement_rows)
    report.append("")

    # ── 4. by kind, from the last (chosen) config's table ──────────────────
    arr = np.frombuffer(b"".join(e["building_shade"] for e in edges), dtype=np.uint8).reshape(len(edges), 288)
    report.append(f"### 4. Shaded fraction by kind (config {configs[-1][0]:g} / {configs[-1][1]:g}, edge means)\n")
    header = "| kind | n | " + " | ".join(l for l, _, _ in CHECK_SLOTS) + " |"
    report.append(header)
    report.append("|---|---|" + "---|" * len(CHECK_SLOTS))
    for kind, n in kinds.most_common(8):
        mask = np.array([e["kind"] == kind for e in edges])
        cells = " | ".join(f"{arr[mask, (m - 1) * 24 + h].mean() / 255:.1%}" for _, m, h in CHECK_SLOTS)
        report.append(f"| {kind} | {n:,} | {cells} |")
    report.append("")

    # ── 5. known-normal checks ─────────────────────────────────────────────
    report.append("### 5. Known-normal checks\n")
    nearest = building_tree.query_nearest(pg, max_distance=NO_WALL_M, return_distance=False, all_matches=False)
    has_wall = np.zeros(len(pts), bool)
    has_wall[nearest[0]] = True
    no_wall_edges = np.array([not has_wall[owner == i].any() if (owner == i).any() else False
                              for i in range(len(edges))])
    noon = (7 - 1) * 24 + 13
    if no_wall_edges.any():
        report.append(f"- edges with no wall within {NO_WALL_M:g} m of any sample point: {no_wall_edges.sum():,}; "
                      f"their July 13:00 mean {arr[no_wall_edges, noon].mean() / 255:.1%} (expect ~0)")
    else:
        report.append(f"- no edge in this area is more than {NO_WALL_M:g} m from a wall (check skipped)")
    ew = np.array([abs(_bearing_deg(e) - 90) <= 30 for e in edges])
    south = np.array([e.get("side") == "S" for e in edges]) & sidewalk & ew
    north = np.array([e.get("side") == "N" for e in edges]) & sidewalk & ew
    if south.any() and north.any():
        s_mean, n_mean = arr[south, noon].mean() / 255, arr[north, noon].mean() / 255
        report.append(f"- solar noon, E-W streets: sidewalks on the SOUTH side (buildings to their south) "
                      f"{s_mean:.1%} (n={south.sum()}) vs NORTH side {n_mean:.1%} (n={north.sum()}) -- "
                      f"{'PASS' if s_mean > n_mean else 'FAIL'}")
    else:
        report.append("- solar noon side check skipped: no E-W sidewalks with both sides in this area")
    low = (1 - 1) * 24 + 12
    report.append(f"- lowest-sun anchor (Jan 12:00) sidewalk mean {arr[sidewalk, low].mean() / 255:.1%}; "
                  f"share of sidewalks fully shaded {(arr[sidewalk, low] == 255).mean():.1%}")
    report.append("")

    # ── 5b. sample step: 2 m vs the production 5 m, per edge, chosen config ──
    cell, hop = configs[-1]
    fine_edges = [dict(e) for e in edges]
    t0 = time.perf_counter()
    fine_tally = shadows.score_building_shade(fine_edges, (geoms, heights), table, step_m=2.0, cell_m=cell, march_step_m=hop)
    fine_s = time.perf_counter() - t0
    fine = np.frombuffer(b"".join(e["building_shade"] for e in fine_edges), dtype=np.uint8).reshape(len(edges), 288)
    report.append(f"### 5b. Sample step 2 m vs {config.SHADOW_SAMPLE_STEP_M:g} m along the edge "
                  f"(config {cell:g} / {hop:g}; {fine_tally['points']:,} vs {tallies[(cell, hop)]['points']:,} points, "
                  f"{fine_s:.0f}s vs {wall:.0f}s)\n")
    report.append("| slot | mean abs diff (of 255) | edges differing > 13 (5%) | > 26 (10%) | > 64 (25%) |")
    report.append("|---|---|---|---|---|")
    for label, m, h in CHECK_SLOTS:
        col = (m - 1) * 24 + h
        diff = np.abs(fine[:, col].astype(int) - arr[:, col].astype(int))
        report.append(f"| {label} | {diff.mean():.1f} | {(diff > 13).mean():.1%} | {(diff > 26).mean():.1%} | "
                      f"{(diff > 64).mean():.1%} |")
    report.append("")

    if dump_dir:
        records = []
        for i, e in enumerate(edges):
            probe = e["coords"][len(e["coords"]) // 2]
            records.append({"kind": e["kind"], "side": e.get("side", ""), "name": e.get("name", ""),
                            "lon": probe[0], "lat": probe[1],
                            **{l: int(arr[i, (m - 1) * 24 + h]) for l, m, h in CHECK_SLOTS}})
        with open(os.path.join(dump_dir, f"shadow_edges_{name}.json"), "w") as fh:
            json.dump(records, fh)

    return {"edges": len(edges), "points": len(pts), "per_slot": per_slot, "worst_validation": worst}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--areas", default=",".join(AREAS))
    parser.add_argument("--configs", default="2/2,1/0.5",
                        help="comma list of cell/hop metres; the LAST is the chosen one used for sections 4-5")
    parser.add_argument("--out-dir", default=f"data/audits/{dt.date.today().isoformat()}")
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    configs = [tuple(float(x) for x in c.split("/")) for c in args.configs.split(",")]
    os.makedirs(args.out_dir, exist_ok=True)

    t0 = time.perf_counter()
    export_path = config.EXPORT_DIR / "citywide.json.gz"
    with gzip.open(export_path, "rt") as fh:
        payload = json.load(fh)
    all_edges = payload["edges"]
    usable_rows = buildings.usable(buildings.load())
    table = sun.sun_table()
    daylight = len(sun.daylight_slots(table))
    logger.info(f"loaded {len(all_edges):,} edges + {len(usable_rows):,} buildings in {time.perf_counter() - t0:.0f}s; "
                f"RSS {_rss_mb():.0f} MB")

    report = [f"# Shadow feasibility -- {dt.date.today().isoformat()}\n",
              f"Export `{export_path}` ({len(all_edges):,} edges, {payload['meta'].get('created')}); "
              f"buildings cache v{config.BUILDINGS_CACHE_VERSION} ({len(usable_rows):,} usable); "
              f"{daylight} daylight slots; sample step {config.SHADOW_SAMPLE_STEP_M:g} m x "
              f"{len(config.SHADOW_STRIP_OFFSETS_M)} across; reach {config.SHADOW_MAX_REACH_M:g} m; "
              f"raster cap {config.SHADOW_RASTER_HEIGHT_CAP_M:.1f} m; configs {args.configs}. "
              f"Machine: {os.uname().machine}, {os.cpu_count()} CPUs.\n"]

    results = {}
    for name in args.areas.split(","):
        logger.info(f"=== {name}")
        results[name] = measure_area(name, AREAS[name], all_edges, usable_rows, table, configs,
                                     args.out_dir if args.dump else None, report)

    # ── 6. extrapolation ───────────────────────────────────────────────────
    report.append("## 6. Citywide extrapolation (ESTIMATE, single-threaded, from measured s/slot/point)\n")
    across = len(config.SHADOW_STRIP_OFFSETS_M)
    report.append("| step m | citywide points | " + " | ".join(f"{c:g}/{h:g} hours" for c, h in configs) + " |")
    report.append("|---|---|" + "---|" * len(configs))
    for step in (2.0, 5.0, 10.0):
        n_points = citywide_points(all_edges, step, across)
        cells = []
        for cfg in configs:
            # seconds per slot per point, averaged over areas weighted by their points
            total_s = sum(r["per_slot"][cfg] for r in results.values())
            total_p = sum(r["points"] for r in results.values())
            cells.append(f"{total_s / total_p * n_points * daylight / 3600:.1f}")
        report.append(f"| {step:g} | {n_points:,} | " + " | ".join(cells) + " |")
    report.append(f"\nPlus per-tile grid building and prepare_buildings for 1.08M footprints "
                  f"(measured per area above, scale by footprint count). Validation worst "
                  f"differences: " + ", ".join(f"{n} {r['worst_validation']:.1f}" for n, r in results.items()) + ".\n")

    text = "\n".join(report)
    path = os.path.join(args.out_dir, "shadow_feasibility_report.md")
    with open(path, "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nreport: {path}   (peak RSS {_rss_mb():.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
