"""Does a tree's coordinate land on the block face we think it does?

THE QUESTION
------------
Shade credit flows: tree -> nearest kerb -> that kerb's `blockf_id` -> a
block face -> every sidewalk edge on that face. The first hop is a
geometric GUESS and it has never been checked. If it is wrong the shade
lands on the wrong block: the cross street gets canopy it does not have,
and the tree's real block reads barer than it is.

This is the corner problem, one dataset over. At an intersection the cross
street's kerb can easily be a metre nearer than the kerb of the street the
tree actually belongs to.

WHAT MAKES THIS MEASURABLE -- ground truth, not a proxy
-------------------------------------------------------
NYC publishes a second dataset of the PLANTING SPACES (tree pits), and
unlike the tree records those carry a street name and a `physicalid` --
the CSCL segment id our block faces hang off. Every tree names its own
planting space. So:

    city says   tree -> plantingspaceglobalid -> planting space physicalid
    we say      tree -> nearest kerb -> blockf_id -> CSCL row -> physicalid

Both sides land on a CSCL segment id and can be compared directly. This is
NYC's own paperwork, independent of our geometry -- not another geometric
measure agreeing with itself.

WHAT IT CANNOT SETTLE
---------------------
`physicalid` names the SEGMENT, not the side. A block face is segment x
side. The side half is covered separately by test_tree_side_assignment.py
(96.7% of trees are decisively nearer one pavement, median 8x closer).

TRAPS, BOTH ALREADY HIT
-----------------------
  - `physicalid` FORMAT DIFFERS BETWEEN THE TWO SOURCES. CSCL writes
    `46810`; planting spaces writes `0005067` -- AND sometimes `12517`,
    unpadded, in the same column. Comparing raw strings scores 39.3%
    instead of 97.7%, which is a deceptive number: not the clean 0% that
    would obviously read as a bug. Normalise with int().
  - Do not quote an agreement rate without the UNAMBIGUOUS subset beside
    it. If trees that are nowhere near a corner also disagree, the
    instrument is broken and there is no finding. That calibration is the
    step whose absence produced three false alarms on 2026-08-23.

Read-only. ~10 min, dominated by the per-tree nearest-kerb search.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from shapely.geometry import Point
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402
from pipeline.fetch import socrata  # noqa: E402
from pipeline.graph.naming import _to_m  # noqa: E402
from tools.audit import fetch_planimetrics  # noqa: E402
from pipeline.graph.blockface import (  # noqa: E402
    build_face_lookup, build_kerb_index,
)

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

# Beyond this a tree is not beside a mapped kerb at all (park interior,
# private land) and gets no answer rather than a forced one.
SEARCH_M = 25.0

# The nearest kerb must be this much closer than the nearest kerb of a
# DIFFERENT segment to count as decisive. Same ratio naming.py and
# test_tree_side_assignment.py use, deliberately.
AMBIGUOUS_RATIO = 0.6


# CSCL abbreviates, the planting-space list spells out: HOPE AVE vs HOPE
# AVENUE, SCHIEFFELIN AVE vs SCHIEFFELIN AVENUE. Without expansion those
# score as DIFFERENT STREETS, which turned a benign
# adjacent-block disagreement into an apparent 97.3% catastrophe. The same
# trap is already recorded for the CSCL-vs-OSM comparison; it was not
# applied here first time round.
_STREET_WORDS = {
    "AVE": "AVENUE", "AV": "AVENUE", "ST": "STREET", "STR": "STREET",
    "RD": "ROAD", "DR": "DRIVE", "BLVD": "BOULEVARD", "PL": "PLACE",
    "LN": "LANE", "CT": "COURT", "TER": "TERRACE", "TERR": "TERRACE",
    "PKWY": "PARKWAY", "PKY": "PARKWAY", "EXPY": "EXPRESSWAY",
    "EXPWY": "EXPRESSWAY", "HWY": "HIGHWAY", "SQ": "SQUARE",
    "CIR": "CIRCLE", "PLZ": "PLAZA", "BRG": "BRIDGE", "TPKE": "TURNPIKE",
    "CONC": "CONCOURSE", "BCH": "BEACH", "PKWY": "PARKWAY",
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
}


def same_street(a: str, b: str) -> bool:
    """Do these two spellings name the same street?

    Exact match after normalising, OR one contained in the other. The
    containment arm is the general fix -- enumerating abbreviations is
    whack-a-mole (GRAND CONC / GRAND CONCOURSE, VAN WYCK EXPY / VAN WYCK
    EXPRESSWAY SR WEST) and a missed one shows up as a fake disagreement.

    Deliberately CONSERVATIVE: it errs toward calling two names the same.
    Both callers use it to EXCLUDE non-conflicts, so a false "same" costs
    one sample while a false "different" wastes a reviewer's judgement on
    two spellings of one street."""
    x, y = _norm_street(a), _norm_street(b)
    if not x or not y:
        return False
    return x == y or x in y or y in x


def _norm_street(name: str) -> str:
    """Both sources uppercase, but they punctuate and abbreviate
    differently. Expand the abbreviations, drop punctuation, drop ordinal
    suffixes (3 RD vs 3RD), then compare with spaces removed."""
    if not name:
        return ""
    out = name.upper()
    for ch in ".,'-/":
        out = out.replace(ch, " ")
    words = []
    for word in out.split():
        # 3RD -> 3, 14TH -> 14, so 5 AVENUE and 5TH AVENUE agree
        if len(word) > 2 and word[:-2].isdigit() and word[-2:] in (
                "ST", "ND", "RD", "TH"):
            word = word[:-2]
        words.append(_STREET_WORDS.get(word, word))
    return "".join(words)


def normalise_id(value) -> int | None:
    """CSCL `46810` vs planting-space `0005067` vs `12517`. int() is the
    only thing all three agree on. See the module docstring."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def city_ground_truth() -> dict:
    """tree globalid -> (segment id, street name), per NYC's own records."""
    link = socrata.fetch_all_rows(
        dataset_id="hn5i-inap", where="tpstructure = 'Full'",
        select="globalid, plantingspaceglobalid", order="globalid",
        cache_name="tree_planting_link_v1")
    spaces = socrata.fetch_all_rows(
        dataset_id="82zj-84is", where="physicalid IS NOT NULL",
        select="globalid, physicalid, streetname", order="globalid",
        cache_name="planting_spaces_v1")

    by_space = {}
    for row in spaces:
        segment = normalise_id(row.get("physicalid"))
        if segment is not None and row.get("globalid"):
            by_space[str(row["globalid"]).upper()] = (
                segment, (row.get("streetname") or "").strip())

    truth = {}
    for row in link:
        space = row.get("plantingspaceglobalid")
        if not space or not row.get("globalid"):
            continue
        hit = by_space.get(str(space).upper())
        if hit:
            truth[str(row["globalid"]).upper()] = hit
    return truth, len(link), len(by_space)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trees",
                    default="data/raw/socrata/trees_citywide_v3.json")
    ap.add_argument("--out",
                    default="data/audits/2026-08-23/tree_block_assignment.json")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many trees WITH ground truth — for "
                         "smoke-testing on a small real slice before paying "
                         "for the full ~10 min pass")
    args = ap.parse_args()
    started = time.perf_counter()

    def elapsed():
        return time.perf_counter() - started

    truth, n_link, n_space = city_ground_truth()
    print(f"[{elapsed():5.1f}s] tree->space links {n_link:,}, "
          f"planting spaces with a segment {n_space:,}, "
          f"trees with ground truth {len(truth):,}", flush=True)

    kerb_index, kerbs, kerb_face, kerb_conflated = build_kerb_index(
        fetch_planimetrics.load("pavement_edge"))
    faces = build_face_lookup(fetch_planimetrics.load("cscl"))
    # blockf_id -> the CSCL segment that face belongs to
    face_segment = {face: normalise_id(meta.segment_id)
                    for face, meta in faces.items()}
    print(f"[{elapsed():5.1f}s] {len(kerbs):,} kerbs, {len(faces):,} faces",
          flush=True)

    with open(args.trees) as fh:
        trees = json.load(fh)
    print(f"[{elapsed():5.1f}s] {len(trees):,} trees", flush=True)

    agree = disagree = no_kerb = no_face = 0
    same_street_n = other_street = unnamed = 0
    city_far = []
    decisive_agree = decisive_total = 0
    ambiguous = 0
    distances, ratios = [], []
    per_boro = {}
    checked = 0

    for n, tree in enumerate(trees):
        gid = str(tree.get("globalid") or "").upper()
        known = truth.get(gid)
        if known is None:
            continue
        point_ll = (tree.get("location") or {}).get("coordinates")
        if not point_ll:
            continue
        checked += 1
        point = Point(*_to_m(point_ll[0], point_ll[1]))

        # ONE index query, then keep the best per segment. Querying twice
        # -- once for the nearest and again for the nearest of a different
        # segment -- doubles the cost of the only expensive step here.
        nearest_by_segment = {}
        best = None
        for position in kerb_index.query(point.buffer(SEARCH_M)):
            if not kerb_conflated[position]:
                continue
            face = kerb_face[position]
            segment = face_segment.get(face)
            if segment is None:
                continue
            d = point.distance(kerbs[position])
            if segment not in nearest_by_segment or d < nearest_by_segment[segment]:
                nearest_by_segment[segment] = d
            if best is None or d < best[0]:
                best = (d, face, segment)
        if best is None:
            no_kerb += 1
            continue
        # How far is the tree from a kerb of the segment the CITY names?
        # If that kerb is also within a metre or two, the tree sits at a
        # corner where both are true and the two sources are answering
        # different questions (we ask which pavement it shades; NYC records
        # which street it is ADDRESSED to). If it is far, we are simply wrong.
        city_dist = nearest_by_segment.get(known[0])
        others = [d for s, d in nearest_by_segment.items() if s != best[2]]
        best_other = min(others) if others else None

        distances.append(best[0])
        ours, theirs = best[2], known[0]
        correct = ours == theirs
        agree += correct
        disagree += not correct

        decisive = False
        if best_other and best_other > 0:
            ratio = best[0] / best_other
            ratios.append(ratio)
            decisive = ratio <= AMBIGUOUS_RATIO
        elif best_other is None:
            decisive = True  # only one segment anywhere near: unambiguous
        if decisive:
            decisive_total += 1
            decisive_agree += correct
        else:
            ambiguous += 1

        # WHEN WE DISAGREE, IS IT THE SAME STREET? A different segment of
        # the same street is a block-boundary effect. A different street is
        # a real mis-assignment. The raw agreement rate cannot tell them
        # apart, and they mean completely different things for shade.
        if not correct:
            ours_name = faces[best[1]].street
            theirs_name = known[1]
            if ours_name and theirs_name:
                if same_street(ours_name, theirs_name):
                    same_street_n += 1
                else:
                    other_street += 1
            else:
                unnamed += 1
            if city_dist is None:
                city_far.append(999.0)
            else:
                city_far.append(city_dist)

        boro = BOROUGHS.get(faces[best[1]].boro, "?")
        slot = per_boro.setdefault(boro, [0, 0])
        slot[0] += correct
        slot[1] += 1

        if (n + 1) % 200000 == 0:
            print(f"[{elapsed():5.1f}s] scanned {n + 1:,}/{len(trees):,}",
                  flush=True)
        if args.limit and checked >= args.limit:
            print(f"[{elapsed():5.1f}s] --limit {args.limit:,} reached, "
                  f"stopping early", flush=True)
            break

    if not checked:
        print("no trees had ground truth -- check the join")
        return 1

    total = agree + disagree
    d = np.array(distances)
    print("\n" + "=" * 74)
    print("DOES OUR NEAREST-KERB RULE PICK THE BLOCK NYC SAYS?")
    print("=" * 74)
    print(f"  trees with city ground truth : {checked:,}")
    print(f"  no conflated kerb within {SEARCH_M:.0f}m : {no_kerb:,}")
    print(f"  compared                     : {total:,}")
    print(f"\n  AGREE    : {agree:,} ({100 * agree / total:.2f}%)")
    print(f"  disagree : {disagree:,} ({100 * disagree / total:.2f}%)")

    print("\n" + "-" * 74)
    print("CALIBRATION -- the unambiguous subset must be near-perfect, or")
    print("the instrument is broken and the rate above means nothing.")
    print("-" * 74)
    if decisive_total:
        print(f"  decisive (nearest kerb <= {AMBIGUOUS_RATIO:g}x the nearest "
              f"kerb of another segment)")
        print(f"    tested : {decisive_total:,} "
              f"({100 * decisive_total / total:.1f}% of all)")
        print(f"    AGREE  : {decisive_agree:,} "
              f"({100 * decisive_agree / decisive_total:.2f}%)")
    print(f"  ambiguous (a second segment is comparably close): "
          f"{ambiguous:,} ({100 * ambiguous / total:.1f}%)")
    if ambiguous:
        amb_agree = agree - decisive_agree
        print(f"    AGREE among those: {amb_agree:,} "
              f"({100 * amb_agree / ambiguous:.2f}%)")

    if disagree:
        print("\n" + "-" * 74)
        print("WHAT KIND OF DISAGREEMENT? same street = block-boundary "
              "effect,\ndifferent street = real mis-assignment.")
        print("-" * 74)
        print(f"  SAME street, different segment : {same_street_n:,} "
              f"({100 * same_street_n / disagree:.1f}% of disagreements)")
        print(f"  DIFFERENT street               : {other_street:,} "
              f"({100 * other_street / disagree:.1f}%)")
        print(f"  one side unnamed               : {unnamed:,} "
              f"({100 * unnamed / disagree:.1f}%)")

    if city_far:
        cf = np.array(city_far)
        reach = cf[cf < 900]
        print(f"\n  DISAGREEMENTS -- distance to a kerb of the CITY's segment:")
        print(f"    within 25m at all : {len(reach):,} of {len(cf):,} "
              f"({100*len(reach)/len(cf):.1f}%)")
        if len(reach):
            print("    of those: median %.2fm  p75 %.2fm  p90 %.2fm" % tuple(
                np.percentile(reach, q) for q in (50, 75, 90)))
            for cut in (3.0, 6.0, 12.0):
                print(f"    city's segment within {cut:4.1f}m : "
                      f"{100*(reach<=cut).sum()/len(cf):5.1f}% of disagreements")

    print(f"\n  distance to the assigned kerb: p10 {np.percentile(d, 10):.2f}m  "
          f"median {np.percentile(d, 50):.2f}m  p90 {np.percentile(d, 90):.2f}m  "
          f"p99 {np.percentile(d, 99):.2f}m")
    print("  (street trees sit in pits between kerb and sidewalk, so a "
          "median\n   of roughly 1-3m is the expected reading)")

    print(f"\n  {'borough':16s} {'trees':>10s} {'agree':>9s}")
    for name in sorted(per_boro):
        ok, n = per_boro[name]
        print(f"  {name:16s} {n:10,} {100 * ok / n:8.2f}%")

    summary = {
        "trees_with_truth": int(checked),
        "compared": int(total),
        "agree_pct": round(100 * agree / total, 2),
        "decisive_tested": int(decisive_total),
        "decisive_agree_pct": round(
            100 * decisive_agree / decisive_total, 2) if decisive_total else None,
        "ambiguous_pct": round(100 * ambiguous / total, 2),
        "median_kerb_dist_m": round(float(np.percentile(d, 50)), 2),
        "by_borough": {k: round(100 * v[0] / v[1], 2) for k, v in per_boro.items()},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\n[{elapsed():5.1f}s] wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
