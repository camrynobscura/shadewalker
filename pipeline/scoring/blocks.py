"""Spread each block face's trees across the pavement on that block face.

THE PROBLEM THIS SOLVES
-----------------------
Shade is trees divided by a length, and OSM's own edges are the wrong
length. They are chopped at arbitrary points: half of all sidewalk edges
are under 5m, and the same tree therefore reads one density on a 2.6m stub
and another on the 51.9m run beside it. `edge_costs` in
server/graph_store.py costs each edge separately, so the router would chase
whichever stretch happened to be chopped finest.

THE FIX -- GROUP, DO NOT DIVIDE
-------------------------------
A block face is one side of one block. So:

    1. every tree      -> the block face of its nearest kerb
    2. every sidewalk  -> SAMPLED every config.BLOCK_FACE_SAMPLE_STEP_M,
                          each step credited to the face it is beside
    3. per face: density = summed tree value / summed sidewalk length
    4. per edge: tree value = its own metres on each face x that face's
                          density, summed over the faces it touches

Step 4 is the whole trick. An edge receives a SHARE of each face's trees in
proportion to the length IT contributes to that face, so when the server
computes `tree_score / length_m` every edge lying on one face returns the
same density -- the 2.6m stub and the 51.9m run agree, because they are the
same pavement. An edge spanning several faces returns their length-weighted
average. Each edge keeps its own length and its own cost.

WHY SAMPLED, NOT ONE ANSWER PER EDGE
------------------------------------
`match_line` gives one face per whole edge from a median probe distance,
and that fails for edges longer than a block: the median exceeds
BLOCK_FACE_MAX_M and the edge matches NOTHING, so the face keeps its trees
and loses its pavement. Face 1922610905 ("14 ST") held 13 trees over 221m
of kerb and was assigned 1.6m of sidewalk, reading 280x the citywide median
density. 28.3% of the city's sidewalk length is in pieces over 200m.
Full account in pipeline/graph/blockface.py:match_line_profile.

NOTHING IS CUT. No edge is divided, no node is added, the routing graph is
untouched -- and splitting would buy nothing anyway, because
pipeline/graph/pedestrian.py already cuts every way at every junction. An
edge long enough to span blocks is therefore one that nothing connects to
along its length: there is no corner inside it to turn at, so there is no
routing decision a finer split could enable.

WHAT SCORES ZERO, AND WHY
-------------------------
  crossings   A crossing runs ACROSS a roadway, so trees rarely shade it.
              User decision, 2026-08-24. This is not a special case: a
              crossing with no trees and a BARE SIDEWALK with no trees are
              the same thing -- unshaded pavement -- and get identical
              treatment. Crossings stay fully routable; they just carry no
              shade. They are also kept OUT of a face's length denominator,
              or a crossing's metres would dilute the density of pavement
              it is not part of.
  park paths  No kerb, no block face. Covered by the land-cover raster
              under the settled parks rule; that code is not written yet,
              so they score zero for now.
  the rest    Anything with no block face within config.BLOCK_FACE_MAX_M.
              80.9% of trees and ~98% of sidewalk length do attach; of the
              trees that do not, 80.8% are inside park polygons and are
              correctly excluded.
"""

import logging

from pipeline import config
from pipeline.scoring.trees import Totals, tree_value

logger = logging.getLogger(__name__)

# OSM `kind` values that receive shade and count toward a block's pavement
# length. Deliberately a narrow allow-list rather than "everything except
# crossings": a plaza, a park path or a set of steps has no block face
# either, and guessing on their behalf is how the centerline model grew
# rules nobody could later justify.
SHADED_KINDS = frozenset({"footway/sidewalk"})


def score_edges(edges: list[dict], tree_rows: list[dict], index) -> dict:
    """Fill tree_deciduous / tree_evergreen / tree_count on every edge.

    Mutates `edges` in place, the same way naming.assign_parent_names does,
    and returns a tally for the caller to log. Every edge gets the three
    fields whether or not it scored, so the export never has to guess -- an
    absent key and a zero mean different things and only one of them is
    true here.

    Also fills `side` from the block face's own L/R, which is what
    pedestrian.py's "C" placeholder was reserved for.
    """
    # --- trees onto faces ---------------------------------------------
    per_face: dict[str, Totals] = {}
    scored = dead_or_unusable = off_face = 0
    for row in tree_rows:
        value = tree_value(row)
        if value is None:
            dead_or_unusable += 1
            continue
        coords = (row.get("location") or {}).get("coordinates")
        if not coords:
            dead_or_unusable += 1
            continue
        match = index.match_point(coords[0], coords[1])
        if match is None:
            off_face += 1
            continue
        per_face.setdefault(match.face.face_id, Totals()).add(value)
        scored += 1
    logger.info(f"  [scoring] {scored:,} trees on {len(per_face):,} block "
                f"faces; {off_face:,} off-face, "
                f"{dead_or_unusable:,} dead or unusable")

    # --- sidewalks onto faces, and each face's pavement length --------
    # Two passes over the edges rather than one: a face's density is not
    # known until every edge on it has been counted, and an edge cannot be
    # scored before its own face's density exists.
    #
    # The profile is METRES PER FACE, not one face per edge, so a face's
    # length is the pavement genuinely beside it -- including the middle of
    # a 500m way whose ends belong to other blocks entirely.
    profile_of: dict[int, dict] = {}
    length_of_face: dict[str, float] = {}
    for position, edge in enumerate(edges):
        if edge.get("kind") not in SHADED_KINDS:
            continue
        profile = index.match_line_profile(edge["coords"])
        if not profile:
            continue
        profile_of[position] = profile
        for face_id, metres in profile.items():
            length_of_face[face_id] = (
                length_of_face.get(face_id, 0.0) + metres)

    # --- density, then each edge's share ------------------------------
    tally = {"edges": len(edges), "scored": 0, "no_face": 0,
             "not_shaded_kind": 0, "face_without_trees": 0, "spanning": 0}
    for position, edge in enumerate(edges):
        edge["tree_deciduous"] = 0.0
        edge["tree_evergreen"] = 0.0
        edge["tree_count"] = 0

        if edge.get("kind") not in SHADED_KINDS:
            tally["not_shaded_kind"] += 1
            continue
        profile = profile_of.get(position)
        if profile is None:
            tally["no_face"] += 1
            continue

        # The face this edge has the most pavement on. An edge spanning
        # blocks still has to report ONE side, and the dominant face is the
        # only defensible answer -- it is also what the whole edge is named
        # after downstream.
        dominant = max(profile.items(), key=lambda item: item[1])[0]
        dominant_face = index.face(dominant)
        if dominant_face is not None:
            edge["side"] = dominant_face.side

        deciduous = evergreen = count = 0.0
        scored_any = False
        for face_id, metres in profile.items():
            totals = per_face.get(face_id)
            if totals is None:
                continue
            face_length = length_of_face[face_id]
            if face_length <= 0:
                continue
            # This edge's share of THIS face is the length it contributes to
            # that face over the face's whole pavement -- not its own total
            # length, which for a spanning edge belongs to several blocks.
            share = metres / face_length
            deciduous += totals.deciduous * share
            evergreen += totals.evergreen * share
            count += totals.count * share
            scored_any = True

        if not scored_any:
            tally["face_without_trees"] += 1
            continue

        # NOT rounded. Rounding here breaks the property this whole design
        # exists to provide -- that every edge on a face reports the SAME
        # density -- because a fixed number of decimal places is a large
        # relative error on a short edge's small share. A 2.6m edge and a
        # 51.9m edge on one face disagreed in the 7th significant figure at
        # 6dp. Rounding is the export's decision, and export.py already
        # writes these two fields unrounded.
        edge["tree_deciduous"] = deciduous
        edge["tree_evergreen"] = evergreen
        # tree_count is the walker-facing "N trees along your route", so it
        # gets the same proportional share. It is fractional here on
        # purpose: rounding per edge would not sum back to the face's real
        # count, and graph_store.py already sums shares across an edge path
        # and rounds once at the end.
        edge["tree_count"] = count
        if len(profile) > 1:
            tally["spanning"] += 1
        tally["scored"] += 1

    logger.info(
        f"  [scoring] {tally['scored']:,}/{tally['edges']:,} edges scored; "
        f"{tally['no_face']:,} no block face, "
        f"{tally['face_without_trees']:,} on a treeless face, "
        f"{tally['not_shaded_kind']:,} not a sidewalk (crossings, park "
        f"paths, steps -- zero by design); "
        f"{tally['spanning']:,} scored edges span more than one block face")
    return tally
