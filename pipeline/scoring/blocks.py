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
BLOCK_FACE_MAX_M and the edge matches nothing, so the face keeps its trees
and loses its pavement (one 14th Street face held 13 trees over 221m of
kerb and was assigned 1.6m of sidewalk, 280x the citywide median density).
28.3% of the city's sidewalk length is in pieces over 200m. Full account
in pipeline/graph/blockface.py:match_line_profile.

Nothing is cut. No edge is divided, no node is added, the routing graph is
untouched -- and splitting would buy nothing anyway, because
pipeline/graph/pedestrian.py already cuts every way at every junction. An
edge long enough to span blocks is therefore one that nothing connects to
along its length: there is no corner inside it to turn at, so there is no
routing decision a finer split could enable.

WHAT SCORES ZERO, AND WHY
-------------------------
  crossings   A crossing runs across a roadway, so trees rarely shade it.
              This is not a special case: a crossing with no trees and a
              bare sidewalk with no trees are the same thing -- unshaded
              pavement -- and get identical treatment. Crossings stay
              fully routable; they just carry no shade. They are also kept
              out of a face's length denominator, or a crossing's metres
              would dilute the density of pavement it is not part of.
  park paths  No kerb, no block face. Scored from the land-cover raster
              by pipeline/scoring/canopy.py, which runs after this.
  the rest    Anything with no block face within config.BLOCK_FACE_MAX_M.
              80.9% of trees and ~98% of sidewalk length do attach; of the
              trees that do not, 80.8% are inside park polygons and are
              correctly excluded.

The two ways a SIDEWALK edge can end up at zero here are recorded on the
edge as `face_outcome` ("no_face" / "treeless_face"), because they mean
different things and only downstream code can act on the difference:
no-face means Forestry structurally could not answer, and a treeless face
inside a park means Forestry was never there to ask.
canopy.score_sidewalk_fallback consumes the marker and gives exactly those
edges the raster's answer instead; an ordinary treeless street keeps its
honest zero. The marker is pipeline-internal -- export.py whitelists its
fields, so it never reaches the file.
"""

import logging

from pipeline import config
from pipeline.scoring.trees import Totals, tree_value

logger = logging.getLogger(__name__)

# OSM `kind` values that receive shade and count toward a block's pavement
# length. Deliberately a narrow allow-list rather than "everything except
# crossings": a plaza, a park path or a set of steps has no block face
# either, and guessing on their behalf breeds rules nobody can later
# justify.
SHADED_KINDS = frozenset({"footway/sidewalk"})


def score_edges(edges: list[dict], tree_rows: list[dict], index) -> dict:
    """Fill tree_deciduous / tree_evergreen / tree_count on every edge.

    Mutates `edges` in place, the same way naming.assign_parent_names does,
    and returns a tally for the caller to log. Every edge gets the three
    fields whether or not it scored, so the export never has to guess -- an
    absent key and a zero mean different things and only one of them is
    true here.

    Also fills `side` -- the compass side of the parent street this
    pavement is on ("N"/"S"/"E"/"W", or "" when no plain word is honest).
    See BlockFaceIndex.compass_side for why it is geometric rather than
    CSCL's L/R.
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
        # Trees get their own, wider cap than sidewalk samples do -- a
        # crown reaches over pavement in a way a kerb line cannot. See
        # TREE_ATTACH_MAX_M's comment for the evidence and its 8m limit.
        match = index.match_point(coords[0], coords[1],
                                  max_m=config.TREE_ATTACH_MAX_M)
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
    # The profile is metres per face, not one face per edge, so a face's
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
            # Recorded on the edge, not just tallied: canopy.py's
            # score_sidewalk_fallback needs to know which zeros are
            # "Forestry could not answer" rather than "answered zero".
            edge["face_outcome"] = "no_face"
            tally["no_face"] += 1
            continue

        # The face this edge has the most pavement on. An edge spanning
        # blocks still has to report one side, and the dominant face is the
        # only defensible answer -- it is also what the whole edge is named
        # after downstream. The side is compass ("N"/"S"/"E"/"W", "" when
        # no plain word is honest), computed from this edge's own geometry
        # against that face's kerb -- not the face's CSCL L/R, which flips
        # at 53% of side-street boundaries (see BlockFaceIndex.compass_side).
        dominant = max(profile.items(), key=lambda item: item[1])[0]
        edge["side"] = index.compass_side(edge["coords"], dominant)

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
            edge["face_outcome"] = "treeless_face"
            tally["face_without_trees"] += 1
            continue

        # Not rounded. Rounding here breaks the property this whole design
        # exists to provide -- that every edge on a face reports the same
        # density -- because a fixed number of decimal places is a large
        # relative error on a short edge's small share. Rounding is the
        # export's decision, and export.py writes these two fields
        # unrounded.
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
