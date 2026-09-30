"""How often is a derived sidewalk name RIGHT, ABSENT, or WRONG?

The holdout test for the naming derivation (pipeline/graph/naming.py).

RECORDED BASELINES (street-achievable subset, by length, RIGHT/none/WRONG)
--------------------------------------------------------------------------
2026-08-23 run: 43.3% precision overall, 81.8% near-street.
2026-08-28 grid (reproduced those exactly, then decided the rule):
  nearest street, no parallel filter:   69.8 / 24.8 / 5.4
  nearest PARALLEL street (ADOPTED):    75.9 / 20.6 / 3.5   -- and it
    names 27.5% more edges citywide (249,931 vs 196,055), keeping 98.2%
    of previously-named length. naming.py ships this.
  parallel + face-cscl must agree:      66.2 / 32.3 / 1.5   -- declined:
    coverage over the last halving of wrong (the drawn route is the
    authority and never depends on names). Re-decide after alias
    normalization (OSM '6th Avenue' vs CSCL 'Avenue of the Americas'),
    which is most of its coverage cost.

METHOD
------
Every edge that carries its OWN OSM name is ground truth (~12k edges).
Blank those names, let each naming source answer, and score three buckets:

  RIGHT   derived name == the hidden real name (normalized)
  NONE    the source declined ("unnamed path" in the app)
  WRONG   a confident different name -- the bucket that sends a walker
          down the wrong street. The product bar: "unnamed path" beats a
          wrong street name, so THIS bucket is
          what any chosen policy must drive toward zero.

SOURCES
-------
  naming.py       the SHIPPED derivation (nearest parallel street) --
                  this tool measures whatever ships, so a naming.py
                  change re-measures itself here.
  face-cscl       the block face's CSCL street name (blockface.py conflates
                  every sidewalk to a face for tree scoring; the face's
                  `street` attribute is otherwise unused for naming).
                  CSCL attributes only -- no centerline geometry.
  Policies: each alone, agreement-only ("P6", the drawer option), and
  agreement-or-single.

CAVEATS THE NUMBERS CARRY
-------------------------
- The truth set is BIASED: mappers name special ways (promenades,
  greenways, named park paths), not ordinary sidewalks. So results are
  also split over the "street-achievable" subset -- edges whose true name
  belongs to a street way within 100m -- which is the population the
  derivation could ever get right. The all-edges figure is the honest
  floor, not the expected field performance.
- Counts and length-weighted shares both reported (a rule that fails only
  on 3m nubs is a different animal from one that fails on 300m runs).
- CSCL spells names 'W  60 ST' where OSM writes 'West 60th Street';
  _normalize() bridges them. Unfixable mismatch examples are printed so
  the normalizer's own misses are visible rather than silently counted
  as WRONG.

Run from the repo root (~4 min, all inputs cached locally):

    uv run python tools/audit/measure_naming_precision.py
"""

import logging
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shapely.geometry import LineString
from shapely.strtree import STRtree

from pipeline import config
from pipeline.fetch import planimetrics
from pipeline.fetch.boundaries import fetch_borough_boundaries
from pipeline.graph import naming, pedestrian
from pipeline.graph.blockface import BlockFaceIndex
from pipeline.graph.boundary import nyc_boundary

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# How far to look for a street carrying the edge's TRUE name when deciding
# whether that truth was achievable by street-derivation at all. Matches
# the 100m cut the 2026-08-23 run reported (the 81.8% figure).
ACHIEVABLE_STREET_M = 100.0

_CSCL_SUFFIX = {
    "st": "street", "ave": "avenue", "blvd": "boulevard", "rd": "road",
    "dr": "drive", "pl": "place", "pkwy": "parkway", "expy": "expressway",
    "hwy": "highway", "tpke": "turnpike", "ln": "lane", "ter": "terrace",
    "sq": "square", "cir": "circle", "plz": "plaza", "br": "bridge",
    "pd": "road", "al": "alley", "ct": "court", "cres": "crescent",
    "bl": "boulevard", "pz": "plaza", "pky": "parkway", "expwy": "expressway",
}
_CSCL_LEADING = {"w": "west", "e": "east", "n": "north", "s": "south",
                 "st": "saint", "ft": "fort", "mt": "mount"}
_ORDINAL_EXCEPTIONS = {"11": "11th", "12": "12th", "13": "13th"}


def _ordinal(token: str) -> str:
    if token in _ORDINAL_EXCEPTIONS:
        return token + "th"
    if token.endswith(("11", "12", "13")):
        return token + "th"
    last = token[-1]
    return token + {"1": "st", "2": "nd", "3": "rd"}.get(last, "th")


_DIRECTION = {"n": "north", "s": "south", "e": "east", "w": "west"}


def _normalize(name: str, cscl: bool) -> str:
    """Both spellings onto one comparison form, lowercase full words with
    bare numbers as ordinals ('W  60 ST' and 'West 60th Street' both
    become 'west 60th street'; 'CRAIG RD S' -> 'craig road south').

    'ST' is genuinely ambiguous: leading or mid-name it reads Saint
    (ST NICHOLAS AVE, OLD ST JAMES PL), last it reads Street. 'AVE N'
    keeps its trailing letter -- Brooklyn's lettered avenues (Avenue
    N/S/U/W...) are the one family where a trailing direction letter IS
    the name."""
    tokens = re.sub(r"[.']", "", name.lower()).split()
    out = []
    for i, token in enumerate(tokens):
        last = i == len(tokens) - 1
        if cscl and i == 0 and not last and token in _CSCL_LEADING:
            out.append(_CSCL_LEADING[token])
            continue
        if cscl and token == "st":
            out.append("street" if last else "saint")
            continue
        if cscl and token in _CSCL_SUFFIX:
            out.append(_CSCL_SUFFIX[token])
            continue
        if cscl and last and i > 0 and token in _DIRECTION:
            # 'avenue n' family keeps the letter; anywhere else a trailing
            # single letter is a direction ('craig rd s')
            out.append(token if out[-1] == "avenue" and i == 1
                       else _DIRECTION[token])
            continue
        if token.isdigit():
            out.append(_ordinal(token))
            continue
        # OSM writes ordinals already ('60th'); keep them
        out.append(token)
    return " ".join(out)


def _street_name_index(street_ways):
    """STRtree over metric street geometries + their normalized names, for
    the 'was the truth achievable' split."""
    geoms, names = [], []
    for way in street_ways:
        pts = [naming._to_m(lon, lat) for lon, lat in zip(way.lons, way.lats)]
        if len(pts) < 2:
            continue
        geoms.append(LineString(pts))
        names.append(_normalize(way.name, cscl=False))
    return STRtree(geoms), geoms, names


def _bucket(derived: str, truth: str) -> str:
    if not derived:
        return "NONE"
    return "RIGHT" if derived == truth else "WRONG"


def _report(title, rows):
    """rows: (bucket, length_m). Prints count and length-weighted shares."""
    n = len(rows)
    total_len = sum(l for _, l in rows) or 1.0
    counts = Counter(b for b, _ in rows)
    lengths = Counter()
    for b, l in rows:
        lengths[b] += l
    parts_n = "  ".join(f"{b} {counts.get(b, 0) / n * 100:5.1f}%"
                        for b in ("RIGHT", "NONE", "WRONG"))
    parts_l = "  ".join(f"{b} {lengths.get(b, 0.0) / total_len * 100:5.1f}%"
                        for b in ("RIGHT", "NONE", "WRONG"))
    print(f"  {title:34s} n={n:>6,}  by count: {parts_n}")
    print(f"  {'':34s} {'':>9} by length: {parts_l}")


def main() -> None:
    t0 = time.time()
    nyc_shape = nyc_boundary(fetch_borough_boundaries())
    ped_ways, street_ways = pedestrian.read_ways(config.OSM_EXTRACT_PATH, nyc_shape)
    _, edges = pedestrian.build_graph(ped_ways)
    print(f"[{time.time() - t0:.0f}s] {len(edges):,} edges")

    holdout = [e for e in edges if e["name"]]
    kind_dist = Counter(e["kind"] for e in holdout)
    print(f"holdout (own-named edges): {len(holdout):,}; top kinds: "
          f"{kind_dist.most_common(5)}")

    # SOURCE 1: osm-proximity, with the holdout names hidden. Streets lend
    # names, edges never do, so blanking all holdout names at once leaks
    # nothing between them.
    blanked = []
    for e in edges:
        copy = dict(e)
        if copy["name"]:
            copy["name"] = ""
        blanked.append(copy)
    naming.assign_parent_names(blanked, street_ways)
    print(f"[{time.time() - t0:.0f}s] source 1 (shipped naming.py rule) "
          f"derived; names {sum(1 for b in blanked if b['name']):,} edges")

    # SOURCE 2: the block face's CSCL street name, dominant face by length.
    index = BlockFaceIndex(planimetrics.load("pavement_edge"),
                           planimetrics.load("cscl"))
    print(f"[{time.time() - t0:.0f}s] block-face index built")

    tree, street_geoms, street_names = _street_name_index(street_ways)

    per_edge = []  # (truth, s1, s2, length_m, achievable, kind, coords)
    for e, b in zip(edges, blanked):
        if not e["name"]:
            continue
        truth = _normalize(e["name"], cscl=False)
        s1 = _normalize(b["name"], cscl=False) if b["name"] else ""

        profile = index.match_line_profile(e["coords"])
        s2 = ""
        if profile:
            dominant = max(profile, key=profile.get)
            face = index._faces.get(dominant)
            if face is not None and face.street:
                s2 = _normalize(face.street, cscl=True)

        line = LineString([naming._to_m(lon, lat) for lon, lat in e["coords"]])
        achievable = False
        for pos in tree.query(line.buffer(ACHIEVABLE_STREET_M)):
            if street_names[pos] == truth and \
                    street_geoms[pos].distance(line) <= ACHIEVABLE_STREET_M:
                achievable = True
                break
        per_edge.append((truth, s1, s2, e["length_m"], achievable,
                         e["kind"], e["coords"]))
    print(f"[{time.time() - t0:.0f}s] all sources scored on "
          f"{len(per_edge):,} holdout edges\n")

    policies = {
        "P1 shipped naming.py rule": lambda s1, s2: s1,
        "P2 face-cscl alone": lambda s1, s2: s2,
        "P3 both agree, else decline (P6)": lambda s1, s2: (
            s1 if s1 and s1 == s2 else ""),
        "P4 agree, or the only answer": lambda s1, s2: (
            s1 if s1 and s1 == s2 else ("" if (s1 and s2) else s1 or s2)),
    }

    for pname, rule in policies.items():
        print(pname)
        all_rows, ach_rows = [], []
        for truth, s1, s2, length_m, achievable, kind, coords in per_edge:
            b = _bucket(rule(s1, s2), truth)
            all_rows.append((b, length_m))
            if achievable:
                ach_rows.append((b, length_m))
        _report("all holdout edges", all_rows)
        _report(f"true name is a street <= {ACHIEVABLE_STREET_M:.0f}m away",
                ach_rows)
        print()

    # The dangerous residue: WRONG under the strictest policy. Each one is
    # the parallel derivation and the city record agreeing on a name that
    # is not the real one.
    print("P3 WRONG examples (both sources agree, truth differs):")
    shown = 0
    for truth, s1, s2, length_m, achievable, kind, coords in per_edge:
        if s1 and s1 == s2 and s1 != truth:
            lon, lat = coords[len(coords) // 2]
            print(f"  {truth!r} <- called {s1!r} ({kind}, {length_m:.0f}m) "
                  f"https://www.google.com/maps/@{lat},{lon},19z")
            shown += 1
            if shown >= 15:
                break

    # Normalizer misses masquerading as WRONG: source 2 wrong but sharing
    # a token with the truth is usually a spelling bridge not yet built.
    print("\npossible normalizer gaps (face-cscl wrong but token-overlapping):")
    seen_gaps = set()
    for truth, s1, s2, length_m, achievable, kind, coords in per_edge:
        if s2 and s2 != truth and set(truth.split()) & set(s2.split()):
            if (truth, s2) in seen_gaps:
                continue
            seen_gaps.add((truth, s2))
            print(f"  truth {truth!r} vs cscl {s2!r}")
            if len(seen_gaps) >= 15:
                break

    # COVERAGE in production: the holdout is biased toward special named
    # ways, so it understates how much of the ORDINARY sidewalk network
    # keeps a name under the agree-only drawer option. Ground truth doesn't
    # exist there, but agreement does: over a seeded sample of edges that
    # get a derived name from the shipped rule, how often does the face
    # name agree (= P3 keeps the name)?
    rng = random.Random(20260827)
    derived_edges = [(e, b) for e, b in zip(edges, blanked)
                     if not e["name"] and b["name"]]
    sample = rng.sample(derived_edges, min(20_000, len(derived_edges)))
    p3_n = p3_len = total_len = 0.0
    for e, b in sample:
        s1 = _normalize(b["name"], cscl=False)
        profile = index.match_line_profile(e["coords"])
        s2 = ""
        if profile:
            face = index._faces.get(max(profile, key=profile.get))
            if face is not None and face.street:
                s2 = _normalize(face.street, cscl=True)
        total_len += e["length_m"]
        if s1 == s2:
            p3_n += 1
            p3_len += e["length_m"]
    n = len(sample)
    print(f"\nproduction coverage (seeded sample of {n:,}/"
          f"{len(derived_edges):,} edges named by the shipped rule; share "
          f"the agree-only drawer option would KEEP): "
          f"{p3_n / n * 100:.1f}% by count, "
          f"{p3_len / total_len * 100:.1f}% by length")
    print(f"\n[{time.time() - t0:.0f}s] done")


if __name__ == "__main__":
    main()
