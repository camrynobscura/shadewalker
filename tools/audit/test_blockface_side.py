"""Is CSCL's left/right block-face side actually CORRECT?

THE QUESTION
------------
`blockf_id` names one SIDE of one block, which is exactly the unit per-side
shade needs. But "a block face never collects both an L and an R sidewalk"
is true BY CONSTRUCTION -- the id already picks a side. That proves nothing
about whether the side is the right one, and a systematically inverted
convention would credit the shady pavement's trees to the sunny one while
every internal check still passed.

METHOD -- three angles, only the third independent of our own geometry
----------------------------------------------------------------------
  A  CSCL's claim vs OUR geometry. Compute which side of the CSCL segment
     the sidewalk sits on, from the segment's own digitisation direction,
     and compare to whether the id was the l_ or the r_ blockface.
     An inverted convention shows ~0% agreement.

  B  Opposite-sides invariant. The two faces of one segment must receive
     sidewalks on geometrically opposite sides.

  C  EXTERNAL park anchor. Where a park lies decisively on ONE side of a
     block, the face nearer it must carry the compass direction pointing
     AT it. Park polygons are independent of both CSCL's attribute and our
     cross product, so this is the only one that can catch a shared error.

     THE TRAP, hit three times while developing this: a sidewalk on the FAR
     side of the street correctly points AWAY from the park, so testing all
     park-adjacent sidewalks returns ~50% and looks like total failure.
     The park must be decisively nearer one face, only that face may be
     tested, and the park must lie roughly PERPENDICULAR to the street --
     on an irregular polygon the nearest point is often along-track, which
     is pure noise. Also: never average azimuths arithmetically (350 and 10
     average to 180, exactly backwards).

Read-only; consumes measure_sidewalk_kerb_match.py's output.
"""
import argparse
import collections
import os
import sys

import numpy as np
from pyproj import Geod
from shapely.geometry import Point
from shapely.ops import nearest_points
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline.fetch.parks import fetch_park_properties  # noqa: E402
from pipeline.graph.boundary import park_polygon  # noqa: E402
from pipeline.graph.naming import (  # noqa: E402
    _K as LON_SCALE, _LAT_M as LAT_SCALE,
)
from tools.audit import fetch_planimetrics  # noqa: E402

GEOD = Geod(ellps="WGS84")


def bearing(lon1, lat1, lon2, lat2):
    azimuth, _, _ = GEOD.inv(lon1, lat1, lon2, lat2)
    return azimuth % 360.0


def angle_between(a, b):
    return abs((a - b + 180) % 360 - 180)


def compass(degrees):
    return "NESW"[int(((degrees + 45) % 360) // 90)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table",
                    default="data/audits/2026-08-23/sidewalk_kerb_match.npz")
    args = ap.parse_args()

    # np.load on an .npz returns a LAZY NpzFile: every subscript re-reads
    # and re-inflates that whole column out of the zip. The per-edge loop
    # below subscripts five columns, so leaving it lazy decompressed a
    # 139k-element array ~700,000 times and turned a two-minute job into a
    # half-hour one. Materialise every column ONCE, up front.
    with np.load(args.table, allow_pickle=False) as npz:
        t = {name: npz[name] for name in
             ("kind", "length_m", "kerb_dist", "blockface", "mid_lon", "mid_lat")}

    segments = fetch_planimetrics.block_face_segments()
    print(f"block faces with CSCL geometry: {len(segments):,}")

    selected = np.flatnonzero(
        (t["kind"] == "footway/sidewalk") & (t["length_m"] >= 5.0)
        & (t["kerb_dist"] <= 5.0) & (t["blockface"] != "")
        & np.isfinite(t["mid_lon"]))
    print(f"sidewalk edges under test: {len(selected):,}\n")

    agree = disagree = 0
    per_segment = collections.defaultdict(dict)
    candidates = []
    for i in selected:
        face = t["blockface"][i]
        if face not in segments:
            continue
        face_geom = segments[face]
        metres, claimed, segment_id = (
            face_geom.line, face_geom.side, face_geom.physicalid)
        point = Point(t["mid_lon"][i] * LON_SCALE, t["mid_lat"][i] * LAT_SCALE)
        along = metres.project(point)
        back = metres.interpolate(max(0.0, along - 8.0))
        forward = metres.interpolate(min(metres.length, along + 8.0))
        cross = ((forward.x - back.x) * (point.y - back.y)
                 - (forward.y - back.y) * (point.x - back.x))
        if abs(cross) < 1e-9:
            continue
        geometric = "L" if cross > 0 else "R"
        if geometric == claimed:
            agree += 1
        else:
            disagree += 1
        per_segment[segment_id][claimed] = geometric

        segment_azimuth = bearing(back.x / LON_SCALE, back.y / LAT_SCALE,
                                  forward.x / LON_SCALE, forward.y / LAT_SCALE)
        derived = ((segment_azimuth - 90.0) % 360.0 if claimed == "L"
                   else (segment_azimuth + 90.0) % 360.0)
        candidates.append((segment_id, claimed, float(t["mid_lon"][i]),
                           float(t["mid_lat"][i]), derived, segment_azimuth))

    total = agree + disagree
    print("=" * 70)
    print("A. CSCL's L/R claim vs our INDEPENDENT geometry")
    print("=" * 70)
    print(f"   tested   : {total:,}")
    print(f"   AGREE    : {agree:,} ({100 * agree / total:.2f}%)")
    print(f"   disagree : {disagree:,} ({100 * disagree / total:.2f}%)")

    both = [v for v in per_segment.values() if len(v) == 2]
    opposite = sum(1 for v in both if v["L"] != v["R"])
    print("\n" + "=" * 70)
    print("B. OPPOSITE-SIDES invariant across one CSCL segment")
    print("=" * 70)
    print(f"   segments with sidewalk on both faces : {len(both):,}")
    print(f"   the two faces land opposite          : {opposite:,} "
          f"({100 * opposite / len(both) if both else 0:.2f}%)")

    parks = park_polygon(fetch_park_properties())
    pieces = list(parks.geoms) if parks.geom_type.startswith("Multi") else [parks]
    park_index = STRtree(pieces)

    by_segment = collections.defaultdict(lambda: collections.defaultdict(list))
    for segment_id, claimed, lon, lat, derived, seg_az in candidates:
        here = Point(lon, lat)
        nearest_park = None
        for k in park_index.query(here.buffer(0.0012)):
            on_park, _ = nearest_points(pieces[k], here)
            _, _, metres_away = GEOD.inv(lon, lat, on_park.x, on_park.y)
            if metres_away < 1.0:
                continue
            if nearest_park is None or metres_away < nearest_park[0]:
                nearest_park = (metres_away, on_park.x, on_park.y)
        if nearest_park is None:
            continue
        by_segment[segment_id][claimed].append(
            (nearest_park[0], derived, seg_az, lon, lat,
             nearest_park[1], nearest_park[2]))

    tested = correct = 0
    errors = []
    by_direction = collections.Counter()
    for faces in by_segment.values():
        if len(faces) != 2:
            continue
        left = float(np.mean([x[0] for x in faces["L"]]))
        right = float(np.mean([x[0] for x in faces["R"]]))
        # the park must be DECISIVELY on one side, or "nearer" is noise
        if max(left, right) < 2 * min(left, right) or abs(left - right) < 10 \
                or min(left, right) > 40:
            continue
        for _, derived, seg_az, lon, lat, plon, plat in (
                faces["L"] if left < right else faces["R"]):
            true_azimuth = bearing(lon, lat, plon, plat)
            off_perpendicular = min(
                angle_between(true_azimuth, (seg_az + 90) % 360),
                angle_between(true_azimuth, (seg_az - 90) % 360))
            if off_perpendicular > 30.0:
                continue
            tested += 1
            error = angle_between(derived, true_azimuth)
            errors.append(error)
            if error <= 60.0:
                correct += 1
                by_direction[compass(true_azimuth) + " ok"] += 1
            else:
                by_direction[compass(true_azimuth) + " BAD"] += 1

    print("\n" + "=" * 70)
    print("C. EXTERNAL park anchor -- park decisively on ONE side")
    print("=" * 70)
    if tested:
        e = np.array(errors)
        print(f"   sidewalk edges tested      : {tested:,}")
        print(f"   compass points AT the park : {correct:,} "
              f"({100 * correct / tested:.2f}%)")
        print(f"   angular error: median {np.median(e):5.1f}deg  "
              f"p75 {np.percentile(e, 75):5.1f}")
        print(f"   outright OPPOSITE (>120deg): {int((e > 120).sum()):,} "
              f"({100 * (e > 120).mean():.2f}%)")
        print("   errors spread evenly across directions means noise, not an")
        print("   inverted convention -- a flip concentrates in one direction:")
        for key, count in sorted(by_direction.items()):
            print(f"       {key:8s} {count:6,}")
    else:
        print("   no qualifying edges")


if __name__ == "__main__":
    main()
