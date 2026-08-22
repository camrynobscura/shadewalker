"""Shared plumbing for the local-OSRM exhaustive census.

The census compares the served graph against a LOCAL OSRM instance
(same OSM source, independent graph construction) in both directions:
too-long (we're missing a connection OSM has) and too-short (we route
somewhere OSM/reality forbids). Reopened 2026-08-20 after its 08-15
decline gate was met; see FIXES item 1 and HISTORY 2026-08-20 (late).

Local OSRM: docker, foot profile, MLD, port 5001 (5000 collides with
macOS AirPlay; 5173 is off-limits -- another project's dev server).
Data: data/oracle/new-york-latest.osm.pbf (Geofabrik, pinned by the
sidecar new-york-latest.timestamp.txt written at download time).
"""
import json
import math
import os
import sys

import numpy as np
import requests
from scipy.sparse import csr_matrix

# repo root from this file's own location (tools/census/census_common.py),
# so every path below survives a clone at any absolute location
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

# scripts live in tools/census (committed); RESULTS live in
# data/audits/census (gitignored, like all data artifacts)
CENSUS_RESULTS = os.path.join(REPO, "data", "audits", "census")

OSRM_LOCAL = "http://localhost:5001"
SNAP_MAX_M = 40.0


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_store():
    from server.graph_store import GraphStore
    store = GraphStore()
    store.load()
    return store


def store_csr(store):
    """Symmetric scipy CSR of the served graph, meters weights."""
    n = len(store._id_to_idx)
    src = np.fromiter((e.source for e in store._graph.es), dtype=np.int64)
    dst = np.fromiter((e.target for e in store._graph.es), dtype=np.int64)
    w = store._length.astype(np.float64)
    return csr_matrix(
        (np.concatenate([w, w]),
         (np.concatenate([src, dst]), np.concatenate([dst, src]))),
        shape=(n, n))


def main_component_mask(store):
    from collections import Counter
    comps = store._graph.connected_components()
    memb = np.array(comps.membership)
    main = Counter(memb.tolist()).most_common(1)[0][0]
    return memb == main


def local_meters_xy(store):
    """Node coords in approximate meters (equirectangular at NYC lat).
    Good to <0.1% at city scale -- fine for neighbor search, never for
    reported distances."""
    ll = store._node_lonlat
    lat0 = math.radians(40.7)
    x = ll[:, 0] * 111320.0 * math.cos(lat0)
    y = ll[:, 1] * 110540.0
    return np.column_stack([x, y])


def osrm_route_m(a_latlon, b_latlon, timeout=10):
    """Local-OSRM foot distance, or (None, reason)."""
    url = (f"{OSRM_LOCAL}/route/v1/foot/"
           f"{a_latlon[1]},{a_latlon[0]};{b_latlon[1]},{b_latlon[0]}"
           f"?overview=false")
    try:
        d = requests.get(url, timeout=timeout).json()
    except Exception as exc:  # noqa: BLE001 -- oracle, record and move on
        return None, f"error: {exc}"
    if d.get("code") != "Ok":
        return None, d.get("code")
    for wp, want in zip(d["waypoints"], (a_latlon, b_latlon)):
        moved = haversine_m(wp["location"][1], wp["location"][0],
                            want[0], want[1])
        if moved > SNAP_MAX_M:
            return None, f"snap {moved:.0f}m"
    return d["routes"][0]["distance"], None


def osrm_route_has_ferry(a_latlon, b_latlon, timeout=10):
    """Does local OSRM's foot route between the points ride a ferry leg?

    True / False, or None when OSRM is down or can't route the pair.
    The item-5c class (2026-08-21): every East River corridor TOO_LONG
    flag was OSRM riding a NYC Ferry crossing -- by design for a
    walking app, so triage auto-explains them instead of re-reporting
    ~170 ferry flags as OPEN leads at every census."""
    url = (f"{OSRM_LOCAL}/route/v1/foot/"
           f"{a_latlon[1]},{a_latlon[0]};{b_latlon[1]},{b_latlon[0]}"
           f"?overview=false&steps=true")
    try:
        d = requests.get(url, timeout=timeout).json()
    except Exception:  # noqa: BLE001 -- oracle, absent is an answer
        return None
    if d.get("code") != "Ok":
        return None
    for leg in d["routes"][0]["legs"]:
        for step in leg["steps"]:
            if step.get("mode") == "ferry":
                return True
    return False


def osrm_table_m(origin_latlon, dest_latlons, timeout=30):
    """One-to-many distances via /table. Returns (list of m|None, snaps)."""
    coords = ";".join(
        f"{lon},{lat}" for lat, lon in [origin_latlon] + dest_latlons)
    url = (f"{OSRM_LOCAL}/table/v1/foot/{coords}"
           f"?sources=0&annotations=distance")
    d = requests.get(url, timeout=timeout).json()
    if d.get("code") != "Ok":
        return None, d.get("code")
    dists = d["distances"][0][1:]
    snaps = []
    for wp, want in zip(d["destinations"][1:], dest_latlons):
        snaps.append(haversine_m(wp["location"][1], wp["location"][0],
                                 want[0], want[1]))
    origin_snap = haversine_m(
        d["sources"][0]["location"][1], d["sources"][0]["location"][0],
        origin_latlon[0], origin_latlon[1])
    out = []
    for dist, snap in zip(dists, snaps):
        if dist is None or snap > SNAP_MAX_M or origin_snap > SNAP_MAX_M:
            out.append(None)
        else:
            out.append(float(dist))
    return out, None


def save_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)
    print("wrote", path, flush=True)
