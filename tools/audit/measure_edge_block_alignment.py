"""Do OSM sidewalk edges line up with NYC's block faces, or straddle them?

THE QUESTION
------------
Shade is trees divided by length, and we want the length to be a block
face -- one side of one block. That only works if an OSM sidewalk edge
belongs to ONE face. Two facts say it might not:

  CSCL block face   median  84m   p75 154   p90 227
  OSM sidewalk edge median  67m   p75 134   p90 208

Nearly the same size. So the risk is not that one edge swallows several
blocks -- it is that edges and blocks are the same size but out of PHASE.
A 67m edge straddling a boundary sits 40m on one face and 27m on the next,
and measure_sidewalk_kerb_match.py files the whole thing under whichever
face is nearest by median probe. The 27m is credited to the wrong block and
the right block is starved.

WHAT THIS DECIDES
-----------------
PURITY -- the share of an edge's length that falls in its own dominant
face -- picks between the two fixes:

  purity near 1.0   edges already line up. Give each edge the length-
                    weighted average of the faces it touches and change
                    nothing structural. (Option B.)
  purity well below edges straddle. Cut them at face boundaries so each
                    piece belongs to one face. (Option A.) Cutting adds
                    only degree-2 nodes, so it creates no new route
                    choices -- it makes the COST of a path accurate, and
                    it makes the reported shade_fraction accurate, which
                    averaging cannot because shade credit saturates and
                    the credit of a mean is not the mean of credits.

METHOD
------
Sample each sidewalk edge every ~SAMPLE_EVERY_M and assign each sample to
the nearest kerb's block face, so an edge gets a face PROFILE rather than
one label. Purity is the dominant face's share of the samples, weighted by
the length each sample stands for.

Deliberately NOT the median-probe rule the matcher uses: that rule exists to
give an edge ONE answer, which is the very thing being questioned here.

Read-only. ~7 min: the OSM read dominates.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402
from pipeline.fetch.boundaries import fetch_borough_boundaries  # noqa: E402
from pipeline.graph.boundary import nyc_boundary  # noqa: E402
from pipeline.graph.naming import _to_m  # noqa: E402
from pipeline.graph.pedestrian import (  # noqa: E402
    find_junctions, _split_way, _chain_lengths_m,
)
from tools.audit import fetch_planimetrics  # noqa: E402
from pipeline.graph.blockface import build_kerb_index  # noqa: E402
from tools.audit.measure_sidewalk_kerb_match import (  # noqa: E402
    read_pedestrian_with_tags,
)

# One sample per this many metres along an edge, minimum 3. Fine enough to
# locate a boundary to within half a step, coarse enough that the whole city
# is ~1.2M lookups rather than ten times that.
SAMPLE_EVERY_M = 15.0

# A sample further than this from any kerb is not beside a street at all
# (park interior, plaza, bridge deck) and gets no face rather than being
# forced onto the nearest one. Same tolerance the matcher settled on.
MAX_KERB_M = 5.0


def face_profile(line, kerb_index, kerbs, kerb_face, kerb_conflated):
    """[(face_id, metres_of_this_edge_it_covers), ...], dominant first."""
    count = max(3, int(line.length // SAMPLE_EVERY_M) + 1)
    share = line.length / count
    weights = {}
    for i in range(count):
        # sample at the MIDDLE of each slice, so a sample stands for the
        # length around it rather than for a point at an endpoint
        point = line.interpolate((i + 0.5) * share)
        best, best_d = "", MAX_KERB_M
        for position in kerb_index.query(point.buffer(MAX_KERB_M)):
            if not kerb_conflated[position]:
                continue
            d = point.distance(kerbs[position])
            if d < best_d:
                best, best_d = kerb_face[position], d
        if best:
            weights[best] = weights.get(best, 0.0) + share
    return sorted(weights.items(), key=lambda kv: -kv[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default="data/audits/2026-08-23/edge_block_alignment")
    ap.add_argument("--min-len", type=float, default=5.0)
    args = ap.parse_args()
    started = time.perf_counter()

    def elapsed():
        return time.perf_counter() - started

    nyc = nyc_boundary(fetch_borough_boundaries())
    kerb_index, kerbs, kerb_face, kerb_conflated = build_kerb_index(
        fetch_planimetrics.load("pavement_edge"))
    print(f"[{elapsed():5.1f}s] {len(kerbs):,} kerb lines", flush=True)

    ped, _ = read_pedestrian_with_tags(config.OSM_EXTRACT_PATH, nyc)
    ways = [w for w, _ in ped]
    tags_by_osm = {w.osm_id: t for w, t in ped}
    junctions = find_junctions(ways)
    edges = []
    for way in ways:
        tags = tags_by_osm.get(way.osm_id, {})
        if tags.get("footway") != "sidewalk":
            continue
        for _, lons, lats in _split_way(way, junctions):
            if len(lons) >= 2:
                edges.append((lons, lats))
    lengths = _chain_lengths_m([(None, lons, lats) for lons, lats in edges])
    keep = [i for i in range(len(edges)) if lengths[i] >= args.min_len]
    print(f"[{elapsed():5.1f}s] {len(keep):,} sidewalk edges "
          f">= {args.min_len:.0f}m", flush=True)

    n_faces, purity, dom_m, edge_m, spill_m = [], [], [], [], []
    for done, i in enumerate(keep):
        lons, lats = edges[i]
        line = LineString([_to_m(a, b) for a, b in zip(lons, lats)])
        if line.length < 1e-9:
            continue
        profile = face_profile(line, kerb_index, kerbs, kerb_face,
                               kerb_conflated)
        covered = sum(m for _, m in profile)
        if covered <= 0:
            continue
        n_faces.append(len(profile))
        purity.append(profile[0][1] / covered)
        dom_m.append(profile[0][1])
        edge_m.append(float(lengths[i]))
        # metres of THIS edge that belong to a face other than its dominant
        # one -- the length credited to the wrong block by a one-label rule
        spill_m.append(covered - profile[0][1])
        if (done + 1) % 20000 == 0:
            print(f"[{elapsed():5.1f}s] {done + 1:,}/{len(keep):,}", flush=True)

    n_faces = np.array(n_faces)
    purity = np.array(purity)
    edge_m = np.array(edge_m)
    spill_m = np.array(spill_m)

    print("\n" + "=" * 74)
    print("ALIGNMENT -- how many block faces does one sidewalk edge touch?")
    print("=" * 74)
    total_km = edge_m.sum() / 1000
    print(f"  edges measured: {len(n_faces):,}, {total_km:,.0f} km")
    print(f"  {'faces touched':>14s} {'edges':>9s} {'% of edges':>11s} "
          f"{'% of km':>9s}")
    for k in (1, 2, 3, 4):
        b = (n_faces == k) if k < 4 else (n_faces >= 4)
        label = str(k) if k < 4 else "4+"
        print(f"  {label:>14s} {int(b.sum()):9,} "
              f"{100 * b.mean():10.1f}% {100 * edge_m[b].sum() / edge_m.sum():8.1f}%")

    print("\n" + "=" * 74)
    print("PURITY -- share of an edge that lies in its OWN dominant face")
    print("=" * 74)
    order = np.argsort(purity)
    cum = np.cumsum(edge_m[order]) / edge_m.sum()
    print("  length-weighted:  p10 %.2f  p25 %.2f  median %.2f  p75 %.2f" % tuple(
        purity[order][np.searchsorted(cum, q)] for q in (.10, .25, .50, .75)))
    for cut in (0.95, 0.90, 0.75, 0.60):
        b = purity < cut
        print(f"  purity below {cut:.2f} : {int(b.sum()):6,} edges, "
              f"{100 * edge_m[b].sum() / edge_m.sum():5.1f}% of km")

    print(f"\n  MISCREDITED LENGTH -- metres filed under the wrong block by a")
    print(f"  one-label-per-edge rule: {spill_m.sum() / 1000:,.0f} km of "
          f"{total_km:,.0f} km ({100 * spill_m.sum() / edge_m.sum():.1f}%)")
    print("\n  Read this as: near 0% -> averaging is enough (option B).")
    print("  Large -> cut edges at face boundaries (option A).")

    summary = {
        "edges": int(len(n_faces)),
        "sidewalk_km": round(float(total_km), 1),
        "pct_km_on_multi_face_edges": round(
            float(100 * edge_m[n_faces > 1].sum() / edge_m.sum()), 2),
        "median_purity_length_weighted": round(
            float(purity[order][np.searchsorted(cum, 0.5)]), 4),
        "miscredited_km": round(float(spill_m.sum() / 1000), 1),
        "miscredited_pct": round(float(100 * spill_m.sum() / edge_m.sum()), 2),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out + ".json", "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\n[{elapsed():5.1f}s] wrote {args.out}.json")


if __name__ == "__main__":
    main()
