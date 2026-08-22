"""Derive pipeline/sidewalk_connectors.json — the T1 sidewalk connector list.

The centerline model excludes the separately-mapped sidewalk network
(footway=sidewalk) on purpose; the measured price is a small class of
severed real connectivity (FIXES item 1, survey 2026-08-20:
data/audits/2026-08-20/sidewalk_admit_survey_report.md). This tool
finds the sidewalk chains whose admission is measurably worth it and
writes them to a curated, committed file the pipeline imports.

THE RULE (T1, user-approved 2026-08-20):
  - both attachments are OSM nodes shared with the CURRENT served
    graph's main component,
  - the chain (shortest sidewalk-only path between the attachments,
    not passing through another attachment) is <= CHAIN_MAX_M,
  - the current graph detour between the attachments exceeds the chain
    by >= SAVING_MIN_M,
  - no part of the chain lies inside a fee-gated or restricted zone.

Derivation measures value against the CURRENT graph, which already
contains previously-admitted connectors — so a re-derivation would see
saving ~ 1x for every chain already admitted and drop it. To keep the
list stable, re-derivation KEEPS every existing entry whose chain
nodes still exist in the sidewalk layer (retiring the rest, reported
loudly, like gap-entry reconciliation) and only APPENDS newly
qualifying chains. Run it at every refetch (REFETCH.md).

Inputs (all local): the citywide any_sidewalks layer cache, the
exported tiles (SHADEWALKER_TILES_DIR or data/tiles), and
pipeline/fee_gated_zones.json + pipeline/restricted_zones.json.

Usage:
  uv run python -m pipeline.derive_sidewalk_connectors           # dry run
  uv run python -m pipeline.derive_sidewalk_connectors --write   # update file
"""
import glob
import gzip
import heapq
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CONNECTORS_PATH = os.path.join(REPO, "pipeline", "sidewalk_connectors.json")
# Both zone files feed the no-overlap rule: a chain through a ticketed
# attraction OR gated operational grounds (restricted_zones.json, v27)
# must never qualify -- the zones' interiors are excluded from the
# graph, so a connector into one would be a bridge to nowhere.
ZONES_PATHS = (
    os.path.join(REPO, "pipeline", "fee_gated_zones.json"),
    os.path.join(REPO, "pipeline", "restricted_zones.json"),
)
LAYER_GLOB = os.path.join(REPO, "data", "raw", "citywide_layers",
                          "any_sidewalks_*.graphml")

CHAIN_MAX_M = 100.0
SAVING_MIN_M = 150.0
# bounded-search ceiling while measuring detours. Qualification only
# needs "detour >= chain + SAVING_MIN_M" (<= 250m), so the ceiling is
# purely about how far saving_m stays EXACT for ranking; beyond it an
# entry records the conservative floor (ceiling - chain). 2km keeps the
# per-candidate Dijkstra ball small -- the first run used 8km and was
# ~10x slower for no admission-relevant information.
SEARCH_LIMIT_M = 2000.0

GRAPHML_NS = "{http://graphml.graphdrawing.org/xmlns}"


def newest_layer_path() -> str:
    paths = sorted(glob.glob(LAYER_GLOB), key=os.path.getmtime)
    if not paths:
        raise SystemExit("no any_sidewalks layer cache found — run a tile "
                         "fetch first (the layer is cached citywide)")
    return paths[-1]


def parse_sidewalk_layer(path):
    """Stream-parse the layer graphml: node coords + undirected edges.
    Key ids are resolved from the <key> declarations, not hard-coded."""
    key_for = {}
    nodes = {}
    edges = {}
    for event, elem in ET.iterparse(path, events=("end",)):
        tag = elem.tag
        if tag == f"{GRAPHML_NS}key":
            key_for[elem.get("id")] = (elem.get("for"), elem.get("attr.name"))
            continue
        if tag == f"{GRAPHML_NS}node":
            lat = lon = None
            for d in elem:
                kf = key_for.get(d.get("key"))
                if kf == ("node", "y"):
                    lat = float(d.text)
                elif kf == ("node", "x"):
                    lon = float(d.text)
            nodes[elem.get("id")] = (lat, lon)
            elem.clear()
        elif tag == f"{GRAPHML_NS}edge":
            u, v = elem.get("source"), elem.get("target")
            length = None
            for d in elem:
                if key_for.get(d.get("key")) == ("edge", "length"):
                    length = float(d.text)
            if length is None:
                elem.clear()
                continue
            key = (u, v) if u <= v else (v, u)
            prev = edges.get(key)
            if prev is None or length < prev:
                edges[key] = length
            elem.clear()
    return nodes, edges


def load_store():
    from server.graph_store import GraphStore
    store = GraphStore()
    store.load()
    return store


def anchor_map(store, sidewalk_node_ids):
    """Sidewalk node id -> (store node id, offset m) for every sidewalk
    node present in the served graph — as a real node (offset 0) or as
    an interior point of a simplified edge (nearest endpoint + offset,
    read from the exports' per-edge node_ids)."""
    tiles_dir = os.environ.get("SHADEWALKER_TILES_DIR",
                               os.path.join(REPO, "data", "tiles"))
    anchors = {}
    for path in sorted(glob.glob(os.path.join(tiles_dir, "*.json.gz"))):
        with gzip.open(path, "rt") as f:
            tile = json.load(f)
        for nid in sidewalk_node_ids & tile["nodes"].keys():
            if nid not in anchors:
                anchors[nid] = (nid, 0.0)
        for e in tile["edges"]:
            ids = e.get("node_ids") or []
            n = len(ids)
            if n < 3:
                continue
            length = float(e.get("length_m") or 0.0)
            u, v = str(e["u"]), str(e["v"])
            for i, nid in enumerate(ids):
                if nid in sidewalk_node_ids and nid not in anchors:
                    frac = i / (n - 1)
                    if ids[0] == v:
                        frac = 1.0 - frac
                    if frac <= 0.5:
                        anchors[nid] = (u, round(frac * length, 1))
                    else:
                        anchors[nid] = (v, round((1 - frac) * length, 1))
    return anchors


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        p = self.p
        while p.setdefault(x, x) != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def chain_paths(adj, comp_nodes, rims):
    """Shortest sidewalk-only path between every rim (anchor) pair of one
    contracted component, path NOT passing through a third rim.
    Returns {(rim_a, rim_b): (length_m, [node path])}."""
    interior = comp_nodes - rims
    out = {}
    rims_l = sorted(rims)
    for src in rims_l[:-1]:
        dist = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, x = heapq.heappop(pq)
            if d > dist.get(x, 1e18):
                continue
            if x != src and x in rims:
                continue
            for y, length in adj[x]:
                if y != src and y not in interior and y not in rims:
                    continue
                nd = d + length
                if nd < dist.get(y, 1e18):
                    dist[y] = nd
                    prev[y] = x
                    heapq.heappush(pq, (nd, y))
        for dst in rims_l:
            if dst > src and dst in dist:
                path = [dst]
                while path[-1] != src:
                    path.append(prev[path[-1]])
                out[(src, dst)] = (dist[dst], list(reversed(path)))
    return out


def main():
    write = "--write" in sys.argv

    layer_path = newest_layer_path()
    print(f"layer: {os.path.basename(layer_path)}", flush=True)
    nodes, edges = parse_sidewalk_layer(layer_path)
    print(f"{len(nodes)} sidewalk nodes, {len(edges)} edges", flush=True)

    store = load_store()
    anchors = anchor_map(store, set(nodes.keys()))
    anchor_set = set(anchors)
    print(f"{len(anchors)} anchors", flush=True)

    from collections import Counter
    comps_ig = store._graph.connected_components()
    memb = np.array(comps_ig.membership)
    main_comp = Counter(memb.tolist()).most_common(1)[0][0]

    def is_main(sw_node):
        att = anchors.get(sw_node)
        if att is None:
            return False
        idx = store._id_to_idx.get(att[0])
        return idx is not None and memb[idx] == main_comp

    # contract at anchors
    dsu = DSU()
    adj = defaultdict(list)
    for (u, v), length in edges.items():
        adj[u].append((v, length))
        adj[v].append((u, length))
        if u not in anchor_set and v not in anchor_set:
            dsu.union(u, v)
    comps = defaultdict(lambda: {"nodes": set()})
    aa_edges = []
    for (u, v), length in edges.items():
        ua, va = u in anchor_set, v in anchor_set
        if ua and va:
            aa_edges.append((u, v, length))
            continue
        root = dsu.find(v if ua else u)
        comps[root]["nodes"].add(u)
        comps[root]["nodes"].add(v)
    comp_rims = defaultdict(set)
    for root, c in comps.items():
        for x in c["nodes"]:
            if x in anchor_set:
                comp_rims[root].add(x)

    zpolys = []
    for zones_path in ZONES_PATHS:
        for z in json.load(open(zones_path))["zones"]:
            zpolys.append(Polygon(z["polygon"]))
    ztree = STRtree(zpolys)

    def chain_in_zone(path_nodes):
        for x in path_nodes:
            lat, lon = nodes[x]
            p = Point(lon, lat)
            if any(zpolys[i].contains(p) for i in ztree.query(p)):
                return True
        return False

    # candidates: shortest through-chain per component + direct
    # anchor-anchor edges, both ends attaching to DISTINCT main-comp nodes
    candidates = []
    for root, rims in comp_rims.items():
        if len(rims) < 2:
            continue
        paths = chain_paths(adj, comps[root]["nodes"], rims)
        best = None
        for (sa, sb), (ln, path) in sorted(paths.items(), key=lambda kv: kv[1][0]):
            if anchors[sa][0] == anchors[sb][0]:
                continue
            if not (is_main(sa) and is_main(sb)):
                continue
            best = (sa, sb, ln, path)
            break
        if best is None:
            continue
        sa, sb, ln, path = best
        chain_m = ln + anchors[sa][1] + anchors[sb][1]
        if chain_m > CHAIN_MAX_M or chain_in_zone(path):
            continue
        candidates.append((sa, sb, chain_m, path))
    for u, v, length in aa_edges:
        if anchors[u][0] == anchors[v][0]:
            continue
        if not (is_main(u) and is_main(v)):
            continue
        chain_m = length + anchors[u][1] + anchors[v][1]
        if chain_m > CHAIN_MAX_M or chain_in_zone([u, v]):
            continue
        candidates.append((u, v, chain_m, [u, v]))
    print(f"{len(candidates)} candidates within CHAIN_MAX_M", flush=True)

    # exact detour per candidate against the served graph
    n = len(store._id_to_idx)
    srcs = np.fromiter((e.source for e in store._graph.es), dtype=np.int64)
    dsts = np.fromiter((e.target for e in store._graph.es), dtype=np.int64)
    w = store._length.astype(np.float64)
    csr = csr_matrix(
        (np.concatenate([w, w]),
         (np.concatenate([srcs, dsts]), np.concatenate([dsts, srcs]))),
        shape=(n, n))
    by_src = defaultdict(list)
    for sa, sb, chain_m, path in candidates:
        ia = store._id_to_idx[anchors[sa][0]]
        ib = store._id_to_idx[anchors[sb][0]]
        by_src[ia].append((ib, sa, sb, chain_m, path))
    qualified = []
    done = 0
    for ia, lst in by_src.items():
        dists = dijkstra(csr, indices=ia, limit=SEARCH_LIMIT_M)
        for ib, sa, sb, chain_m, path in lst:
            g = dists[ib]
            saving = (SEARCH_LIMIT_M if math.isinf(g) else g) - chain_m
            if saving >= SAVING_MIN_M:
                lat, lon = nodes[path[len(path) // 2]]
                qualified.append({
                    "att_a": sa, "att_b": sb,
                    "chain_nodes": path,
                    "chain_m": round(chain_m, 1),
                    "saving_m": round(saving),
                    "lat": round(lat, 6), "lon": round(lon, 6),
                })
        done += 1
        if done % 5000 == 0:
            print(f"  {done}/{len(by_src)} sources, "
                  f"{len(qualified)} qualified", flush=True)
    print(f"{len(qualified)} qualified connectors", flush=True)

    # reconcile with the existing file: keep entries whose chain nodes
    # still exist in the layer (their savings self-collapse once
    # admitted, so they never re-qualify); retire the rest loudly
    existing = []
    if os.path.exists(CONNECTORS_PATH):
        existing = json.load(open(CONNECTORS_PATH))["connectors"]
    known = {(e["att_a"], e["att_b"]) for e in existing}
    kept, retired, zone_retired = [], [], []
    for e in existing:
        if not all(x in nodes for x in e["chain_nodes"]):
            retired.append(e)
        elif chain_in_zone(e["chain_nodes"]):
            # a zone (fee-gated or restricted) grew over an existing
            # chain -- the zone's interior edges are excluded from the
            # graph, so the connector would attach to nothing (first
            # case: a chain inside LaGuardia's fence, retired when the
            # restricted zones landed, v27)
            zone_retired.append(e)
        else:
            kept.append(e)
    new = [q for q in qualified if (q["att_a"], q["att_b"]) not in known]
    for e in retired:
        print(f"  RETIRED (chain nodes gone from layer): "
              f"{e['att_a']}<->{e['att_b']} at {e['lat']},{e['lon']}",
              flush=True)
    for e in zone_retired:
        print(f"  RETIRED (chain now inside an excluded zone): "
              f"{e['att_a']}<->{e['att_b']} at {e['lat']},{e['lon']}",
              flush=True)
    retired = retired + zone_retired
    print(f"existing kept {len(kept)}, retired {len(retired)}, "
          f"new {len(new)} -> total {len(kept) + len(new)}", flush=True)

    if write:
        out = {
            "rule": {"chain_max_m": CHAIN_MAX_M,
                     "saving_min_m": SAVING_MIN_M},
            "derived": {"layer": os.path.basename(layer_path),
                        "graph_nodes": n},
            "connectors": kept + sorted(
                new, key=lambda q: -q["saving_m"]),
        }
        with open(CONNECTORS_PATH, "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
        print(f"wrote {CONNECTORS_PATH}", flush=True)
    else:
        print("dry run — pass --write to update the file", flush=True)


if __name__ == "__main__":
    main()
