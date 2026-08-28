"""Can you actually cross the street on OSM's sidewalk network?

THE RISK THIS TESTS
-------------------
A sidewalk-only model routes across a street only where OSM has mapped a
`footway=crossing`. Where one is missing, the two sides of a street stay
CONNECTED (so a component-count check sees nothing wrong) but only via a
crossing hundreds of metres away. The router then walks you to the
corner, across, and back.

That is the same failure the centerline gap ledger exists to fix, just
relocated -- so before betting a rewrite on the sidewalk model, measure
it directly rather than inferring it from connectivity.

METHOD
------
Sample sidewalk nodes. For each, find a node that is geometrically close
(8-40m: the width of a street, not the same block face) but NOT already
adjacent along the network. Then measure the real walking distance
between them on the pedestrian graph.

  street width apart, short walk  -> a crossing is mapped. Fine.
  street width apart, long walk   -> missing crossing. A real detour.

Reports the full distribution, not just a pass/fail, because the
question is "how often and how badly", not "does it ever happen".

Local only. Four boroughs; Staten Island excluded.

PROMOTED TO A TEST 2026-08-28 (PLAN `citywide-guards`):
tests/test_citywide_invariants.py's crossing-detour test runs this method
against the EXPORT graph (the one that actually routes) on every citywide
pytest run, re-baselined there because the populations differ (this
tool's raw-pbf graph read median 12m / 3.1% > 200m; the export reads
13.5m / 2.13%). This tool stays as the pbf-side instrument: when the test
goes red, running this against the same OSM pin says whether the change
came from OSM's data or from our pipeline.
"""
import argparse
import heapq
import json
import math
import os
import random
import sys
from collections import defaultdict

import osmium
from osmium.filter import EntityFilter
from shapely.geometry import shape, box
from shapely.prepared import prep
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from tools.audit.measure_sidewalk_only_coverage import (  # noqa: E402
    DSU, KEEP_BOROUGHS, is_pedestrian,
)

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it -- this line
# used to be a copy in each of these scripts.
EXTRACT = config.OSM_EXTRACT_PATH
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

# "Across a street", not "further along the same pavement". NYC street
# reservations run ~18-30m building line to building line; sidewalk
# centrelines sit maybe 8-25m apart across an ordinary street, more on an
# avenue. Below 8m is almost always the same block face.
NEAR_MIN_M = 8.0
NEAR_MAX_M = 40.0

# How far we are willing to search before calling it hopeless.
CUTOFF_M = 1200.0

# A crossing detour past this is a real routing problem, not a kerb-to-
# kerb nicety.
BAD_DETOUR_M = 200.0


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * K, (a[1] - b[1]) * LAT_M)


def bounded(adj, src, dst, cutoff):
    seen = {src: 0.0}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == dst:
            return d
        if d > cutoff or d > seen.get(u, float("inf")):
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd <= cutoff and nd < seen.get(v, float("inf")):
                seen[v] = nd
                heapq.heappush(heap, (nd, v))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--out",
                    default="data/audits/2026-08-22/crossing_detours.json")
    args = ap.parse_args()

    gj = json.load(open(BOROUGHS))
    area = unary_union([shape(f["geometry"]) for f in gj["features"]
                        if f["properties"]["boroname"].lower()
                        in KEEP_BOROUGHS])
    ready = prep(area)
    minx, miny, maxx, maxy = area.bounds

    cell = 250.0
    clon, clat = cell / K, cell / LAT_M
    in_area = set()
    for cx in range(int(minx / clon), int(maxx / clon) + 2):
        for cy in range(int(miny / clat), int(maxy / clat) + 2):
            if ready.intersects(box(cx * clon, cy * clat,
                                    (cx + 1) * clon, (cy + 1) * clat)):
                in_area.add((cx, cy))

    coords, adj, dsu = {}, defaultdict(list), DSU()
    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags or not is_pedestrian(tags):
            continue
        pts = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(pts) < 2:
            continue
        if not any((int(p[1] / clon), int(p[2] / clat)) in in_area
                   for p in pts):
            continue
        prev = None
        for nid, lon, lat in pts:
            coords.setdefault(nid, (lon, lat))
            if prev is not None:
                w = dist_m(coords[prev], coords[nid])
                adj[prev].append((nid, w))
                adj[nid].append((prev, w))
                dsu.union(prev, nid)
            prev = nid
    print(f"pedestrian graph: {len(coords):,} nodes")

    # Only judge the main component; islands are a separate, already
    # measured problem and would swamp this signal.
    from collections import Counter
    root = Counter(dsu.find(n) for n in coords).most_common(1)[0][0]
    main_nodes = [n for n in coords if dsu.find(n) == root]
    print(f"main component: {len(main_nodes):,} nodes")

    grid = defaultdict(list)
    gs = NEAR_MAX_M
    for n in main_nodes:
        lon, lat = coords[n]
        grid[(int(lon * K / gs), int(lat * LAT_M / gs))].append(n)

    random.seed(args.seed)
    random.shuffle(main_nodes)

    results = []
    for n in main_nodes:
        if len(results) >= args.samples:
            break
        lon, lat = coords[n]
        gx, gy = int(lon * K / gs), int(lat * LAT_M / gs)
        direct = {v for v, _ in adj[n]}
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in grid.get((gx + dx, gy + dy), ()):
                    if m == n or m in direct:
                        continue
                    d = dist_m(coords[n], coords[m])
                    if NEAR_MIN_M <= d <= NEAR_MAX_M:
                        if best is None or d < best[1]:
                            best = (m, d)
        if best is None:
            continue
        m, straight = best
        walk = bounded(adj, n, m, CUTOFF_M)
        results.append({"a": n, "b": m,
                        "straight_m": round(straight, 1),
                        "walk_m": round(walk, 1) if walk else None,
                        "at": f"{coords[n][1]:.5f},{coords[n][0]:.5f}"})

    ok = [r for r in results if r["walk_m"] is not None]
    unreachable = len(results) - len(ok)
    walks = sorted(r["walk_m"] for r in ok)
    bad = [r for r in ok if r["walk_m"] > BAD_DETOUR_M]

    def pct(p):
        return walks[min(len(walks) - 1, int(len(walks) * p))]

    print(f"\n=== CROSSING THE STREET ON THE SIDEWALK NETWORK ===")
    print(f"  sampled pairs {NEAR_MIN_M:.0f}-{NEAR_MAX_M:.0f}m apart "
          f"(i.e. across a street): {len(results):,}")
    print(f"  no route within {CUTOFF_M:.0f}m: {unreachable}")
    if walks:
        print(f"\n  actual walking distance between them:")
        print(f"    median      {pct(0.50):7.0f} m")
        print(f"    75th pct    {pct(0.75):7.0f} m")
        print(f"    90th pct    {pct(0.90):7.0f} m")
        print(f"    95th pct    {pct(0.95):7.0f} m")
        print(f"    worst       {walks[-1]:7.0f} m")
        for bar in (50, 100, 200, 400):
            n_over = sum(1 for w in walks if w > bar)
            print(f"    over {bar:>3}m: {n_over:>5,} "
                  f"({n_over / len(walks) * 100:.1f}%)")
        print(f"\n  REAL PROBLEM CASES (>{BAD_DETOUR_M:.0f}m to cross a "
              f"street): {len(bad):,} of {len(ok):,} "
              f"({len(bad) / len(ok) * 100:.1f}%)")
        for r in sorted(bad, key=lambda r: -r["walk_m"])[:8]:
            print(f"    {r['straight_m']:5.1f}m apart -> {r['walk_m']:7.0f}m "
                  f"walk   https://www.google.com/maps/@{r['at']},19z")

    with open(os.path.join(REPO, args.out), "w") as fh:
        json.dump({"results": results}, fh)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
