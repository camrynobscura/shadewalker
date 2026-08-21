"""Census pass A: EXHAUSTIVE near-coincident pair scan + OSRM verdicts.

The invariant scan (tests/test_citywide_invariants.py) samples 0.7% of
nodes; this is the same detector run over EVERY main-component node:
  flag any node pair <= 30m apart in reality (100m when either end is
  degree-1, the dead-end sweep's radius) whose graph walk is
  >= max(8x real, 200m) -- then ask local OSRM for a verdict on every
  flagged pair.

Output keyed by node ids AND coords so the post-rebuild re-census can
diff on coords (ids churn across rebuilds).

Usage: uv run python data/audits/census/census_pass_a.py [out.json]
Needs local OSRM up on :5001 (see census_common).
"""
import math
import sys
from collections import defaultdict

import numpy as np
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree


from census_common import (haversine_m, load_store, local_meters_xy,
                           main_component_mask, osrm_route_m, save_json,
                           store_csr)

RADIUS_M = 30.0
DEADEND_RADIUS_M = 100.0
FLAG_FACTOR = 8.0
FLAG_MIN_M = 200.0


def main():
    from census_common import CENSUS_RESULTS
    out_path = sys.argv[1] if len(sys.argv) > 1 else (
        f"{CENSUS_RESULTS}/pass_a_baseline_v25.json")
    store = load_store()
    csr = store_csr(store)
    mask = main_component_mask(store)
    xy = local_meters_xy(store)
    ll = store._node_lonlat
    deg = np.array(store._graph.degree())
    idx_main = np.where(mask)[0]
    print(f"{len(idx_main)} main-component nodes", flush=True)

    tree = cKDTree(xy[idx_main])
    # 30m pairs among everyone
    pairs = tree.query_pairs(RADIUS_M, output_type="ndarray")
    print(f"{len(pairs)} pairs <= {RADIUS_M}m", flush=True)
    # 100m pairs where either end is degree-1
    deg1_local = np.where(deg[idx_main] == 1)[0]
    extra = []
    if len(deg1_local):
        d1tree = cKDTree(xy[idx_main][deg1_local])
        hits = d1tree.query_ball_tree(tree, DEADEND_RADIUS_M)
        for li, near in zip(deg1_local, hits):
            for j in near:
                if j != li:
                    extra.append((min(li, j), max(li, j)))
    extra = np.array(sorted(set(map(tuple, extra))), dtype=np.int64) \
        if extra else np.empty((0, 2), dtype=np.int64)
    print(f"{len(extra)} extra dead-end pairs <= {DEADEND_RADIUS_M}m",
          flush=True)
    all_pairs = {tuple(p) for p in pairs.tolist()}
    all_pairs.update(map(tuple, extra.tolist()))
    print(f"{len(all_pairs)} unique pairs total", flush=True)

    # group by source, bounded dijkstra per source
    by_src = defaultdict(list)
    for a, b in all_pairs:
        ga, gb = int(idx_main[a]), int(idx_main[b])
        real = haversine_m(ll[ga][1], ll[ga][0], ll[gb][1], ll[gb][0])
        by_src[ga].append((gb, real))

    flagged = []
    done = 0
    for ga, lst in by_src.items():
        limit = max(max(FLAG_FACTOR * real, FLAG_MIN_M)
                    for _, real in lst) + 10.0
        dists = dijkstra(csr, indices=ga, limit=limit)
        for gb, real in lst:
            g = dists[gb]
            bar = max(FLAG_FACTOR * real, FLAG_MIN_M)
            if math.isinf(g) or g >= bar:
                flagged.append((ga, gb, real, None if math.isinf(g) else g))
        done += 1
        if done % 20000 == 0:
            print(f"  {done}/{len(by_src)} sources, "
                  f"{len(flagged)} flagged", flush=True)
    print(f"{len(flagged)} flagged pairs", flush=True)

    idx_to_id = {v: k for k, v in store._id_to_idx.items()}
    rows = []
    for i, (ga, gb, real, g) in enumerate(flagged):
        a_ll = (float(ll[ga][1]), float(ll[ga][0]))
        b_ll = (float(ll[gb][1]), float(ll[gb][0]))
        osrm, err = osrm_route_m(a_ll, b_ll)
        if osrm is None:
            verdict = ("OSM_BLOCKS" if err in ("NoRoute",)
                       else f"OSRM_{err}")
        elif osrm <= max(250.0, 5.0 * real):
            verdict = "OSM_WALKS_IT"
        elif g is not None and g <= 1.2 * osrm:
            verdict = "WE_MATCH_OSM"
        else:
            verdict = "GREY"
        rows.append({
            "a": idx_to_id[ga], "b": idx_to_id[gb],
            "a_ll": a_ll, "b_ll": b_ll,
            "real_m": round(real, 1),
            "graph_m": None if g is None else round(g, 1),
            "osrm_m": None if osrm is None else round(osrm, 1),
            "verdict": verdict,
        })
        if (i + 1) % 2000 == 0:
            print(f"  osrm {i + 1}/{len(flagged)}", flush=True)

    from collections import Counter
    print(Counter(r["verdict"] for r in rows), flush=True)
    save_json({"radius_m": RADIUS_M, "deadend_radius_m": DEADEND_RADIUS_M,
               "flag_factor": FLAG_FACTOR, "flag_min_m": FLAG_MIN_M,
               "n_pairs_scanned": len(all_pairs), "rows": rows}, out_path)


if __name__ == "__main__":
    main()
