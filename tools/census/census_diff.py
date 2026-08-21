"""Census diff: BEFORE vs AFTER a rebuild, fully attributed.

Usage: uv run python data/audits/census/census_diff.py baseline_v25 v26

Pass B rows join on their coordinate pairs (stable across rebuilds by
construction). The oracle side must be byte-identical (same OSRM data);
any osrm_m mismatch fails loudly -- it would mean the comparison isn't
about our change any more. Our side classifies:
  IMPROVED   ours_m dropped >= 50m
  REGRESSED  ours_m rose >= 50m  (every one must be explained)
  NOISE      |delta| < 50m
Pass A sites match by proximity (100m): a BEFORE site with no AFTER
site within reach is HEALED; an AFTER site with no BEFORE site is NEW
(investigate); the rest PERSIST.
"""
import json
import math
import sys
from collections import Counter

import numpy as np
from scipy.spatial import cKDTree

import os
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CENSUS = os.path.join(REPO, "data", "audits", "census")
NOISE_M = 50.0
SITE_MATCH_M = 100.0


def meters_xy(lat, lon):
    lat0 = math.radians(40.7)
    return (lon * 111320.0 * math.cos(lat0), lat * 110540.0)


def diff_pass_b(before, after):
    def key(r):
        return (tuple(r["a_ll"]), tuple(r["b_ll"]))
    b_rows = {key(r): r for r in before["rows"]}
    a_rows = {key(r): r for r in after["rows"]}
    shared = b_rows.keys() & a_rows.keys()
    print(f"pass B: {len(shared)} shared pairs "
          f"({len(b_rows) - len(shared)} baseline-only, "
          f"{len(a_rows) - len(shared)} new-only)", flush=True)

    oracle_mismatch = 0
    changes = {"IMPROVED": [], "REGRESSED": [], "NOISE": 0}
    for k in shared:
        b, a = b_rows[k], a_rows[k]
        if b["osrm_m"] != a["osrm_m"]:
            oracle_mismatch += 1
            continue
        if b["ours_m"] is None or a["ours_m"] is None:
            if b["ours_m"] != a["ours_m"]:
                changes["IMPROVED" if b["ours_m"] is None else "REGRESSED"] \
                    .append({"pair": k, "before": b["ours_m"],
                             "after": a["ours_m"]})
            continue
        d = a["ours_m"] - b["ours_m"]
        if d <= -NOISE_M:
            changes["IMPROVED"].append(
                {"pair": k, "before": b["ours_m"], "after": a["ours_m"],
                 "delta": round(d)})
        elif d >= NOISE_M:
            changes["REGRESSED"].append(
                {"pair": k, "before": b["ours_m"], "after": a["ours_m"],
                 "delta": round(d)})
        else:
            changes["NOISE"] += 1
    if oracle_mismatch:
        raise SystemExit(f"ORACLE MISMATCH on {oracle_mismatch} pairs -- "
                         "the OSRM side changed; diff is invalid")
    print(f"pass B: {len(changes['IMPROVED'])} improved, "
          f"{len(changes['REGRESSED'])} regressed, "
          f"{changes['NOISE']} noise", flush=True)
    flag_flips = Counter(
        (b_rows[k]["flag"], a_rows[k]["flag"]) for k in shared
        if b_rows[k]["flag"] != a_rows[k]["flag"])
    print("pass B flag transitions:", dict(flag_flips), flush=True)
    return changes, flag_flips


def diff_sites(before_triage, after_triage):
    b_sites = before_triage["pass_a_sites"]
    a_sites = after_triage["pass_a_sites"]
    a_xy = np.array([meters_xy(s["lat"], s["lon"]) for s in a_sites]) \
        if a_sites else np.empty((0, 2))
    a_tree = cKDTree(a_xy) if len(a_xy) else None
    b_xy = np.array([meters_xy(s["lat"], s["lon"]) for s in b_sites]) \
        if b_sites else np.empty((0, 2))
    b_tree = cKDTree(b_xy) if len(b_xy) else None

    healed, persist = [], []
    for s in b_sites:
        d = a_tree.query(meters_xy(s["lat"], s["lon"]))[0] \
            if a_tree is not None else 1e9
        (healed if d > SITE_MATCH_M else persist).append(s)
    new = []
    for s in a_sites:
        d = b_tree.query(meters_xy(s["lat"], s["lon"]))[0] \
            if b_tree is not None else 1e9
        if d > SITE_MATCH_M:
            new.append(s)

    print(f"sites: {len(healed)} HEALED, {len(persist)} PERSIST, "
          f"{len(new)} NEW", flush=True)
    print("healed by attribution:",
          dict(Counter(s["attribution"] for s in healed)), flush=True)
    print("persisting by attribution:",
          dict(Counter(s["attribution"] for s in persist)), flush=True)
    if new:
        print("NEW sites (investigate):", flush=True)
        for s in sorted(new, key=lambda s: -s["excess_m"])[:15]:
            print(f"  {s['lat']:.5f},{s['lon']:.5f} excess={s['excess_m']} "
                  f"attr={s['attribution']} pair={s['worst_pair']}",
                  flush=True)
    return healed, persist, new


def main():
    before_run, after_run = sys.argv[1], sys.argv[2]
    b_b = json.load(open(f"{CENSUS}/pass_b_{before_run}.json"))
    a_b = json.load(open(f"{CENSUS}/pass_b_{after_run}.json"))
    changes, flips = diff_pass_b(b_b, a_b)

    b_t = json.load(open(f"{CENSUS}/{before_run}_triage.json"))
    a_t = json.load(open(f"{CENSUS}/{after_run}_triage.json"))
    healed, persist, new = diff_sites(b_t, a_t)

    out = {
        "pass_b_improved": sorted(changes["IMPROVED"],
                                  key=lambda c: c.get("delta", 0))[:200],
        "pass_b_regressed": sorted(changes["REGRESSED"],
                                   key=lambda c: -c.get("delta", 0)),
        "pass_b_noise": changes["NOISE"],
        "sites_healed": healed,
        "sites_new": new,
        "persist_counts": dict(Counter(s["attribution"] for s in persist)),
    }
    path = f"{CENSUS}/diff_{before_run}_to_{after_run}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print("wrote", path, flush=True)


if __name__ == "__main__":
    main()
