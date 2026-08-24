"""Which block face is this sidewalk, or this tree, on?

WHY THIS EXISTS
---------------
Shade is trees divided by a length, and OSM's own edges are the wrong
length: they are chopped at arbitrary points, so half of them are under 5m
(holding 2.3% of the walking distance) while two thirds of the distance
sits in pieces longer than a block. The same tree reads one density on a
2.6m stub and another on the 51.9m run beside it.

The fix is to group pavement by BLOCK FACE -- one side of one block -- and
score per face. NYC supplies the key: every kerb line carries a `blockf_id`
conflated to a CSCL block face, and CSCL turns that into a side, a street
name and a segment id.

This module answers one question, for a point or for a line: which face?

IS THIS AN OSM OVERRIDE?
------------------------
No. It produces a LABEL and a GROUPING. It adds no way, removes none, and
connects nothing -- the pedestrian graph is untouched, and with the shade
weight at zero the router's costs are unchanged (see server/graph_store.py's
edge_costs: `length / (1 + tree_weight * density)` is exactly `length` when
the weight is 0). Same category as pipeline/graph/naming.py, which draws
this line in its own docstring. The user confirmed on 2026-08-24 that the
follow-OSM rule governs ROUTING; shade-side labels are fine.

THE RULES HERE ARE MEASURED, NOT CHOSEN
---------------------------------------
  - NEAREST KERB, not nearest centerline. A centerline sits mid-roadway so
    its distance to a sidewalk is half the road width, which is a property
    of the road rather than of the relationship. Kerb gap grows +0.0036
    m/ft of street width; centerline gap +0.0618. Verified over 143,591
    sidewalks and, for trees, by blind human review of 12 conflicts (12/12).
  - MEDIAN PROBE DISTANCE for a line, never the minimum. A cross street is
    close at one END of a sidewalk and far along the rest, so a
    minimum-distance rule calls almost every sidewalk ambiguous -- the
    first version of the parent-street audit reported 16.1% instead of
    94.0% exactly this way.
  - `conflated` REQUIRED. It is NYC's own flag for whether the kerb ->
    block-face link succeeded (91.9% do). Without it we score links the
    source itself disowns.
  - CSCL ATTRIBUTES ONLY. Side, street, segment id. Never its geometry as a
    ruler: a centerline segment can be far shorter than the run of kerb
    conflated to it, and measuring block length that way produced a false
    "13% of the city is mis-assigned" on 2026-08-23. Full account in
    pipeline/fetch/planimetrics.py.
"""

import logging

import numpy as np
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from pipeline import config
from pipeline.graph.naming import _probe_points, _to_m

logger = logging.getLogger(__name__)


class Face:
    """One side of one block, with the CSCL attributes for THAT side."""

    __slots__ = ("face_id", "segment_id", "side", "street", "boro", "width_ft")

    def __init__(self, face_id, segment_id, side, street, boro, width_ft):
        self.face_id = face_id
        self.segment_id = segment_id
        self.side = side
        self.street = street
        self.boro = boro
        self.width_ft = width_ft

    def __repr__(self):
        return (f"Face({self.face_id} {self.street!r} side={self.side} "
                f"seg={self.segment_id})")


class Match:
    """A resolved face, with what it beat.

    `runner_up_m` is the distance to the nearest kerb of a DIFFERENT CSCL
    SEGMENT -- i.e. a different street, not merely the other side of the
    same one. It is exposed rather than turned into an accept/reject rule
    here: only 0.52% of sidewalk length is genuinely contested at 2m slack,
    so no caller has needed a rule yet, and inventing one now would be a
    threshold nobody measured.
    """

    __slots__ = ("face", "distance_m", "runner_up_m")

    def __init__(self, face, distance_m, runner_up_m):
        self.face = face
        self.distance_m = distance_m
        self.runner_up_m = runner_up_m


def build_face_lookup(cscl_rows) -> dict:
    """block-face id -> Face. One CSCL row yields up to two faces."""
    lookup = {}
    for row in cscl_rows:
        for field, side in (("l_blockfaceid", "L"), ("r_blockfaceid", "R")):
            value = row.get(field)
            if not value:
                continue
            lookup[str(value)] = Face(
                face_id=str(value),
                segment_id=str(row.get("physicalid") or ""),
                side=side,
                street=row.get("full_street_name") or "",
                boro=row.get("boroughcode") or "",
                width_ft=(float(row["streetwidth"])
                          if row.get("streetwidth") else float("nan")),
            )
    return lookup


def build_kerb_index(pave_rows):
    """(STRtree, geometries, face-id array, conflated array) in metres.

    Geometry is in the flat metre approximation naming.py uses, kept
    deliberately identical so distances here are comparable with every
    measurement taken during the evaluation.
    """
    lines, face_ids, conflated = [], [], []
    for row in pave_rows:
        raw = row.get("the_geom")
        if not raw:
            continue
        try:
            geom = shape(raw)
        except Exception:
            continue
        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for part in parts:
            points = [_to_m(x, y) for x, y in part.coords]
            if len(points) < 2:
                continue
            lines.append(LineString(points))
            face_ids.append(str(row.get("blockf_id") or ""))
            conflated.append(row.get("conflated") == "1")
    return STRtree(lines), lines, np.array(face_ids), np.array(conflated)


class BlockFaceIndex:
    """Nearest-block-face lookup over NYC's kerb lines.

    Built once per run (~40s for 178,947 kerbs) and queried per tree and
    per edge. Callers get a Match or None; the conflated filter, the
    distance cap and the median-probe rule are applied here rather than
    re-implemented by each caller.
    """

    def __init__(self, pave_rows, cscl_rows):
        self._index, self._kerbs, self._face_of_kerb, self._conflated = (
            build_kerb_index(pave_rows))
        self._faces = build_face_lookup(cscl_rows)
        logger.info(f"  [blockface] {len(self._kerbs):,} kerb lines, "
                    f"{len(self._faces):,} resolvable block faces")

    def __len__(self):
        return len(self._faces)

    def face(self, face_id):
        return self._faces.get(face_id)

    def _resolve(self, search_area, distance_of, max_m):
        """Shared core. `distance_of(kerb_geometry) -> metres`.

        `search_area` is passed in rather than stashed on self: a query
        parameter held as instance state is not re-entrant, and it reads as
        if the object remembers something it does not.
        """
        best = None
        nearest_by_segment = {}
        for position in self._index.query(search_area):
            if not self._conflated[position]:
                continue
            face = self._faces.get(self._face_of_kerb[position])
            if face is None:
                continue
            d = distance_of(self._kerbs[position])
            if d > max_m:
                continue
            segment = face.segment_id
            if segment not in nearest_by_segment or d < nearest_by_segment[segment]:
                nearest_by_segment[segment] = d
            if best is None or d < best[0]:
                best = (d, face)
        if best is None:
            return None
        others = [d for s, d in nearest_by_segment.items()
                  if s != best[1].segment_id]
        return Match(best[1], best[0], min(others) if others else None)

    def match_point(self, lon: float, lat: float,
                    max_m: float = None) -> Match | None:
        """The face a single point sits on -- a tree, typically."""
        max_m = config.BLOCK_FACE_MAX_M if max_m is None else max_m
        point = Point(*_to_m(lon, lat))
        return self._resolve(point.buffer(max_m), point.distance, max_m)

    def match_line(self, coords, max_m: float = None) -> Match | None:
        """The face a line runs along -- a sidewalk edge, typically.

        MEDIAN distance over points sampled along the whole line, not the
        minimum. See the module docstring: the minimum makes a cross street
        look like a parent.
        """
        max_m = config.BLOCK_FACE_MAX_M if max_m is None else max_m
        line = LineString([_to_m(lon, lat) for lon, lat in coords])
        if line.length < 1e-9:
            return None
        probes = _probe_points(line)

        def median_distance(kerb):
            distances = sorted(probe.distance(kerb) for probe in probes)
            return distances[len(distances) // 2]

        return self._resolve(line.buffer(max_m), median_distance, max_m)
