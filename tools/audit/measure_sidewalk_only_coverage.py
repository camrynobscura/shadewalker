"""Where does OSM's pedestrian network fail, if we use only it?

THE PROPOSED MODEL
------------------
Route on OSM's dedicated pedestrian infrastructure alone -- sidewalks,
crossings, footways, steps, pedestrian streets, foot-permitted cycleways
-- with no streets, no fallback layer, and no hand-made gap repairs. Four boroughs; Staten Island excluded.

WHY THIS MEASUREMENT AND NOT A RATIO
------------------------------------
The tempting shortcut is "full coverage would be 2x street-km, we have
1.23x, so we're at 62%". That bakes in "every street has sidewalks on
both sides", which is false (highways have none, some streets are
one-sided, some are legitimately sidewalk-free) -- an assumption of
exactly the kind that has already produced two retracted claims here.

So this measures the thing that actually matters instead: build the
graph, take its largest connected component, and ask per grid cell
whether you can actually walk there. A cell is only a REAL hole if it
has walkable street frontage (so people plausibly need to walk there)
but no usable pedestrian network.

  CONNECTED   pedestrian nodes present, in the main component
  ISLAND      pedestrian nodes present, but cut off from the main network
  HOLE        walkable street present, no pedestrian infrastructure
  EMPTY       no walkable street either -- water, rail yard, park interior

Emits a GeoJSON of ISLAND and HOLE cells so the gaps can be looked at.

Usage:
  uv run python tools/audit/measure_sidewalk_only_coverage.py
  uv run python tools/audit/measure_sidewalk_only_coverage.py --cell 500
"""
import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

import osmium
from osmium.filter import EntityFilter
from shapely.geometry import shape
from shapely.prepared import prep
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it.
EXTRACT = config.OSM_EXTRACT_PATH
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")
KEEP_BOROUGHS = {"manhattan", "brooklyn", "queens", "bronx"}

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

# Dedicated pedestrian infrastructure -- the proposed model. Streets
# themselves are deliberately absent.
PED_HIGHWAY = {"footway", "path", "steps", "pedestrian"}
# Street ways, kept only to decide whether a cell is somewhere a
# person would plausibly need to walk.
STREET_HIGHWAY = {
    "primary", "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "unclassified", "residential", "living_street",
    "service", "track",
}


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def is_pedestrian(tags):
    hw = tags.get("highway")
    if tags.get("area") == "yes":
        return False
    if tags.get("foot") in ("no", "private"):
        return False
    if tags.get("access") in ("private", "no") and \
            tags.get("foot") not in ("yes", "designated"):
        return False
    if hw == "cycleway":
        return tags.get("foot") in ("yes", "designated")
    return hw in PED_HIGHWAY


def is_street(tags):
    hw = tags.get("highway")
    if hw not in STREET_HIGHWAY or tags.get("area") == "yes":
        return False
    if tags.get("foot") in ("no", "private"):
        return False
    if tags.get("service") in ("private", "driveway", "parking_aisle"):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", type=float, default=250.0,
                    help="grid cell size in metres")
    ap.add_argument("--boroughs", default=None,
                    help="comma-separated borough names; default is the "
                         "four walkable ones. Use to check a single "
                         "borough before deciding whether to include it.")
    ap.add_argument("--out", default="data/audits/2026-08-22/sidewalk_only")
    args = ap.parse_args()

    keep = ({b.strip().lower() for b in args.boroughs.split(",")}
            if args.boroughs else KEEP_BOROUGHS)
    gj = json.load(open(BOROUGHS))
    polys = [shape(f["geometry"]) for f in gj["features"]
             if f["properties"]["boroname"].lower() in keep]
    names = sorted(f["properties"]["boroname"] for f in gj["features"]
                   if f["properties"]["boroname"].lower() in keep)
    if not polys:
        raise SystemExit(f"no borough matched {sorted(keep)}")
    area = unary_union(polys)
    ready = prep(area)
    minx, miny, maxx, maxy = area.bounds
    print(f"boroughs: {', '.join(names)}")
    print(f"bbox: {minx:.4f},{miny:.4f} .. {maxx:.4f},{maxy:.4f}\n")

    cell_lon = args.cell / K
    cell_lat = args.cell / LAT_M

    # Precompute which grid cells fall in the four boroughs, so per-node
    # membership is a set lookup rather than millions of point-in-polygon
    # tests (the naive version did not finish).
    in_area = set()
    cx0, cx1 = int(minx / cell_lon), int(maxx / cell_lon) + 1
    cy0, cy1 = int(miny / cell_lat), int(maxy / cell_lat) + 1
    from shapely.geometry import box
    for cx in range(cx0, cx1 + 1):
        for cy in range(cy0, cy1 + 1):
            if ready.intersects(box(cx * cell_lon, cy * cell_lat,
                                    (cx + 1) * cell_lon,
                                    (cy + 1) * cell_lat)):
                in_area.add((cx, cy))
    print(f"grid cells inside the selected boroughs: {len(in_area):,} "
          f"({args.cell:.0f}m)")

    def cell_of(lon, lat):
        return (int(lon / cell_lon), int(lat / cell_lat))

    ped = DSU()
    ped_nodes = set()
    ped_cells = defaultdict(set)
    street_cells = set()
    ped_km = street_km = 0.0

    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        p, s = is_pedestrian(tags), is_street(tags)
        if not (p or s):
            continue
        pts = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(pts) < 2:
            continue
        inside = [t for t in pts if cell_of(t[1], t[2]) in in_area]
        if not inside:
            continue
        length = 0.0
        for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:]):
            length += math.hypot((x1 - x0) * K, (y1 - y0) * LAT_M)
        if s:
            street_km += length / 1000.0
            for _, lon, lat in inside:
                street_cells.add(cell_of(lon, lat))
        if p:
            ped_km += length / 1000.0
            prev = None
            for nid, lon, lat in pts:
                ped_nodes.add(nid)
                ped_cells[cell_of(lon, lat)].add(nid)
                if prev is not None:
                    ped.union(prev, nid)
                prev = nid

    print(f"pedestrian infrastructure: {ped_km:,.0f} km / {len(ped_nodes):,} nodes")
    print(f"walkable street centerline: {street_km:,.0f} km\n")

    sizes = Counter(ped.find(n) for n in ped_nodes)
    main_root, main_n = sizes.most_common(1)[0]
    print(f"components: {len(sizes):,} | largest: {main_n:,} nodes "
          f"({main_n / max(len(ped_nodes), 1) * 100:.1f}%)")

    verdict = {}
    for c in set(street_cells) | set(ped_cells):
        nodes = ped_cells.get(c)
        if not nodes:
            verdict[c] = "HOLE" if c in street_cells else "EMPTY"
        elif any(ped.find(n) == main_root for n in nodes):
            verdict[c] = "CONNECTED"
        else:
            verdict[c] = "ISLAND"
    counts = Counter(verdict.values())
    relevant = counts["CONNECTED"] + counts["ISLAND"] + counts["HOLE"]
    print(f"\n=== GRID ({args.cell:.0f}m cells, cells with any walkable "
          f"street or pedestrian way) ===")
    for k in ("CONNECTED", "ISLAND", "HOLE", "EMPTY"):
        n = counts.get(k, 0)
        pctr = f"{n / max(relevant, 1) * 100:5.1f}%" if k != "EMPTY" else "     "
        print(f"  {k:<10} {n:>7,}  {pctr}")
    print(f"\n  of cells where someone would plausibly walk ({relevant:,}): "
          f"{counts['CONNECTED'] / max(relevant, 1) * 100:.1f}% usable, "
          f"{(counts['ISLAND'] + counts['HOLE']) / max(relevant, 1) * 100:.1f}% "
          f"not")

    feats = []
    for c, v in verdict.items():
        if v not in ("ISLAND", "HOLE"):
            continue
        x0, y0 = c[0] * cell_lon, c[1] * cell_lat
        x1, y1 = x0 + cell_lon, y0 + cell_lat
        feats.append({"type": "Feature", "properties": {"verdict": v},
                      "geometry": {"type": "Polygon", "coordinates": [[
                          [x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}})
    out_geo = os.path.join(REPO, args.out + "_gaps.geojson")
    os.makedirs(os.path.dirname(out_geo), exist_ok=True)
    with open(out_geo, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    print(f"\nwrote {len(feats):,} gap cells -> {args.out}_gaps.geojson")

    # Rough hosting size: the served graph stores per-node coords and
    # per-edge length/canopy. Current citywide is 513k nodes -> compare.
    print(f"\nSCALE: {len(ped_nodes):,} pedestrian nodes vs the 512,982 "
          f"nodes we ship today ({len(ped_nodes) / 512982:.1f}x)")
    return 0


def _pt(lon, lat):
    from shapely.geometry import Point
    return Point(lon, lat)


if __name__ == "__main__":
    sys.exit(main())
