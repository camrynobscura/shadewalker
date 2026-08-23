"""Can a sidewalk way be given the right street name?

THE QUESTION
------------
97.3% of NYC's 249,665 `footway=sidewalk` ways carry no name of their own,
and OSM's `associatedStreet` relation covers 0.7% of them (83 relations
citywide), so turn-by-turn directions on a sidewalk network have nothing
to say unless the name is DERIVED from the nearest street.

That derivation is only safe if it is usually unambiguous. This measures
how often it is.

METHOD
------
For each sampled sidewalk, find every named street within PARENT_MAX_M and
group the candidates BY NAME -- two ways both called "Court Street" are one
candidate, not two, since either gives the same answer. Then:

  CLEAR       nearest name is <= AMBIGUOUS_RATIO x the distance of the
              nearest DIFFERENTLY-named street, or is the only name in
              range -- the sidewalk plainly belongs to one street
  AMBIGUOUS   two different street names are comparably close (corners,
              slip roads, service roads beside an arterial)
  NO PARENT   no named street within PARENT_MAX_M -- park paths, plaza
              interiors, greenways. These emit NO name rather than a guess.

Same AMBIGUOUS_RATIO bar as test_tree_side_assignment.py, deliberately:
both ask "is the nearest candidate decisively nearer than the runner-up?"

Local only. Read-only.
"""
import argparse
import json
import math
import os
import random
import sys

import osmium
from osmium.filter import EntityFilter
from shapely.geometry import LineString, box, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from tools.audit.measure_sidewalk_only_coverage import (  # noqa: E402
    KEEP_BOROUGHS, is_street,
)
from tools.audit.test_per_side_shade import to_m  # noqa: E402

from pipeline import config  # noqa: E402

# The pinned extract, from the one place that defines it -- this line
# used to be a copy in each of these scripts.
EXTRACT = config.OSM_EXTRACT_PATH
BOROUGHS = os.path.join(REPO, "data", "raw", "socrata",
                        "borough_boundaries_wh2p-dxnf.geojson")

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0

# How far from a sidewalk to look for its parent street. A NYC sidewalk
# sits roughly 5-20m from its own centerline; past 30m the nearest street
# is more likely a different one than a far-set parent.
PARENT_MAX_M = 30.0
# The nearer name must be this much closer to count as unambiguous.
AMBIGUOUS_RATIO = 0.6
# Sidewalks shorter than this are mostly corner nubs and driveway aprons --
# they carry little route length and their nearest-street answer is noisy.
MIN_SIDEWALK_LEN_M = 15.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidewalks", type=int, default=8000,
                    help="how many sidewalks to sample (default 8000)")
    ap.add_argument("--seed", type=int, default=20260822)
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

    # Streets keep their name; sidewalks keep whether they already have one.
    streets, street_names = [], []
    sidewalks, sidewalk_named = [], []
    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        is_sw = (tags.get("highway") == "footway"
                 and tags.get("footway") == "sidewalk")
        named_street = bool(tags.get("name")) and is_street(tags)
        if not (is_sw or named_street):
            continue
        coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coords) < 2:
            continue
        if not any((int(x / clon), int(y / clat)) in in_area
                   for x, y in coords):
            continue
        geom = LineString([to_m(x, y) for x, y in coords])
        if is_sw:
            sidewalks.append(geom)
            sidewalk_named.append(bool(tags.get("name")))
        else:
            streets.append(geom)
            street_names.append(tags["name"])

    print(f"named streets {len(streets):,} | sidewalks {len(sidewalks):,}")
    street_idx = STRtree(streets)

    rng = random.Random(args.seed)
    order = list(range(len(sidewalks)))
    rng.shuffle(order)

    clear = ambiguous = no_parent = 0
    own_name = 0
    only_one_name = 0
    ratios = []
    parent_distances = []
    evaluated = 0

    for i in order:
        if evaluated >= args.sidewalks:
            break
        sw = sidewalks[i]
        if sw.length < MIN_SIDEWALK_LEN_M:
            continue
        evaluated += 1
        if sidewalk_named[i]:
            own_name += 1

        # Distance PER STREET NAME, measured as the MEDIAN over points
        # sampled along the sidewalk -- not the minimum over the whole line.
        #
        # The minimum is the wrong metric and produces a badly wrong answer:
        # a block-length sidewalk touches a different cross street at each
        # end, so min-distance says all three streets are "within 30m" and
        # nearly every sidewalk reads as ambiguous. The parent street runs
        # alongside for the WHOLE length; a cross street is close at one end
        # and far everywhere else, which is exactly what a median separates.
        n_probes = max(3, int(sw.length // 10))
        probes = [sw.interpolate(sw.length * k / (n_probes - 1))
                  for k in range(n_probes)]

        by_name = {}
        for j in street_idx.query(sw.buffer(PARENT_MAX_M)):
            name = street_names[j]
            ds = sorted(p.distance(streets[j]) for p in probes)
            median_d = ds[len(ds) // 2]
            if median_d > PARENT_MAX_M:
                continue
            if name not in by_name or median_d < by_name[name]:
                by_name[name] = median_d

        if not by_name:
            no_parent += 1
            continue

        ranked = sorted(by_name.values())
        parent_distances.append(ranked[0])
        if len(ranked) == 1:
            clear += 1
            only_one_name += 1
            continue
        near, other = ranked[0], ranked[1]
        if other <= 0:
            clear += 1
            continue
        r = near / other
        ratios.append(r)
        if r <= AMBIGUOUS_RATIO:
            clear += 1
        else:
            ambiguous += 1

    total = clear + ambiguous + no_parent
    if not total:
        print("no sidewalks measured")
        return 1

    ratios.sort()
    parent_distances.sort()

    def pct(n):
        return f"{n:,} ({n / total * 100:.1f}%)"

    print("\n=== CAN A SIDEWALK BE GIVEN THE RIGHT STREET NAME? ===")
    print(f"  sidewalks evaluated: {total:,} "
          f"(>= {MIN_SIDEWALK_LEN_M:g}m, sampled seed {args.seed})")
    print(f"  already carry their own name: {pct(own_name)} "
          f"-- these win over any derived name\n")
    print(f"  CLEAR     (one name in range, or nearest <= "
          f"{AMBIGUOUS_RATIO:g}x the runner-up): {pct(clear)}")
    print(f"      of which only ONE named street was in range: "
          f"{only_one_name:,}")
    print(f"  AMBIGUOUS (two different names comparably close): "
          f"{pct(ambiguous)}")
    print(f"  NO PARENT (no named street within {PARENT_MAX_M:g}m): "
          f"{pct(no_parent)}")

    if ratios:
        print(f"\n  near/far name ratio: median "
              f"{ratios[len(ratios) // 2]:.3f}, 75th "
              f"{ratios[int(len(ratios) * .75)]:.3f}, 90th "
              f"{ratios[int(len(ratios) * .90)]:.3f}")
    if parent_distances:
        print(f"  distance to assigned parent: median "
              f"{parent_distances[len(parent_distances) // 2]:.1f}m, 90th "
              f"{parent_distances[int(len(parent_distances) * .90)]:.1f}m, "
              f"max {parent_distances[-1]:.1f}m")

    print(f"\n  => a derived name is safe for {clear / total * 100:.1f}% "
          f"of sidewalk length-carrying ways;")
    print(f"     {(ambiguous + no_parent) / total * 100:.1f}% would emit no "
          f"name rather than guess.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
