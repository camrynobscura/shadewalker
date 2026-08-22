"""Can we actually measure shade per pavement, BEFORE building anything?

THE UNKNOWN THIS CLOSES
-----------------------
The case for rebuilding on sidewalk data rests on per-side shade being
both real and measurable. `measure_side_of_street_asymmetry.py` showed
it is real (38% of streets are lopsided). This tests whether we can
MEASURE it correctly -- the piece that had no evidence behind it and was
holding my confidence down.

METHOD
------
Find streets with `footway=sidewalk` mapped on BOTH sides. For each
side independently:

  1. buffer the sidewalk polyline by PEDESTRIAN_BUFFER_M (where a walker
     actually is, and what shades them),
  2. sample the 2021 6-inch NYC land cover raster inside that buffer,
  3. compute the fraction that is tree canopy
     (config.CANOPY_RASTER_TREE_CLASS = 1).

Then two checks:

  A. DISCRIMINATION -- do the two sides of a street actually differ in
     the raster? If the raster says both sides are the same everywhere,
     per-side shade is unmeasurable no matter how good the model is.
  B. AGREEMENT -- does the tree-point data pick the same shadier side as
     the raster? Two independent datasets (Forestry point inventory vs
     aerial land cover) agreeing is far stronger evidence than either
     alone, and disagreement would mean our tree-based scoring cannot be
     trusted per-side.

Local only. Four boroughs.
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import osmium
import rasterio
from osmium.filter import EntityFilter
from pyproj import Transformer
from rasterio.features import geometry_mask
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import transform as shp_transform, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from tools.audit.measure_sidewalk_only_coverage import (  # noqa: E402
    KEEP_BOROUGHS, is_street,
)

EXTRACT = os.path.join(REPO, "data", "oracle", "new-york-latest.osm.pbf")
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")
TREES = os.path.join(REPO, "data", "raw", "socrata", "trees_*_v3.json")

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

# A walker occupies about this much of the pavement; canopy over it is
# what shades them. Kept small on purpose -- widen it and the two sides'
# buffers start overlapping on a narrow street, which would manufacture
# agreement.
PEDESTRIAN_BUFFER_M = 2.0
# A sidewalk must be at least this far from the centerline to be counted
# as "a side", and no further than this, or we pick up the next street.
SIDE_MIN_M, SIDE_MAX_M = 3.0, 25.0
MIN_SIDEWALK_LEN_M = 40.0
TREE_REACH_M = 15.0

TO_RASTER = Transformer.from_crs("EPSG:4326", "EPSG:2263", always_xy=True)


def to_m(lon, lat):
    return (lon * K, lat * LAT_M)


def side_of(line, p):
    """+1 / -1 for which side of a directed line a point falls."""
    s = line.project(p)
    a = line.interpolate(max(s - 1.0, 0.0))
    b = line.interpolate(min(s + 1.0, line.length))
    cross = ((b.x - a.x) * (p.y - a.y)) - ((b.y - a.y) * (p.x - a.x))
    return 1 if cross >= 0 else -1


def canopy_fraction(src, lines_4326, buffer_m):
    """Fraction of the pavement strip that is tree canopy in the raster.

    The buffer is applied AFTER reprojecting into the raster's own CRS
    (EPSG:2263, US survey feet) -- never in degrees. Buffering lat/lon
    degrees as if they were metres is this project's most-repeated
    geospatial bug, and at NYC's latitude it would stretch the strip ~30%
    wider east-west than north-south.
    """
    buf_ft = buffer_m / 0.3048006096012192  # US survey feet
    projected = [shp_transform(lambda x, y: TO_RASTER.transform(x, y), ln)
                 for ln in lines_4326]
    geom = unary_union([p.buffer(buf_ft) for p in projected])
    minx, miny, maxx, maxy = geom.bounds
    try:
        win = rasterio.windows.from_bounds(minx, miny, maxx, maxy,
                                           src.transform)
        win = win.round_offsets().round_lengths()
        if win.width < 1 or win.height < 1:
            return None
        data = src.read(1, window=win)
        if data.size == 0:
            return None
        mask = geometry_mask([geom], out_shape=data.shape,
                             transform=src.window_transform(win),
                             invert=True)
        vals = data[mask]
        if vals.size < 20:
            return None
        from pipeline import config
        return float((vals == config.CANOPY_RASTER_TREE_CLASS).mean())
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--streets", type=int, default=400)
    ap.add_argument("--out",
                    default="data/audits/2026-08-22/per_side_shade_test.json")
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

    # Streets and sidewalks in one pass.
    streets, sidewalks = [], []
    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        hw, fw = tags.get("highway"), tags.get("footway")
        is_sw = hw == "footway" and fw == "sidewalk"
        if not (is_sw or is_street(tags)):
            continue
        coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coords) < 2:
            continue
        if not any((int(x / clon), int(y / clat)) in in_area
                   for x, y in coords):
            continue
        rec = (LineString([to_m(x, y) for x, y in coords]), coords,
               tags.get("name"))
        (sidewalks if is_sw else streets).append(rec)
    print(f"streets {len(streets):,} | sidewalk ways {len(sidewalks):,}")

    sw_tree = STRtree([s[0] for s in sidewalks])

    # Trees, for the independent cross-check.
    trees = {}
    for path in glob.glob(TREES):
        for t in json.load(open(path)):
            gid, loc = t.get("globalid"), (t.get("location") or {}).get("coordinates")
            if not gid or not loc:
                continue
            if (int(loc[0] / clon), int(loc[1] / clat)) not in in_area:
                continue
            try:
                dbh = max(float(t.get("dbh") or 0), 1.0)
            except (TypeError, ValueError):
                dbh = 1.0
            trees[gid] = (loc[0], loc[1], dbh)
    tree_pts = [Point(*to_m(x, y)) for x, y, _ in trees.values()]
    tree_w = [w for _, _, w in trees.values()]
    tree_idx = STRtree(tree_pts)
    print(f"tree points: {len(trees):,}")

    src = rasterio.open(os.path.join(REPO, "data", "raw", "canopy",
                                     "landcover_nyc_2021_6in.tif"))
    print(f"raster: {src.width:,}x{src.height:,} @ {src.res[0]}ft\n")

    rows = []
    for line_m, coords, name in streets:
        if len(rows) >= args.streets:
            break
        if line_m.length < 60:
            continue
        # Sidewalks on each side of this street.
        sides = {1: [], -1: []}
        for j in sw_tree.query(line_m.buffer(SIDE_MAX_M)):
            sw_m, sw_coords, _ = sidewalks[j]
            d = line_m.distance(sw_m)
            if not (SIDE_MIN_M <= d <= SIDE_MAX_M):
                continue
            if sw_m.length < MIN_SIDEWALK_LEN_M:
                continue
            mid = sw_m.interpolate(sw_m.length / 2)
            sides[side_of(line_m, mid)].append(sw_coords)
        if not sides[1] or not sides[-1]:
            continue

        out = {}
        ok = True
        for sgn in (1, -1):
            geoms = [LineString(c) for c in sides[sgn]]
            frac = canopy_fraction(src, geoms, PEDESTRIAN_BUFFER_M)
            if frac is None:
                ok = False
                break
            out[sgn] = frac
        if not ok:
            continue

        # Independent tree-point view of the same street.
        tw = {1: 0.0, -1: 0.0}
        for j in tree_idx.query(line_m.buffer(TREE_REACH_M)):
            p = tree_pts[j]
            if line_m.distance(p) > TREE_REACH_M:
                continue
            tw[side_of(line_m, p)] += tree_w[j]

        rows.append({"name": name,
                     "canopy_a": round(out[1], 4),
                     "canopy_b": round(out[-1], 4),
                     "trees_a": round(tw[1], 1),
                     "trees_b": round(tw[-1], 1)})
        if len(rows) % 100 == 0:
            print(f"  ...{len(rows)}/{args.streets}", flush=True)

    if not rows:
        print("no streets with sidewalks mapped on both sides")
        return 1

    # A. Discrimination.
    diffs = sorted(abs(r["canopy_a"] - r["canopy_b"]) for r in rows)
    big = [r for r in rows if abs(r["canopy_a"] - r["canopy_b"]) >= 0.10]
    huge = [r for r in rows if abs(r["canopy_a"] - r["canopy_b"]) >= 0.25]
    print(f"\n=== A. DOES THE RASTER SEE A DIFFERENCE BETWEEN SIDES? ===")
    print(f"  streets with sidewalks on both sides: {len(rows):,}")
    print(f"  |canopy difference| between the two pavements:")
    print(f"    median      {diffs[len(diffs) // 2] * 100:5.1f} pp")
    print(f"    75th pct    {diffs[int(len(diffs) * .75)] * 100:5.1f} pp")
    print(f"    90th pct    {diffs[int(len(diffs) * .90)] * 100:5.1f} pp")
    print(f"    max         {diffs[-1] * 100:5.1f} pp")
    print(f"  differ by >=10pp: {len(big):,} ({len(big) / len(rows) * 100:.1f}%)")
    print(f"  differ by >=25pp: {len(huge):,} ({len(huge) / len(rows) * 100:.1f}%)")

    # B. Agreement between the two independent sources.
    judged = [r for r in rows
              if abs(r["canopy_a"] - r["canopy_b"]) >= 0.05
              and abs(r["trees_a"] - r["trees_b"]) > 0
              and max(r["trees_a"], r["trees_b"]) > 0]
    agree = [r for r in judged
             if (r["canopy_a"] > r["canopy_b"]) == (r["trees_a"] > r["trees_b"])]
    print(f"\n=== B. DO TREE POINTS AND THE RASTER AGREE ON WHICH SIDE? ===")
    print(f"  streets where both sources express an opinion: {len(judged):,}")
    if judged:
        print(f"  they pick the SAME shadier side: {len(agree):,} "
              f"({len(agree) / len(judged) * 100:.1f}%)")
        print(f"  (50% would be chance; two independent datasets)")

    with open(os.path.join(REPO, args.out), "w") as fh:
        json.dump({"streets": rows}, fh)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
