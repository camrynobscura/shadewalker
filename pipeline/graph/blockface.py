"""Which block face is this sidewalk, or this tree, on?

WHY THIS EXISTS
---------------
Shade is trees divided by a length, and OSM's own edges are the wrong
length: they are chopped at arbitrary points, so half of them are under 5m
(holding 2.3% of the walking distance) while two thirds of the distance
sits in pieces longer than a block. The same tree reads one density on a
2.6m stub and another on the 51.9m run beside it.

The fix is to group pavement by block face -- one side of one block -- and
score per face. NYC supplies the key: every kerb line carries a `blockf_id`
conflated to a CSCL block face, and CSCL turns that into a side, a street
name and a segment id.

This module answers one question, for a point or for a line: which face?

IS THIS AN OSM OVERRIDE?
------------------------
No. It produces a label and a grouping. It adds no way, removes none, and
connects nothing -- the pedestrian graph is untouched, and with the shade
weight at zero the router's costs are unchanged (see server/graph_store.py's
edge_costs: `length / (1 + tree_weight * density)` is exactly `length` when
the weight is 0). Same category as pipeline/graph/naming.py, which draws
this line in its own docstring. The follow-OSM rule governs routing;
shade-side labels are fine.

THE RULES HERE ARE MEASURED, NOT CHOSEN
---------------------------------------
  - Nearest kerb, not nearest street centerline. A centerline sits
    mid-roadway so its distance to a sidewalk is half the road width, a
    property of the road rather than of the relationship. Kerb gap grows
    +0.0036 m/ft of street width; centerline gap +0.0618 (over 143,591
    sidewalks; for trees, blind human review of 12 conflicts agreed 12/12).
  - Median probe distance for a line, never the minimum. A cross street is
    close at one end of a sidewalk and far along the rest, so a
    minimum-distance rule calls almost every sidewalk ambiguous.
  - `conflated` required. It is NYC's own flag for whether the kerb ->
    block-face link succeeded (91.9% do). Without it we score links the
    source itself disowns.
  - CSCL attributes only: side, street, segment id. Never its geometry as
    a ruler, because a centerline segment can be far shorter than the run
    of kerb conflated to it (pipeline/fetch/planimetrics.py).
"""

import logging
import math

import numpy as np
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from pipeline import config
from pipeline.graph.naming import _probe_points, _to_m

logger = logging.getLogger(__name__)


def _angular_distance(a: float, b: float) -> float:
    """Smallest separation between two bearings, degrees, 0-180."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class Face:
    """One side of one block, with the CSCL attributes for that side."""

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

    `runner_up_m` is the distance to the nearest kerb of a different CSCL
    segment -- i.e. a different street, not merely the other side of the
    same one. It is exposed rather than turned into an accept/reject rule
    here: only 0.52% of sidewalk length is genuinely contested at 2m slack,
    so no caller needs a rule, and inventing one would be a threshold
    nobody measured.
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
    measurement the audit tools take.

    Road edges only. Pavement Edge also carries alleys (feat_code 2270) and
    airport runways (2230), and neither has a sidewalk beside it; see
    config.ROAD_EDGE_FEAT_CODE.
    """
    lines, face_ids, conflated = [], [], []
    skipped = 0
    for row in pave_rows:
        if row.get("feat_code") != config.ROAD_EDGE_FEAT_CODE:
            skipped += 1
            continue
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
    if skipped:
        logger.info(f"  [blockface] {skipped:,} non-road-edge kerb lines "
                    f"skipped (alleys, runways)")
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
        # face id -> positions of its (conflated) kerb lines, for
        # compass_side. Built once: 171k rows, trivial next to the STRtree.
        self._kerb_positions_of_face: dict[str, list[int]] = {}
        for position, face_id in enumerate(self._face_of_kerb):
            if self._conflated[position] and face_id:
                self._kerb_positions_of_face.setdefault(
                    str(face_id), []).append(position)
        logger.info(f"  [blockface] {len(self._kerbs):,} kerb lines, "
                    f"{len(self._faces):,} resolvable block faces")

    def __len__(self):
        return len(self._faces)

    def face(self, face_id):
        return self._faces.get(face_id)

    def compass_side(self, coords, face_id) -> str:
        """Which compass side of its street this pavement is on -- "N",
        "S", "E" or "W" -- or "" when no single plain word is honest.

        Geometry, not CSCL's L/R: the L/R is relative to each segment's
        arbitrary digitization direction and does not survive a block
        boundary -- same-name sidewalk pairs continuing across a side
        street disagree 53.3% of the time (a coin flip; measured
        2026-08-28), so a direction keyed on it would announce phantom
        "cross to the other side" steps. The kerb, by contrast, physically
        separates this pavement from its roadway: the direction from the
        pavement to its kerb points at the street, so its opposite names
        the side the pavement is on.

        Four plain words only: generous 90-degree bins match how the city
        talks -- Manhattan's grid is ~29 degrees off true and everyone
        still says "the north side of 23rd Street". Where the mean side
        direction sits within config.SIDE_DECLINE_MARGIN_DEG of a bin
        boundary (a true diagonal), or wanders along the edge (a curve --
        resultant below config.SIDE_MIN_RESULTANT), the answer is "" and
        directions simply omit the side, the same philosophy as naming:
        no word beats a confusing word.
        """
        positions = self._kerb_positions_of_face.get(str(face_id))
        if not positions:
            return ""
        line = LineString([_to_m(lon, lat) for lon, lat in coords])
        if line.length <= 0:
            return ""
        vectors = []  # per-probe unit vector pointing AWAY from the roadway
        for probe in _probe_points(line):
            nearest = None
            for position in positions:
                kerb = self._kerbs[position]
                point = kerb.interpolate(kerb.project(probe))
                d = probe.distance(point)
                if nearest is None or d < nearest[0]:
                    nearest = (d, point)
            dx, dy = nearest[1].x - probe.x, nearest[1].y - probe.y
            norm = math.hypot(dx, dy)
            if norm < 0.01:
                continue  # probe sits ON the kerb; no direction to read
            vectors.append((-dx / norm, -dy / norm))
        if not vectors:
            return ""

        def mean_of(vs):
            x = sum(v[0] for v in vs)
            y = sum(v[1] for v in vs)
            return x, y, math.hypot(x, y) / len(vs)

        x_sum, y_sum, resultant = mean_of(vectors)
        if resultant < config.SIDE_MIN_RESULTANT:
            # A kerb line wraps its block's corner, so a probe near the
            # edge's end can attach to the wrapped return and read the
            # roadway direction backwards -- one flipped probe in three
            # collapses the resultant to ~0.33 (17% of a 20k sample sat
            # at exactly that value). Drop the minority pointing >90
            # degrees from the first-pass mean and retry once. A genuine
            # curve (an L wrapping a corner) spreads smoothly instead,
            # keeps its majority, and still fails the resultant test
            # below.
            mean_bearing = math.degrees(math.atan2(x_sum, y_sum)) % 360.0
            kept = [v for v in vectors
                    if _angular_distance(
                        math.degrees(math.atan2(v[0], v[1])) % 360.0,
                        mean_bearing) <= 90.0]
            if len(kept) * 2 <= len(vectors):
                return ""
            x_sum, y_sum, resultant = mean_of(kept)
            if resultant < config.SIDE_MIN_RESULTANT:
                return ""
        bearing = math.degrees(math.atan2(x_sum, y_sum)) % 360.0
        centers = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}
        word, center = min(centers.items(),
                           key=lambda item: _angular_distance(bearing, item[1]))
        if _angular_distance(bearing, center) > 45.0 - config.SIDE_DECLINE_MARGIN_DEG:
            return ""
        return word

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
        """The face a single point sits on.

        The default cap is the line cap (BLOCK_FACE_MAX_M). Tree scoring
        passes config.TREE_ATTACH_MAX_M explicitly: a crown reaches over
        pavement in a way a kerb line cannot, and the two caps are
        deliberately separate constants; see their comments in config.py.
        """
        max_m = config.BLOCK_FACE_MAX_M if max_m is None else max_m
        point = Point(*_to_m(lon, lat))
        return self._resolve(point.buffer(max_m), point.distance, max_m)

    def match_line(self, coords, max_m: float = None) -> Match | None:
        """The face a line runs along -- one answer for the whole line.

        Median distance over points sampled along the whole line, not the
        minimum. See the module docstring: the minimum makes a cross street
        look like a parent.

        Not for scoring -- use match_line_profile. One answer per edge is
        fine for a question about the edge as a whole ("which street is this
        sidewalk part of"), and wrong for apportioning shade, because an
        edge longer than a block has no single right answer and this returns
        None for it rather than the several right ones.
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

    def match_line_profile(self, coords, step_m: float = None,
                           max_m: float = None) -> dict:
        """How much of a line lies beside each block face: {face_id: metres}.

        WHY THIS EXISTS, AND WHY match_line CANNOT BE USED FOR SCORING
        --------------------------------------------------------------
        match_line gives one answer for a whole edge, from the median
        distance over its probes. That is right for naming a sidewalk after
        a street, and wrong for scoring it, because OSM often draws a
        sidewalk as a single unbroken way running past many blocks. Beside
        any one block such a line is ~2m from the kerb, but its median over
        the whole length is far larger, so it exceeds BLOCK_FACE_MAX_M and
        matches nothing: the face keeps its trees and loses its pavement
        (one 14th Street face read 280x the citywide median density that
        way). 28.3% of the city's sidewalk length sits in pieces over 200m,
        so this is the common case.

        Sampling asks the question once per step instead, so each part of a
        long edge is credited to the block it is actually beside.

        Nothing is cut. This returns a length profile, not a split: no edge
        is divided, no node is added, and the routing graph is untouched.
        It does not need to be -- pipeline/graph/pedestrian.py already cuts
        every way at every junction (find_junctions / _split_way), so an
        edge long enough to span blocks is by construction one that nothing
        connects to along its length. There is no corner to turn at inside
        it, so there is no routing decision a split could enable.

        Each sample sits at the centre of its own slice, and the slices tile
        the whole line, so this measures along the entire edge rather than
        deriving an extent from one midpoint per feature.

        Returns {} when no part of the line is within max_m of a conflated
        road-edge kerb. The profile's values sum to at most the line's
        length: steps beside no block face are credited to nothing, which is
        deliberate. Unattributable pavement is not silently given some
        neighbouring block's shade.
        """
        step_m = (config.BLOCK_FACE_SAMPLE_STEP_M if step_m is None else step_m)
        max_m = config.BLOCK_FACE_MAX_M if max_m is None else max_m
        line = LineString([_to_m(lon, lat) for lon, lat in coords])
        if line.length < 1e-9:
            return {}

        # One STRtree query for the whole line, then plain distance maths per
        # sample against the handful of kerbs it returned. Querying per sample
        # would repeat the same spatial lookup up to hundreds of times for one
        # long edge, and the citywide pass makes ~7.1M samples.
        candidates = []
        for position in self._index.query(line.buffer(max_m)):
            if not self._conflated[position]:
                continue
            face = self._faces.get(self._face_of_kerb[position])
            if face is None:
                continue
            candidates.append((self._kerbs[position], face))
        if not candidates:
            return {}

        slices = max(1, math.ceil(line.length / step_m))
        slice_m = line.length / slices

        profile: dict[str, float] = {}
        for index in range(slices):
            probe = line.interpolate((index + 0.5) * slice_m)
            best_face, best_distance = None, None
            for kerb, face in candidates:
                distance = probe.distance(kerb)
                if distance > max_m:
                    continue
                if best_distance is None or distance < best_distance:
                    best_face, best_distance = face, distance
            if best_face is not None:
                profile[best_face.face_id] = (
                    profile.get(best_face.face_id, 0.0) + slice_m)
        return profile
