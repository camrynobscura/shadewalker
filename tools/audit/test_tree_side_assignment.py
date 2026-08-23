"""Can a NYC Tree Map point be assigned to the correct sidewalk?

THE RIGHT QUESTION
------------------
The Forestry Tree Points dataset (Socrata hn5i-inap) is this project's
primary tree source, deliberately and settledly so -- it is live, and it
carries species, diameter and condition, which is what drives the
seasonal shade model. The 2021 land-cover raster has none of that.

So the question for a sidewalk-based rebuild is NOT "raster or points".
It is whether each point can be attributed to the right PAVEMENT. If a
street tree sits clearly nearer one sidewalk than the other, assignment
is trivial. If most sit ambiguously between them, per-side scoring from
points is unreliable and would need rethinking.

METHOD
------
For streets with `footway=sidewalk` mapped on both sides, take every
Forestry tree within reach and measure its distance to the nearest
sidewalk on EACH side. Then ask how decisive that comparison is:

  CLEAR       nearer sidewalk is <= AMBIGUOUS_RATIO x the distance of
              the other, i.e. the tree plainly belongs to one pavement
  AMBIGUOUS   the two are comparably close -- a coin toss

Also reports absolute distance to its assigned sidewalk, because a tree
30m from any pavement is not shading a walker regardless of side.

Local only.
"""
import argparse
import glob
import json
import math
import os
import sys

import osmium
from osmium.filter import EntityFilter
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from tools.audit.measure_sidewalk_only_coverage import (  # noqa: E402
    KEEP_BOROUGHS, is_street,
)
from tools.audit.test_per_side_shade import side_of, to_m  # noqa: E402

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it -- this line
# used to be a copy in each of these scripts.
EXTRACT = config.OSM_EXTRACT_PATH
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")
TREES = os.path.join(REPO, "data", "raw", "socrata", "trees_*_v3.json")

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

SIDE_MIN_M, SIDE_MAX_M = 3.0, 25.0
MIN_SIDEWALK_LEN_M = 40.0
TREE_REACH_M = 25.0
# The nearer sidewalk must be this much closer to count as unambiguous.
AMBIGUOUS_RATIO = 0.6
# Past this a tree is not meaningfully shading that pavement anyway.
USEFUL_MAX_M = 12.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--streets", type=int, default=800)
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

    streets, sidewalks = [], []
    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        is_sw = tags.get("highway") == "footway" and \
            tags.get("footway") == "sidewalk"
        if not (is_sw or is_street(tags)):
            continue
        coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coords) < 2:
            continue
        if not any((int(x / clon), int(y / clat)) in in_area
                   for x, y in coords):
            continue
        geom = LineString([to_m(x, y) for x, y in coords])
        (sidewalks if is_sw else streets).append(geom)
    print(f"streets {len(streets):,} | sidewalks {len(sidewalks):,}")

    sw_tree = STRtree(sidewalks)

    trees = {}
    for path in glob.glob(TREES):
        for t in json.load(open(path)):
            gid = t.get("globalid")
            loc = (t.get("location") or {}).get("coordinates")
            if not gid or not loc:
                continue
            if (int(loc[0] / clon), int(loc[1] / clat)) not in in_area:
                continue
            trees[gid] = loc
    tree_pts = [Point(*to_m(*c)) for c in trees.values()]
    tree_idx = STRtree(tree_pts)
    print(f"tree points {len(trees):,}\n")

    clear = ambiguous = useful = far = 0
    ratios = []
    n_streets = 0
    for line in streets:
        if n_streets >= args.streets:
            break
        if line.length < 60:
            continue
        sides = {1: [], -1: []}
        for j in sw_tree.query(line.buffer(SIDE_MAX_M)):
            sw = sidewalks[j]
            d = line.distance(sw)
            if not (SIDE_MIN_M <= d <= SIDE_MAX_M):
                continue
            if sw.length < MIN_SIDEWALK_LEN_M:
                continue
            sides[side_of(line, sw.interpolate(sw.length / 2))].append(sw)
        if not sides[1] or not sides[-1]:
            continue
        n_streets += 1
        a = unary_union(sides[1])
        b = unary_union(sides[-1])
        for j in tree_idx.query(line.buffer(TREE_REACH_M)):
            p = tree_pts[j]
            if line.distance(p) > TREE_REACH_M:
                continue
            da, db = p.distance(a), p.distance(b)
            near, other = min(da, db), max(da, db)
            if other <= 0:
                continue
            r = near / other
            ratios.append(r)
            if r <= AMBIGUOUS_RATIO:
                clear += 1
            else:
                ambiguous += 1
            if near <= USEFUL_MAX_M:
                useful += 1
            else:
                far += 1

    total = clear + ambiguous
    if not total:
        print("no trees measured")
        return 1
    ratios.sort()
    print(f"=== CAN A TREE POINT BE ASSIGNED TO A PAVEMENT? ===")
    print(f"  streets with sidewalks both sides: {n_streets:,}")
    print(f"  tree points evaluated: {total:,}\n")
    print(f"  CLEAR (nearer sidewalk <= {AMBIGUOUS_RATIO:g}x the other): "
          f"{clear:,} ({clear / total * 100:.1f}%)")
    print(f"  AMBIGUOUS (comparably close to both):        "
          f"{ambiguous:,} ({ambiguous / total * 100:.1f}%)")
    print(f"\n  distance ratio near/far: median "
          f"{ratios[len(ratios) // 2]:.3f}, 75th "
          f"{ratios[int(len(ratios) * .75)]:.3f}, 90th "
          f"{ratios[int(len(ratios) * .90)]:.3f}")
    print(f"\n  within {USEFUL_MAX_M:g}m of its assigned pavement: "
          f"{useful:,} ({useful / total * 100:.1f}%)")
    print(f"  further than that (shading nobody):           "
          f"{far:,} ({far / total * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
