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

Provisional MISSING_CONN rows are then EXACTLY re-measured against the
served graph (FIXES 1g, 2026-08-21): pass A's graph_m is a bounded
dijkstra, so a None means "beyond the search limit", not "infinite" --
ranking its 1e9 stand-in put fake excesses at the top of the queue
(the sentinel-ranking bug). Rows whose exact distance drops back under
the harm bar are demoted to AGREE.

Surviving pairs cluster into SITES (shared node or midpoints within
100m), ranked by worst exact g - osrm. Each site is attributed, in
order:
  IMPORTED_END  worst pair touches a synthetic (imported-layer) node --
                item 6's queue, split out BEFORE the radius checks so a
                nearby T1 connector can't mislabel it (FIXES 1g)
  T1            within 150m of an admitted T1 sidewalk connector
  FEE_ZONE /    within 100m of a fee-gated / restricted zone polygon
  RESTRICTED_ZONE  (entrance stubs and excluded-interior remnants)
  OPEN          none of the above -- the census's genuinely new leads

Pass B flags get the same attribution, with two tracing extras:
TOO_SHORT rows go through our actual shortest path (a curated gap
bridge or an imported synthetic edge explains the shortcut BY DESIGN;
the rest are over-permissiveness leads, the Todt Hill class), and
TOO_LONG / UNREACHABLE_OURS rows are checked against local OSRM for a
ferry leg first (item 5c: OSRM riding a NYC Ferry crossing is not a
missing walking connection; attribution FERRY). The ferry check needs
:5001 up -- when it isn't, rows keep their radius attribution and gain
"ferry_check": "unavailable".
"""
import json
import math
import sys
from collections import Counter, defaultdict

import numpy as np
from scipy.spatial import cKDTree


from census_common import (REPO, CENSUS_RESULTS, haversine_m,
                           load_store, osrm_route_has_ferry, save_json)

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
    """(kind, buffered polygon) for fee-gated AND restricted zones: a
    flag near either kind is zone-attributable, not an OPEN lead --
    restricted zones added with the v27 airport exclusions (FIXES 1f).
    Kinds stay distinct so the attribution label tells the truth."""
    from shapely.geometry import Polygon
    polys = []
    for kind, fname in (("FEE_ZONE", "fee_gated_zones.json"),
                        ("RESTRICTED_ZONE", "restricted_zones.json")):
        for z in json.load(open(f"{REPO}/pipeline/{fname}"))["zones"]:
            polys.append(
                (kind, Polygon(z["polygon"]).buffer(ZONE_NEAR_M / 111000.0)))
    return polys


def near_zone(zpolys, lat, lon):
    """The zone kind the point sits in (buffered), or None."""
    from shapely.geometry import Point
    p = Point(lon, lat)
    for kind, poly in zpolys:
        if poly.contains(p):
            return kind
    return None


def exact_remeasure(store, rows):
    """Replace pass A's bounded graph_m with exact igraph distances.

    Rows whose ids aren't in the loaded store (another build vintage,
    or a node the current build dropped) keep their bounded value and
    gain "remeasure": "id_missing" -- rank those with suspicion."""
    by_src = defaultdict(list)
    for i, r in enumerate(rows):
        ia = store._id_to_idx.get(r["a"])
        ib = store._id_to_idx.get(r["b"])
        if ia is None or ib is None:
            r["remeasure"] = "id_missing"
            continue
        by_src[ia].append((i, ib))
    for ia, lst in by_src.items():
        dists = store._graph.distances(
            source=[ia], target=[ib for _i, ib in lst],
            weights=store._length)[0]
        for (i, _ib), dist in zip(lst, dists):
            rows[i]["graph_m"] = (None if math.isinf(dist)
                                  else round(float(dist), 1))
            rows[i]["remeasure"] = "exact"
    return rows


def rescore_pass_a(store, t1_tree, zpolys):
    data = json.load(open(f"{CENSUS}/pass_a_{RUN}.json"))
    rows = data["rows"]

    def score(r):
        g = r["graph_m"] if r["graph_m"] is not None else float("inf")
        osrm = r["osrm_m"]
        if osrm is None:
            return ("OSRM_SNAP" if str(r["verdict"]).startswith("OSRM_snap")
                    else "OSRM_OTHER")
        # a ~0m OSRM read for a pair tens of meters apart means both
        # endpoints snapped to the SAME network point -- a degenerate
        # measurement, not evidence of a short walk (found via a 465-pair
        # Cypress Hills false flag, 2026-08-21: local OSRM 0m, fresh
        # OSRM + our graph agreeing at 1,267m)
        if osrm < 5.0 and r["real_m"] > 20.0:
            return "OSRM_DEGENERATE"
        if g <= 1.2 * osrm or g - osrm < HARM_M:
            return "AGREE"
        if osrm <= max(250.0, 5.0 * r["real_m"]):
            return "MISSING_CONN"
        return "GREY_MID"

    # provisional pass on the bounded numbers, exact re-measure of the
    # provisional MISSING_CONN rows, then re-score those -- see the
    # module docstring's sentinel-ranking note (FIXES 1g)
    provisional = [r for r in rows if score(r) == "MISSING_CONN"]
    exact_remeasure(store, provisional)
    scored = Counter()
    missing = []
    demoted = 0
    for r in rows:
        v = score(r)
        scored[v] += 1
        if v == "MISSING_CONN":
            missing.append(r)
        elif r.get("remeasure") == "exact":
            demoted += 1  # provisional MISSING_CONN, exact walk is fine
    print("pass A re-scored:", dict(scored),
          f"({demoted} demoted by exact re-measure)", flush=True)

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
        # graph_m here is exact (or flagged id_missing) after
        # exact_remeasure; None now genuinely means disconnected
        excess = (r["graph_m"] or 1e9) - r["osrm_m"]
        zone_kind = near_zone(zpolys, lat, lon)
        if ":" in r["a"] or ":" in r["b"]:
            attr = "IMPORTED_END"
        elif t1_tree is not None and t1_tree.query(meters_xy(lat, lon))[0] <= T1_NEAR_M:
            attr = "T1"
        elif zone_kind is not None:
            attr = zone_kind
        else:
            attr = "OPEN"
        out_sites.append({
            "lat": round(lat, 6), "lon": round(lon, 6),
            "n_pairs": len(members),
            "worst_pair": [r["a"], r["b"]],
            "real_m": r["real_m"], "graph_m": r["graph_m"],
            "remeasure": r.get("remeasure"),
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
    ferry_unavailable = 0
    for r in flags:
        lat = (r["a_ll"][0] + r["b_ll"][0]) / 2
        lon = (r["a_ll"][1] + r["b_ll"][1]) / 2
        row = dict(r)
        attr = None
        if r["flag"] in ("TOO_LONG", "UNREACHABLE_OURS"):
            # item-5c ferry auto-explain FIRST (FIXES 1g): OSRM riding
            # a NYC Ferry crossing is shorter by design, not a missing
            # walking connection
            ferry = osrm_route_has_ferry(r["a_ll"], r["b_ll"])
            if ferry is None:
                ferry_unavailable += 1
                row["ferry_check"] = "unavailable"
            zone_kind = (near_zone(zpolys, r["a_ll"][0], r["a_ll"][1])
                         or near_zone(zpolys, r["b_ll"][0], r["b_ll"][1]))
            if ferry:
                attr = "FERRY"
            elif t1_tree is not None and \
                    t1_tree.query(meters_xy(lat, lon))[0] <= T1_NEAR_M:
                attr = "T1"
            elif zone_kind is not None:
                attr = zone_kind
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
    if ferry_unavailable:
        print(f"WARNING: ferry check unavailable for {ferry_unavailable} "
              f"rows (local OSRM down?) -- their FERRY candidates are "
              f"attributed by radius instead", flush=True)
    return out


def main():
    t1_tree = build_t1_tree()
    zpolys = build_zone_polys()
    store = load_store()
    scored, a_sites = rescore_pass_a(store, t1_tree, zpolys)
    b_rows = triage_pass_b(store, t1_tree, zpolys)
    save_json({
        "pass_a_scored_counts": dict(scored),
        "pass_a_sites": a_sites,
        "pass_b_flagged": b_rows,
    }, f"{CENSUS}/{RUN}_triage.json")


if __name__ == "__main__":
    main()
