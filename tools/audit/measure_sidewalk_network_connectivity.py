"""Is OSM's NYC sidewalk network actually disconnected, or did we measure
a subset that could not possibly be connected?

WHY
---
A standing project claim (CLAUDE.md) says OSM's sidewalk layer "splits
into 43,294 disconnected components, the largest holding 0.2% of its
nodes ... so it cannot be routed on as-is", and that claim is the stated
justification for building on street centerlines instead.

But that number came from the `any_sidewalks_*.graphml` cache, which is
ANY_SIDEWALK_FILTER's output -- `["footway"="sidewalk"]` and nothing
else. Sidewalks do not join to each other directly; they join THROUGH
crossings. Measuring sidewalks with the crossings removed guarantees one
fragment per block face no matter how well OSM is mapped, so the figure
may be an artifact of the subset rather than a fact about OSM.

This settles it by building three graphs from the pinned extract and
comparing:

  A  footway=sidewalk only            (reproduces the standing claim)
  B  sidewalk + footway=crossing      (adds what actually joins them)
  C  every pedestrian-usable way      (sidewalks, crossings, centerlines,
                                       paths, steps -- the real network)

If B and C are well connected, the justification for discarding the
sidewalk layer does not hold as stated, and the 81% of ledger repairs
that exist to patch our own exclusion have no underlying cause.

Local only -- no network, no OSRM.
"""
import math
import os
import sys
from collections import Counter, defaultdict

import osmium
from osmium.filter import EntityFilter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

EXTRACT = os.path.join(REPO, "data", "oracle", "new-york-latest.osm.pbf")

# Every highway value a pedestrian can use. Kept deliberately BROADER than
# the sidewalk-only model's own filter: this script's whole job is to
# compare progressively wider slices of OSM, so its widest slice has to
# include street centerlines too.
WALKABLE_HIGHWAY = {
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "pedestrian", "footway", "path", "steps",
    "service", "bridleway", "track",
}


def is_reality(tags):
    """Any mapped way a pedestrian can actually walk, sidewalks and
    crossings included.

    Inlined rather than imported: the module this used to come from
    (`verify_connections_against_osm.py`) audited the retired gap ledger
    and is not committed, so importing it would leave this script broken
    on a fresh checkout.
    """
    hw = tags.get("highway")
    if hw == "cycleway":
        return tags.get("foot") in ("designated", "yes")
    if hw not in WALKABLE_HIGHWAY or tags.get("area") == "yes":
        return False
    if tags.get("foot") in ("no", "private"):
        return False
    # An explicit foot=designated|yes overrides access=private|no --
    # OSM's own tag hierarchy, and real on NYC bridge walkways.
    blocked = tags.get("access") in ("private", "no")
    overridden = tags.get("foot") in ("designated", "yes")
    if blocked and not overridden:
        return False
    if tags.get("service") in ("private", "driveway"):
        return False
    return True

K = 111320.0 * math.cos(math.radians(40.7))
LAT_M = 110540.0


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


def classify(tags):
    """Which of the three graphs this way belongs to."""
    hw = tags.get("highway")
    fw = tags.get("footway")
    sets = []
    if hw == "footway" and fw == "sidewalk":
        sets += ["A", "B"]
    elif hw == "footway" and fw == "crossing":
        sets += ["B"]
    if is_reality(tags):
        sets += ["C"]
    return sets


def report(name, dsu, nodes, km):
    if not nodes:
        print(f"  {name}: empty")
        return
    sizes = Counter(dsu.find(n) for n in nodes)
    ordered = sorted(sizes.values(), reverse=True)
    total = sum(ordered)
    top = ordered[0]
    # How much of the network sits in components too small to be useful?
    tiny = sum(s for s in ordered if s < 50)
    print(f"  {name}")
    print(f"    {len(nodes):,} nodes / {km:,.0f} km")
    print(f"    components: {len(ordered):,}")
    print(f"    largest: {top:,} nodes ({top / total * 100:.1f}% of the layer)")
    print(f"    in fragments under 50 nodes: {tiny:,} ({tiny / total * 100:.1f}%)")


def main():
    graphs = {k: DSU() for k in "ABC"}
    nodes = {k: set() for k in "ABC"}
    km = defaultdict(float)
    coords = {}

    fp = (osmium.FileProcessor(EXTRACT, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(EntityFilter(osmium.osm.WAY)))
    seen = 0
    for way in fp:
        tags = dict(way.tags)
        if not tags:
            continue
        sets = classify(tags)
        if not sets:
            continue
        seen += 1
        pts = []
        for n in way.nodes:
            if n.location.valid():
                pts.append((n.ref, n.lon, n.lat))
        if len(pts) < 2:
            continue
        length = 0.0
        for (_, x0, y0), (_, x1, y1) in zip(pts, pts[1:]):
            length += math.hypot((x1 - x0) * K, (y1 - y0) * LAT_M)
        for s in sets:
            km[s] += length / 1000.0
            prev = None
            for nid, lon, lat in pts:
                coords.setdefault(nid, (lon, lat))
                nodes[s].add(nid)
                if prev is not None:
                    graphs[s].union(prev, nid)
                prev = nid

    print(f"ways read: {seen:,}\n")
    print("=== HOW CONNECTED IS OSM'S PEDESTRIAN LINE-WORK? ===\n")
    report("A  sidewalks ONLY (what the standing claim measured)",
           graphs["A"], nodes["A"], km["A"])
    print()
    report("B  sidewalks + crossings (what actually joins them)",
           graphs["B"], nodes["B"], km["B"])
    print()
    report("C  every pedestrian-usable way (the real network)",
           graphs["C"], nodes["C"], km["C"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
