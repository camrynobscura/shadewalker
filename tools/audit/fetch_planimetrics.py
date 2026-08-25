"""Audit-side helpers over NYC Planimetrics, plus a diagnostics CLI.

THE FETCHING MOVED. It lives in `pipeline/fetch/planimetrics.py` now --
the block-face model ships, so pulling these layers is pipeline work, not
audit work. `load` is re-exported here unchanged so the audit tools that
already call `fetch_planimetrics.load("cscl")` keep working.

What stays here is what only an audit needs:
  block_face_segments()  CSCL rows -> per-block-face geometry in metres
  main()                 the conflation diagnostics whose numbers are
                         quoted in the working notes (91.9% conflated,
                         175,772 distinct faces), kept so they stay
                         reproducible rather than becoming folklore

Local, read-only.
"""
import argparse
import logging
import os
import sys
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline.fetch.planimetrics import (  # noqa: E402,F401
    DATASETS, cache_path, fetch_all, load,
)


class BlockFace(NamedTuple):
    """One side of one CSCL segment.

    `line` is the LONGEST part only -- a single LineString, which is what a
    left/right cross product needs. `total_m` is the length of ALL parts.
    They are separate fields on purpose: using `line.length` as the face's
    length understates any face CSCL digitised as several pieces.

    NOTE both are geometry, and CSCL geometry must NOT be used to measure
    how long a block is -- see pipeline/fetch/planimetrics.py. `line` is
    for DIRECTION (which side of the street a point falls on), which is a
    legitimate use; `total_m` exists only so a caller that does compare
    lengths cannot silently use the longest-part length instead.
    """
    line: object
    side: str
    physicalid: str
    total_m: float


def block_face_segments() -> dict:
    """block-face id -> BlockFace, geometry in metres.

    Lives here rather than in either consuming tool because both the side
    test and the alignment evaluation need the same parse, and two copies
    of geometry handling drift. Metres via naming._to_m -- the SAME flat
    approximation the kerb match used, so distances stay comparable across
    tools.
    """
    from shapely.geometry import LineString, shape

    from pipeline.graph.naming import _to_m

    out = {}
    for row in load("cscl"):
        raw = row.get("the_geom")
        if not raw:
            continue
        try:
            geom = shape(raw)
        except Exception:
            continue
        parts = [p for p in (geom.geoms if geom.geom_type.startswith("Multi")
                             else [geom]) if len(p.coords) >= 2]
        if not parts:
            continue
        # Convert to metres BEFORE picking the longest part: comparing part
        # lengths in degrees is anisotropic (1 deg lon = 84.5km, 1 deg lat =
        # 111km at NYC), so a north-south face can win against an
        # east-west one purely from the units.
        in_metres = [LineString([_to_m(x, y) for x, y in p.coords])
                     for p in parts]
        longest = max(in_metres, key=lambda p: p.length)
        total = sum(p.length for p in in_metres)
        for field, side in (("l_blockfaceid", "L"), ("r_blockfaceid", "R")):
            if row.get(field):
                out[str(row[field])] = BlockFace(
                    longest, side, str(row.get("physicalid") or ""), total)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even if a cache exists")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    for label in DATASETS:
        rows = load(label, refresh=args.refresh)
        with_geom = sum(1 for r in rows if r.get("the_geom"))
        print(f"{label}: {len(rows):,} rows, {with_geom:,} with geometry "
              f"-> {cache_path(label)}")

    pave = load("pavement_edge")
    conflated = sum(1 for r in pave if r.get("conflated") == "1")
    faces = {r.get("blockf_id") for r in pave if r.get("blockf_id")}
    print(f"\nkerbs conflated to a block face: {conflated:,}/{len(pave):,} "
          f"({100 * conflated / len(pave):.1f}%)")
    print(f"distinct block faces: {len(faces):,}")


if __name__ == "__main__":
    main()
