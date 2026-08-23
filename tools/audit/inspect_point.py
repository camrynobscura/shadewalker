"""Dump every OSM way near a lon/lat from the pinned extract, with tags.

A ground-truth microscope for one spot. Item 6's remaining work is
mostly "what is actually mapped at this location?", and answering that
from the extract beats guessing from a route trace.

Usage:
  uv run python tools/audit/inspect_point.py -74.074018 40.64085 --radius 40
  uv run python tools/audit/inspect_point.py <lon> <lat> --radius 25 --barriers-only
"""
import argparse
import math
import os
import sys

import osmium
from osmium.filter import EntityFilter
from pyproj import Transformer
from shapely.geometry import LineString, Point

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it -- this line
# used to be a copy in each of these scripts.
EXTRACT = config.OSM_EXTRACT_PATH
TRANSFORM = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)

INTERESTING = ("highway", "footway", "barrier", "access", "foot", "service",
               "name", "area", "railway", "man_made", "amenity", "leisure",
               "landuse", "building", "bridge", "tunnel", "layer", "crossing",
               "sidewalk", "surface", "gate")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lon", type=float)
    ap.add_argument("lat", type=float)
    ap.add_argument("--radius", type=float, default=30.0)
    ap.add_argument("--barriers-only", action="store_true")
    ap.add_argument("--extract", default=EXTRACT)
    args = ap.parse_args()

    cx, cy = TRANSFORM.transform(args.lon, args.lat)
    centre = Point(cx, cy)
    dlon = (args.radius + 50) / (111320.0 * math.cos(math.radians(args.lat)))
    dlat = (args.radius + 50) / 110540.0
    lo_lon, hi_lon = args.lon - dlon, args.lon + dlon
    lo_lat, hi_lat = args.lat - dlat, args.lat + dlat

    hits = []
    fp = (osmium.FileProcessor(args.extract, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        if args.barriers_only and "barrier" not in tags:
            continue
        near = False
        for n in way.nodes:
            if (n.location.valid() and lo_lon <= n.lon <= hi_lon
                    and lo_lat <= n.lat <= hi_lat):
                near = True
                break
        if not near:
            continue
        pts = [TRANSFORM.transform(n.lon, n.lat)
               for n in way.nodes if n.location.valid()]
        if len(pts) < 2:
            continue
        line = LineString(pts)
        d = centre.distance(line)
        if d <= args.radius:
            hits.append((d, way.id, tags))

    hits.sort()
    print(f"{len(hits)} ways within {args.radius:.0f}m of "
          f"{args.lat:.6f},{args.lon:.6f}")
    print(f"https://www.google.com/maps/@{args.lat},{args.lon},20z\n")
    for d, wid, tags in hits:
        shown = {k: v for k, v in tags.items() if k in INTERESTING}
        rest = len(tags) - len(shown)
        line = "  ".join(f"{k}={v}" for k, v in sorted(shown.items()))
        print(f"  {d:6.2f}m  way/{wid:<12} {line}"
              + (f"  (+{rest} tags)" if rest else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
