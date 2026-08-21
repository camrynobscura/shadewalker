"""Census triage: re-score, cluster, and ATTRIBUTE the raw census flags.

Pass A's in-run verdicts were mis-ordered (agreement was tested after
"OSM walks it", so agreeing pairs inflated the miss count) and used a
200m bar below the project's ~300m harm threshold. All raw measurements
were saved, so this re-scores offline:

  AGREE        g <= 1.2x osrm, or g - osrm < 300m  (no harm)
  MISSING_CONN osrm <= max(250, 5x real) AND g - osrm >= 300m
               (OSM connects them; we detour 300m+ extra)
  GREY_MID     everything else with both measurements
  OSRM_SNAP    OSRM couldn't measure the pair (kept, unranked)

MISSING_CONN pairs cluster into SITES (shared node or midpoints within
100m), ranked by worst g - osrm. Each site is attributed:
  T1        within 150m of an admitted T1 sidewalk connector
  FEE_ZONE  within 100m of a fee-gated zone polygon (entrance stubs)
  OPEN      neither -- the census's genuinely new leads

Pass B flags get the same attribution, plus TOO_SHORT rows are traced
through our actual shortest path: traversing a curated gap bridge or an
imported synthetic edge explains the shortcut BY DESIGN; the rest are
over-permissiveness leads (the Todt Hill class).
"""
import json
import math
import sys
from collections import Counter, defaultdict

import numpy as np
from scipy.spatial import cKDTree


from census_common import (REPO, CENSUS_RESULTS, haversine_m,
                           load_store, save_json)

CENSUS = CENSUS_RESULTS
# dated dir holding the T1 survey outputs the attribution reads
AUDITS = f"{REPO}/data/audits/2026-08-20"
# which run to triage: pass_a_<RUN>.json / pass_b_<RUN>.json -> <RUN>_triage.json
RUN = sys.argv[1] if len(sys.argv) > 1 else "baseline_v25"
HARM_M = 300.0
SITE_JOIN_M = 100.0
T1_NEAR_M = 150.0
ZONE_NEAR_M = 100.0


def meters_xy(lat, lon):
    lat0 = math.radians(40.7)
    return (lon * 111320.0 * math.cos(lat0), lat * 110540.0)


def build_t1_tree():
    d = json.load(open(f"{AUDITS}/sidewalk_admit_survey_final.json"))
    pts = [meters_xy(c["lat"], c["lon"]) for c in d["T1_150_list"]]
    return cKDTree(np.array(pts)) if pts else None


def build_zone_polys():
    from shapely.geometry import Polygon
    zones = json.load(open(
        f"{REPO}/pipeline/fee_gated_zones.json"))["zones"]
    return [Polygon(z["polygon"]).buffer(ZONE_NEAR_M / 111000.0)
            for z in zones]


def near_zone(zpolys, lat, lon):
    from shapely.geometry import Point
    p = Point(lon, lat)
    return any(poly.contains(p) for poly in zpolys)


def rescore_pass_a(t1_tree, zpolys):
    data = json.load(open(f"{CENSUS}/pass_a_{RUN}.json"))
    rows = data["rows"]
    scored = Counter()
    missing = []
    for r in rows:
        g = r["graph_m"] if r["graph_m"] is not None else float("inf")
        osrm = r["osrm_m"]
        if osrm is None:
            v = "OSRM_SNAP" if str(r["verdict"]).startswith("OSRM_snap") \
                else "OSRM_OTHER"
        elif g <= 1.2 * osrm or g - osrm < HARM_M:
            v = "AGREE"
        elif osrm <= max(250.0, 5.0 * r["real_m"]):
            v = "MISSING_CONN"
        else:
            v = "GREY_MID"
        scored[v] += 1
        if v == "MISSING_CONN":
            missing.append(r)
    print("pass A re-scored:", dict(scored), flush=True)

    # cluster into sites: shared node OR midpoint proximity
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_node = defaultdict(list)
    mids = []
    for i, r in enumerate(missing):
        by_node[r["a"]].append(i)
        by_node[r["b"]].append(i)
        mids.append(meters_xy((r["a_ll"][0] + r["b_ll"][0]) / 2,
                              (r["a_ll"][1] + r["b_ll"][1]) / 2))
    for idxs in by_node.values():
        for j in idxs[1:]:
            union(idxs[0], j)
    if mids:
        mt = cKDTree(np.array(mids))
        for i, j in mt.query_pairs(SITE_JOIN_M):
            union(int(i), int(j))

    sites = defaultdict(list)
    for i in range(len(missing)):
        sites[find(i)].append(i)
    out_sites = []
    for members in sites.values():
        worst = max(members, key=lambda i: (
            (missing[i]["graph_m"] or 1e9) - missing[i]["osrm_m"]))
        r = missing[worst]
        lat = (r["a_ll"][0] + r["b_ll"][0]) / 2
        lon = (r["a_ll"][1] + r["b_ll"][1]) / 2
        excess = (r["graph_m"] or 1e9) - r["osrm_m"]
        if t1_tree is not None and t1_tree.query(meters_xy(lat, lon))[0] <= T1_NEAR_M:
            attr = "T1"
        elif near_zone(zpolys, lat, lon):
            attr = "FEE_ZONE"
        else:
            attr = "OPEN"
        out_sites.append({
            "lat": round(lat, 6), "lon": round(lon, 6),
            "n_pairs": len(members),
            "worst_pair": [r["a"], r["b"]],
            "real_m": r["real_m"], "graph_m": r["graph_m"],
            "osrm_m": r["osrm_m"], "excess_m": round(min(excess, 1e9)),
            "attribution": attr,
        })
    out_sites.sort(key=lambda s: -s["excess_m"])
    print(f"{len(out_sites)} MISSING_CONN sites:",
          Counter(s["attribution"] for s in out_sites), flush=True)
    return scored, out_sites


def triage_pass_b(store, t1_tree, zpolys):
    data = json.load(open(f"{CENSUS}/pass_b_{RUN}.json"))
    flags = [r for r in data["rows"] if r["flag"]]
    print(f"pass B: {len(flags)} flagged of {len(data['rows'])}", flush=True)

    gaps = json.load(open(
        f"{REPO}/server/known_node_gaps.json"))
    gap_pairs = {frozenset((e[0], e[1])) for e in gaps}
    idx_to_id = {v: k for k, v in store._id_to_idx.items()}
    xy = None

    def node_at(lat, lon):
        nonlocal xy
        if xy is None:
            ll = store._node_lonlat
            lat0 = math.radians(40.7)
            xy = cKDTree(np.column_stack([
                ll[:, 0] * 111320.0 * math.cos(lat0), ll[:, 1] * 110540.0]))
        d, i = xy.query(meters_xy(lat, lon))
        return int(i) if d <= 5.0 else None

    out = []
    for r in flags:
        lat = (r["a_ll"][0] + r["b_ll"][0]) / 2
        lon = (r["a_ll"][1] + r["b_ll"][1]) / 2
        row = dict(r)
        attr = None
        if r["flag"] in ("TOO_LONG", "UNREACHABLE_OURS"):
            if t1_tree is not None and \
                    t1_tree.query(meters_xy(lat, lon))[0] <= T1_NEAR_M:
                attr = "T1"
            elif near_zone(zpolys, r["a_ll"][0], r["a_ll"][1]) or \
                    near_zone(zpolys, r["b_ll"][0], r["b_ll"][1]):
                attr = "FEE_ZONE"
            else:
                attr = "OPEN"
        elif r["flag"] == "TOO_SHORT":
            ia = node_at(*r["a_ll"])
            ib = node_at(*r["b_ll"])
            if ia is None or ib is None:
                attr = "NODE_MOVED"
            else:
                path = store._graph.get_shortest_paths(
                    ia, to=ib, weights=store._length, output="vpath")[0]
                ids = [idx_to_id[i] for i in path]
                if any(":" in nid for nid in ids):
                    attr = "IMPORTED_LAYER"
                elif any(frozenset((x, y)) in gap_pairs
                         for x, y in zip(ids, ids[1:])):
                    attr = "GAP_BRIDGE"
                else:
                    attr = "OPEN_SHORT"
        row["attribution"] = attr
        out.append(row)
    print("pass B attribution:",
          Counter((r["flag"], r["attribution"]) for r in out), flush=True)
    return out


def main():
    t1_tree = build_t1_tree()
    zpolys = build_zone_polys()
    store = load_store()
    scored, a_sites = rescore_pass_a(t1_tree, zpolys)
    b_rows = triage_pass_b(store, t1_tree, zpolys)
    save_json({
        "pass_a_scored_counts": dict(scored),
        "pass_a_sites": a_sites,
        "pass_b_flagged": b_rows,
    }, f"{CENSUS}/{RUN}_triage.json")


if __name__ == "__main__":
    main()
