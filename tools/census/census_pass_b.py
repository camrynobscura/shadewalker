"""Census pass B: corridor-scale route-field comparison on a 500m grid.

One origin node per 500m grid cell (main component only); destinations
are the grid nodes nearest to 1km and 2km offsets in 8 compass
directions. Every pair is measured on BOTH engines:
  ours    scipy dijkstra (full, per origin)
  OSRM    local /table (one call per origin)
Flags BOTH directions: ours/osrm >= 1.25 AND ours-osrm >= 300m
(too-long: we're missing something), or the mirror (too-short: we
route where OSM doesn't). ALL pair results are saved keyed by grid
cell + coords so the post-rebuild re-census diffs cleanly.

Usage: uv run python data/audits/census/census_pass_b.py [out.json]
Needs local OSRM on :5001.
"""
import math
import sys

import numpy as np
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree


from census_common import (load_store, local_meters_xy,
                           main_component_mask, osrm_table_m, save_json,
                           store_csr)

CELL_M = 500.0
RINGS_M = (1000.0, 2000.0)
RATIO_BAR = 1.25
ABS_BAR_M = 300.0


def main():
    from census_common import CENSUS_RESULTS
    out_path = sys.argv[1] if len(sys.argv) > 1 else (
        f"{CENSUS_RESULTS}/pass_b_baseline_v25.json")
    store = load_store()
    csr = store_csr(store)
    mask = main_component_mask(store)
    xy = local_meters_xy(store)
    ll = store._node_lonlat
    idx_main = np.where(mask)[0]

    # one representative node per 500m cell: the node closest to center
    cell = np.floor(xy[idx_main] / CELL_M).astype(np.int64)
    reps = {}
    centers = (cell + 0.5) * CELL_M
    d2 = ((xy[idx_main] - centers) ** 2).sum(axis=1)
    for i in range(len(idx_main)):
        key = (int(cell[i][0]), int(cell[i][1]))
        if key not in reps or d2[i] < reps[key][1]:
            reps[key] = (int(idx_main[i]), d2[i])
    rep_nodes = np.array(sorted(g for g, _ in reps.values()))
    print(f"{len(rep_nodes)} grid cells with a main-component node",
          flush=True)

    rep_tree = cKDTree(xy[rep_nodes])
    dirs = [(math.cos(t), math.sin(t))
            for t in np.arange(0, 2 * math.pi, math.pi / 4)]
    pairs = set()
    for ri, g in enumerate(rep_nodes):
        for ring in RINGS_M:
            for dx, dy in dirs:
                target = xy[g] + np.array([dx, dy]) * ring
                dist, j = rep_tree.query(target)
                if dist > CELL_M:      # no cell out there (water etc.)
                    continue
                other = int(rep_nodes[j])
                if other != int(g):
                    pairs.add((min(int(g), other), max(int(g), other)))
    print(f"{len(pairs)} unique probe pairs", flush=True)

    by_src = {}
    for a, b in pairs:
        by_src.setdefault(a, []).append(b)

    rows = []
    n_flag = 0
    for done, (ga, dests) in enumerate(sorted(by_src.items())):
        ours = dijkstra(csr, indices=ga)
        a_ll = (float(ll[ga][1]), float(ll[ga][0]))
        dest_lls = [(float(ll[gb][1]), float(ll[gb][0])) for gb in dests]
        osrm_ds, err = osrm_table_m(a_ll, dest_lls)
        if osrm_ds is None:
            osrm_ds = [None] * len(dests)
        for gb, b_ll, osrm in zip(dests, dest_lls, osrm_ds):
            o = ours[gb]
            o = None if math.isinf(o) else float(o)
            flag = None
            if o is not None and osrm is not None and osrm > 0:
                if o / osrm >= RATIO_BAR and o - osrm >= ABS_BAR_M:
                    flag = "TOO_LONG"
                elif osrm / o >= RATIO_BAR and osrm - o >= ABS_BAR_M:
                    flag = "TOO_SHORT"
            elif o is None and osrm is not None:
                flag = "UNREACHABLE_OURS"
            if flag:
                n_flag += 1
            rows.append({
                "a_ll": [round(a_ll[0], 6), round(a_ll[1], 6)],
                "b_ll": [round(b_ll[0], 6), round(b_ll[1], 6)],
                "ours_m": None if o is None else round(o),
                "osrm_m": None if osrm is None else round(osrm),
                "flag": flag,
            })
        if (done + 1) % 500 == 0:
            print(f"  {done + 1}/{len(by_src)} origins, "
                  f"{n_flag} flags", flush=True)

    from collections import Counter
    print(Counter(r["flag"] for r in rows if r["flag"]), flush=True)
    save_json({"cell_m": CELL_M, "rings_m": RINGS_M,
               "ratio_bar": RATIO_BAR, "abs_bar_m": ABS_BAR_M,
               "n_pairs": len(rows), "rows": rows}, out_path)


if __name__ == "__main__":
    main()
