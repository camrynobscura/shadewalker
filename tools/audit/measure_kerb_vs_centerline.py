"""Is the kerb a better anchor for a sidewalk than the street centerline?

THE QUESTION
------------
A sidewalk has to be attached to SOMETHING before its trees can be counted
against a sensible length. The obvious anchor is the street centerline, and
it is the wrong one: the centerline sits in the middle of the roadway, so
its distance to the sidewalk is half the road width. That is a property of
the road, not of the relationship, and it means no single threshold can be
right for both a side street and a boulevard.

A kerb is the roadway's EDGE. A sidewalk sits beside it at roughly the same
distance whatever the street's width. This measures whether that holds.

WHAT IT REPORTS
---------------
  REGISTRATION      how far an OSM sidewalk actually is from NYC's kerb.
                    Two independently-surveyed datasets have to agree
                    geometrically before anything else matters.
  SCALE-INVARIANCE  gap vs street width, for both anchors. This is the
                    whole question -- the centerline's gap must grow with
                    width, and the kerb's must not.
  COVERAGE          share of sidewalk length that finds a usable block
                    face, per borough. A citywide average can hide a
                    borough failing outright.
  AMBIGUITY         how often a runner-up block face of a DIFFERENT street
                    is nearly as close -- the corner problem that defeated
                    every centerline threshold.

Everything here is a full-population measurement needing no name ground
truth. Read-only; consumes measure_sidewalk_kerb_match.py's output.

DELETED ON PURPOSE -- DO NOT REBUILD
------------------------------------
An "over-collection" section once divided the sidewalk assigned to a block
face by that face's own CSCL CENTERLINE length, to ask whether a face
collects roughly its own length. It reported that 13% of the city's
sidewalk sat on faces collecting >2x their length, and that number is
WRONG. A CSCL centerline segment can be far shorter than the run of kerb
conflated to it, so the ratio flags ordinary blocks. measure_edge_block_
alignment.py then measured the same question without CSCL and found edges
are aligned: 93.5% lie entirely within one face and only 2.0% of length is
miscredited.

The lesson is the module's own thesis turned on itself: the centerline is
the wrong instrument for measuring a sidewalk's relationship to a block,
and that is just as true when it is used as a RULER as when it was used as
a threshold. CSCL is now attributes only -- which side, what name. No
measurement in this project should use centerline geometry.

A second trap in the same family: do not reason about where an edge is
from its MIDPOINT. Two 40m pieces laid end to end have midpoints 40m apart
but cover 80m of ground, so any "extent" built from midpoints understates
reality by about one piece and manufactures exactly the anomaly you went
looking for. Midpoints in the .npz exist for ONE thing: a coordinate to
stand at in a Street View link.
"""
import argparse

import numpy as np

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}


def weighted_percentile(values, weights, q):
    """Length-weighted percentile -- a 200m sidewalk should count for more
    than a 6m one when describing 'the typical gap'."""
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights) / weights.sum()
    return float(values[np.searchsorted(cumulative, q / 100.0)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table",
                    default="data/audits/2026-08-23/sidewalk_kerb_match.npz")
    args = ap.parse_args()

    # np.load on an .npz is LAZY -- each subscript re-inflates that whole
    # column out of the zip. Harmless for the once-per-section reads here,
    # fatal for anything that subscripts inside a per-edge loop. Materialise
    # once so no later section can reintroduce that.
    with np.load(args.table, allow_pickle=False) as npz:
        t = {name: npz[name] for name in npz.files}

    kerb = t["kerb_dist"]
    centre = t["centerline_dist"]
    length = t["length_m"]
    sidewalk = (t["kind"] == "footway/sidewalk") & (length >= 5.0)
    have = sidewalk & np.isfinite(kerb)

    print("=" * 74)
    print("REGISTRATION -- how far is an OSM sidewalk from the city's kerb?")
    print("=" * 74)
    print(f"  sidewalk edges {int(sidewalk.sum()):,}, "
          f"{length[sidewalk].sum() / 1000:,.0f} km")
    ps = [weighted_percentile(kerb[have], length[have], q)
          for q in (10, 25, 50, 75, 90, 99)]
    print("  length-weighted gap:  p10 %.2f  p25 %.2f  median %.2f  "
          "p75 %.2f  p90 %.2f  p99 %.2f" % tuple(ps))
    print(f"\n  {'borough':16s} {'sidewalk km':>12s} {'median':>9s} {'p90':>8s} {'p99':>8s}")
    for code, name in BOROUGHS.items():
        b = have & (t["boro"] == code)
        if b.sum() < 100:
            continue
        print(f"  {name:16s} {length[b].sum() / 1000:11,.0f} "
              f"{weighted_percentile(kerb[b], length[b], 50):8.2f}m "
              f"{weighted_percentile(kerb[b], length[b], 90):7.2f}m "
              f"{weighted_percentile(kerb[b], length[b], 99):7.2f}m")

    print("\n" + "=" * 74)
    print("SCALE-INVARIANCE -- does the gap grow with the street's width?")
    print("=" * 74)
    print(f"  {'width (ft)':>12s} {'n':>9s} {'KERB med/p90':>18s} "
          f"{'CENTERLINE med/p90':>22s}")
    points = []
    for lo, hi in ((0, 25), (25, 32), (32, 40), (40, 50), (50, 65), (65, 200)):
        b = have & (t["width_ft"] >= lo) & (t["width_ft"] < hi)
        if b.sum() < 300:
            continue
        k50 = weighted_percentile(kerb[b], length[b], 50)
        k90 = weighted_percentile(kerb[b], length[b], 90)
        cb = b & np.isfinite(centre)
        c50 = weighted_percentile(centre[cb], length[cb], 50)
        c90 = weighted_percentile(centre[cb], length[cb], 90)
        points.append((0.5 * (lo + hi), k50, c50))
        print(f"  {str(lo) + '-' + str(hi):>12s} {int(b.sum()):9,} "
              f"{k50:9.2f} {k90:8.2f} {c50:13.2f} {c90:8.2f}")
    if len(points) >= 3:
        x = np.array([p[0] for p in points])
        kerb_slope = np.polyfit(x, np.array([p[1] for p in points]), 1)[0]
        centre_slope = np.polyfit(x, np.array([p[2] for p in points]), 1)[0]
        print(f"\n  slope of gap vs width:  KERB {kerb_slope:+.4f} m/ft"
              f"   CENTERLINE {centre_slope:+.4f} m/ft")
        print("  A scale-invariant anchor has slope ~0. The centerline cannot:")
        print("  it sits at half the roadway, so its slope IS the road widening.")

    print("\n" + "=" * 74)
    print("COVERAGE -- how much sidewalk gets a usable block face?")
    print("=" * 74)
    total = length[sidewalk].sum()
    for tol in (2.0, 3.0, 5.0, 8.0, 12.0):
        within = sidewalk & (kerb <= tol)
        usable = within & t["conflated"] & (t["street"] != "")
        print(f"  within {tol:4.1f}m : {100 * length[within].sum() / total:6.2f}% "
              f"|  with a resolvable block face: "
              f"{100 * length[usable].sum() / total:6.2f}%")
    print(f"\n  {'borough':16s} {'<=5m':>8s} {'conflated':>11s}")
    for code, name in BOROUGHS.items():
        b = have & (t["boro"] == code)
        if b.sum() < 100:
            continue
        bt = length[b].sum()
        print(f"  {name:16s} {100 * length[b & (kerb <= 5)].sum() / bt:7.2f}% "
              f"{100 * length[b & (kerb <= 5) & t['conflated']].sum() / bt:10.2f}%")

    print("\n" + "=" * 74)
    print("BLOCK FACE AS A DENOMINATOR")
    print("=" * 74)
    # `conflated` is NYC's OWN flag for whether the kerb->block-face link
    # succeeded. The coverage section above already requires it; this section
    # did not, so it was scoring face assignments the source itself disowns.
    # Same population in both, or the two disagree for no reason.
    good = (sidewalk & (kerb <= 5.0) & (t["blockface"] != "")
            & t["conflated"])
    per_face = {}
    for face, metres in zip(t["blockface"][good], length[good]):
        per_face[face] = per_face.get(face, 0.0) + float(metres)
    v = np.array(sorted(per_face.values()))
    print(f"  block faces receiving sidewalk : {len(v):,}")
    print(f"  sidewalk per face: p10 {np.percentile(v, 10):5.1f}m  "
          f"median {np.percentile(v, 50):6.1f}m  p90 {np.percentile(v, 90):6.1f}m  "
          f"max {v.max():.0f}m")
    print(f"  faces under 20m: {100 * (v < 20).mean():.1f}% of faces, "
          f"holding {100 * v[v < 20].sum() / v.sum():.2f}% of length")

    # THE TAIL IS THE RISK, NOT THE MEDIAN. Every edge on a face inherits
    # that face's density, so an oversized face smears one shade value over
    # every metre of it. A median of ~85m says nothing about how much
    # pavement lives on the faces that are not blocks at all -- that is the
    # number that decides whether this denominator is safe, so measure it
    # rather than quoting the max and moving on.
    print(f"\n  {'face size':>14s} {'faces':>9s} {'% of faces':>11s} "
          f"{'% of sidewalk km':>17s}")
    edges_of = {}
    for i in np.flatnonzero(good):
        edges_of.setdefault(t["blockface"][i], []).append(i)
    for lo, hi in ((0, 250), (250, 500), (500, 1000), (1000, 10 ** 9)):
        band = v[(v >= lo) & (v < hi)]
        label = f"{lo}-{hi}m" if hi < 10 ** 9 else f"over {lo}m"
        print(f"  {label:>14s} {len(band):9,} {100 * len(band) / len(v):10.2f}% "
              f"{100 * band.sum() / v.sum():16.2f}%")

    print("\n  the ten largest faces -- if these are parkways and bridge "
          "approaches\n  rather than ordinary blocks, the tail is a "
          "known shape, not a surprise:")
    biggest = sorted(per_face.items(), key=lambda kv: -kv[1])[:10]
    street = t["street"]
    for face, metres in biggest:
        i = edges_of[face][0]
        print(f"    {metres:8.0f}m  {str(street[i])[:38]:38s} "
              f"{len(edges_of[face]):5,} edges")


if __name__ == "__main__":
    main()
