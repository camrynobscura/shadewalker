"""How much does side-of-street actually matter for shade?

THE CLAIM BEING TESTED
----------------------
The case for rebuilding on sidewalk data rests on this: a street's two
sides often have very different tree cover, and a model with one line
per street reports only their average -- so it tells you a street is "moderately
shady" when really one side is shaded and the other is bare.

This measures it rather than asserting it.

METHOD
------
For every walkable street in the four boroughs, take the real
NYC Forestry tree points within 20m, and assign each to the LEFT or
RIGHT of the street using the sign of the cross product against the
nearest segment. Then compare the two sides.

Trees are weighted by trunk diameter (dbh) as well as counted, because
one mature London plane shades more pavement than three saplings, and a
count alone would understate real asymmetry.

A street is "lopsided" when one side holds >= 70% of the shade. That is
the population a one-line-per-street model necessarily gets wrong for at least
one of its two pavements.

Local only. Staten Island excluded.
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

import osmium
from osmium.filter import EntityFilter
from shapely.geometry import LineString, Point, box, shape
from shapely.prepared import prep
from shapely.ops import unary_union
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from tools.audit.measure_sidewalk_only_coverage import (  # noqa: E402
    KEEP_BOROUGHS, is_street,
)

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it.
EXTRACT = config.OSM_EXTRACT_PATH
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")
TREES = os.path.join(REPO, "data", "raw", "socrata", "trees_*_v3.json")

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

# Street trees sit in the pavement strip; 20m from the street line covers
# both sides of an ordinary street and most of an avenue without reaching
# into the next block.
TREE_REACH_M = 20.0
# Enough trees that a split is meaningful rather than noise.
MIN_TREES = 4
# One side holding this much of the shade = the average is a bad summary.
LOPSIDED = 0.70


def to_m(lon, lat):
    return (lon * K, lat * LAT_M)


def width_class(tags):
    """Narrow vs wide, because the two errors compound differently.

    On a narrow street the same trees shade both pavements, so a
    whole-street average is nearly right. On a wide avenue the far side's
    trees shade nothing you walk on -- so the average is wrong even when
    the two sides are SYMMETRIC, and doubly wrong when they are not.

    `width` is rarely tagged in NYC; `lanes` is the usable proxy, with
    highway class as the fallback.
    """
    try:
        w = float(str(tags.get("width", "")).split()[0])
        if w >= 20:
            return "wide (>=20m tagged)"
        if w >= 12:
            return "medium (12-20m tagged)"
        return "narrow (<12m tagged)"
    except (TypeError, ValueError, IndexError):
        pass
    try:
        lanes = int(float(tags.get("lanes")))
        if lanes >= 5:
            return "wide (5+ lanes)"
        if lanes >= 3:
            return "medium (3-4 lanes)"
        return "narrow (1-2 lanes)"
    except (TypeError, ValueError):
        pass
    hw = tags.get("highway")
    if hw in ("primary", "primary_link", "secondary", "secondary_link"):
        return "wide (arterial, untagged lanes)"
    if hw in ("tertiary", "tertiary_link"):
        return "medium (tertiary, untagged lanes)"
    return "narrow (residential/other)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default="data/audits/2026-08-22/side_asymmetry.json")
    args = ap.parse_args()

    gj = json.load(open(BOROUGHS))
    area = unary_union([shape(f["geometry"]) for f in gj["features"]
                        if f["properties"]["boroname"].lower()
                        in KEEP_BOROUGHS])
    ready = prep(area)
    minx, miny, maxx, maxy = area.bounds
    clon, clat = 250.0 / K, 250.0 / LAT_M
    in_area = set()
    for cx in range(int(minx / clon), int(maxx / clon) + 2):
        for cy in range(int(miny / clat), int(maxy / clat) + 2):
            if ready.intersects(box(cx * clon, cy * clat,
                                    (cx + 1) * clon, (cy + 1) * clat)):
                in_area.add((cx, cy))

    # Trees, deduplicated across overlapping tile caches by globalid.
    trees = {}
    for path in glob.glob(TREES):
        for t in json.load(open(path)):
            gid = t.get("globalid")
            loc = (t.get("location") or {}).get("coordinates")
            if not gid or not loc:
                continue
            if (int(loc[0] / clon), int(loc[1] / clat)) not in in_area:
                continue
            try:
                dbh = float(t.get("dbh") or 0)
            except (TypeError, ValueError):
                dbh = 0.0
            trees[gid] = (loc[0], loc[1], max(dbh, 1.0))
    print(f"tree points in the four boroughs: {len(trees):,}")

    pts = [Point(*to_m(lon, lat)) for lon, lat, _ in trees.values()]
    weights = [w for _, _, w in trees.values()]
    tree_tree = STRtree(pts)

    results = []
    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    n_streets = 0
    for way in fp:
        tags = dict(way.tags)
        if not tags or not is_street(tags):
            continue
        coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coords) < 2:
            continue
        if not any((int(x / clon), int(y / clat)) in in_area
                   for x, y in coords):
            continue
        n_streets += 1
        line = LineString([to_m(x, y) for x, y in coords])
        if line.length < 30:
            continue

        left = right = 0.0
        nl = nr = 0
        for j in tree_tree.query(line.buffer(TREE_REACH_M)):
            p = pts[j]
            if line.distance(p) > TREE_REACH_M:
                continue
            # Which side: sign of the cross product of the segment
            # direction with the vector to the tree, taken at the
            # closest point on the line.
            s = line.project(p)
            a = line.interpolate(max(s - 1.0, 0.0))
            b = line.interpolate(min(s + 1.0, line.length))
            cross = ((b.x - a.x) * (p.y - a.y)) - ((b.y - a.y) * (p.x - a.x))
            if cross >= 0:
                left += weights[j]
                nl += 1
            else:
                right += weights[j]
                nr += 1

        if nl + nr < MIN_TREES:
            continue
        total = left + right
        share = max(left, right) / total if total else 0.0
        results.append({"name": tags.get("name"),
                        "len_m": round(line.length, 1),
                        "n_left": nl, "n_right": nr,
                        "w_left": round(left, 1), "w_right": round(right, 1),
                        "dominant_share": round(share, 3),
                        "width_class": width_class(tags)})

    print(f"walkable street ways examined: {n_streets:,}")
    print(f"streets with >= {MIN_TREES} nearby trees: {len(results):,}\n")
    if not results:
        return 1

    shares = sorted(r["dominant_share"] for r in results)

    def pct(p):
        return shares[min(len(shares) - 1, int(len(shares) * p))]

    lop = [r for r in results if r["dominant_share"] >= LOPSIDED]
    extreme = [r for r in results if r["dominant_share"] >= 0.90]
    onesided = [r for r in results if min(r["n_left"], r["n_right"]) == 0]

    print("=== HOW LOPSIDED IS STREET SHADE? ===")
    print("  share of tree canopy (dbh-weighted) on the heavier side:")
    print(f"    median        {pct(0.50) * 100:5.1f}%")
    print(f"    75th pct      {pct(0.75) * 100:5.1f}%")
    print(f"    90th pct      {pct(0.90) * 100:5.1f}%")
    print(f"\n  LOPSIDED (>= {LOPSIDED * 100:.0f}% on one side): "
          f"{len(lop):,} of {len(results):,} "
          f"({len(lop) / len(results) * 100:.1f}%)")
    print(f"  severely (>= 90% on one side):  {len(extreme):,} "
          f"({len(extreme) / len(results) * 100:.1f}%)")
    print(f"  ALL trees on one side:          {len(onesided):,} "
          f"({len(onesided) / len(results) * 100:.1f}%)")
    print(f"\n  A one-line-per-street model reports the average for both pavements,")
    print(f"  so on those {len(lop) / len(results) * 100:.0f}% of streets it")
    print(f"  is wrong for at least one side by construction.")

    print("\n=== BY STREET WIDTH ===")
    print("  (on a narrow street the same trees shade both pavements, so")
    print("   the average is nearly right; on a wide one it is not)")
    groups = defaultdict(list)
    for r in results:
        groups[r["width_class"]].append(r)
    order = sorted(groups, key=lambda g: (not g.startswith("wide"),
                                          not g.startswith("medium"), g))
    print(f"\n  {'street type':<34} {'streets':>8} {'median':>8} "
          f"{'lopsided':>9}")
    for g in order:
        rs = groups[g]
        sh = sorted(r["dominant_share"] for r in rs)
        med = sh[len(sh) // 2]
        nlop = sum(1 for r in rs if r["dominant_share"] >= LOPSIDED)
        print(f"  {g:<34} {len(rs):>8,} {med * 100:>7.1f}% "
              f"{nlop / len(rs) * 100:>8.1f}%")

    with open(os.path.join(REPO, args.out), "w") as fh:
        json.dump({"streets": results}, fh)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
