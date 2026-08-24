"""Tests for pipeline/scoring/blocks.py -- shade spread across a block face.

The property under test is the one the whole design rests on: EVERY EDGE ON
A BLOCK FACE MUST REPORT THE SAME DENSITY, whatever length OSM chopped it
to. That is what fixes the original bug, where the same tree read
1-per-2.6m on one piece and 1-per-51.9m on the next.

The index is faked here. A real BlockFaceIndex needs 170,985 road-edge kerb
lines and the whole CSCL layer; what this module actually depends on is the
narrow contract "give me the face for this point / the length profile for
this line", so the tests supply that directly and stay fast. Coverage of the
real index lives in the audit tools (measure_kerb_vs_centerline.py,
test_tree_block_assignment.py).

The fake speaks PROFILES -- {face_id: metres} -- because that is what
scoring now consumes. `match_line`'s one-face-per-edge answer was retired
for scoring on 2026-08-24: it matched nothing at all on edges longer than a
block, which is 28.3% of the city's sidewalk length.
"""

import pytest

from pipeline.graph.blockface import Face, Match
from pipeline.scoring import blocks


def face(face_id="F1", side="L", street="COURT ST", segment="900"):
    return Face(face_id=face_id, segment_id=segment, side=side,
                street=street, boro="3", width_ft=32.0)


class FakeIndex:
    """Maps a coordinate to a face, and a line to a length profile.

    `points` is {(lon, lat): Face}; `lines` is {first coord: {face_id:
    metres}}. Anything absent returns None / {}, which is how "no block face
    within range" reaches the code under test.
    """

    def __init__(self, points=None, lines=None, faces=None):
        self._points = points or {}
        self._lines = lines or {}
        self._faces = faces or {}

    def match_point(self, lon, lat, max_m=None):
        hit = self._points.get((lon, lat))
        return Match(hit, 1.0, None) if hit else None

    def match_line_profile(self, coords, step_m=None, max_m=None):
        return dict(self._lines.get(tuple(coords[0]), {}))

    def face(self, face_id):
        return self._faces.get(face_id)


def fake_index(points=None, spans=None):
    """Build a FakeIndex from {first coord: [(Face, metres), ...]}.

    Spelled out per edge rather than derived from edge length, because the
    two are deliberately NOT the same number any more: an edge contributes
    only the metres that actually lie beside a face.
    """
    lines, faces = {}, {}
    for first, allocation in (spans or {}).items():
        profile = {}
        for block_face, metres in allocation:
            profile[block_face.face_id] = (
                profile.get(block_face.face_id, 0.0) + metres)
            faces[block_face.face_id] = block_face
        lines[first] = profile
    for block_face in (points or {}).values():
        faces[block_face.face_id] = block_face
    return FakeIndex(points=points, lines=lines, faces=faces)


def edge(u, v, length_m, kind="footway/sidewalk", first=(0.0, 0.0)):
    return {"u": u, "v": v, "key": 0, "side": "C", "length_m": length_m,
            "name": "", "kind": kind,
            "coords": [list(first), [1.0, 1.0]]}


def tree(lon, lat, dbh="30", condition="Excellent",
         species="Acer rubrum - red maple"):
    return {"dbh": dbh, "tpcondition": condition, "genusspecies": species,
            "location": {"type": "Point", "coordinates": [lon, lat]}}


# --- the load-bearing property ----------------------------------------

def test_every_edge_on_a_face_reports_the_same_density():
    """A 2.6m stub and a 51.9m run on one block face must agree. This is
    the entire reason the block face is the unit."""
    f = face()
    edges = [edge("a", "b", 2.6, first=(0.0, 0.0)),
             edge("b", "c", 51.9, first=(0.1, 0.1)),
             edge("c", "d", 30.5, first=(0.2, 0.2))]
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 2.6)],
                              (0.1, 0.1): [(f, 51.9)],
                              (0.2, 0.2): [(f, 30.5)]})

    blocks.score_edges(edges, [tree(-73.9, 40.7)], index)

    densities = [(e["tree_deciduous"] + e["tree_evergreen"]) / e["length_m"]
                 for e in edges]
    assert densities[0] == pytest.approx(densities[1])
    assert densities[1] == pytest.approx(densities[2])


def test_the_shares_sum_back_to_the_faces_own_total():
    """Proportional shares must conserve the trees, not invent or lose any."""
    f = face()
    edges = [edge("a", "b", 20.0, first=(0.0, 0.0)),
             edge("b", "c", 60.0, first=(0.1, 0.1))]
    index = fake_index(points={(-73.9, 40.7): f, (-73.8, 40.7): f},
                       spans={(0.0, 0.0): [(f, 20.0)],
                              (0.1, 0.1): [(f, 60.0)]})

    blocks.score_edges(edges, [tree(-73.9, 40.7), tree(-73.8, 40.7)], index)

    # two Excellent trees at exactly the dbh cap: value 1.0 each
    assert sum(e["tree_deciduous"] for e in edges) == pytest.approx(2.0)
    assert sum(e["tree_count"] for e in edges) == pytest.approx(2.0)
    # and the 60m edge gets three times the 20m edge's share
    assert edges[1]["tree_deciduous"] == pytest.approx(
        edges[0]["tree_deciduous"] * 3)


def test_a_short_edge_keeps_a_fractional_count():
    """The bug that made export truncate: a small share must survive as a
    fraction rather than collapsing to zero."""
    f = face()
    edges = [edge("a", "b", 5.0, first=(0.0, 0.0)),
             edge("b", "c", 95.0, first=(0.1, 0.1))]
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 5.0)],
                              (0.1, 0.1): [(f, 95.0)]})

    blocks.score_edges(edges, [tree(-73.9, 40.7)], index)
    assert 0 < edges[0]["tree_count"] < 1
    assert edges[0]["tree_count"] == pytest.approx(0.05)


# --- the long-edge fix: an edge spanning block faces ------------------
#
# This is what match_line could not do at all. Before 2026-08-24 an edge
# running past several blocks exceeded BLOCK_FACE_MAX_M on the median and
# matched NOTHING -- scoring zero itself and starving every face it passed.

def test_an_edge_spanning_two_faces_gets_a_length_weighted_average():
    """The 500m-way case. 100m beside a leafy block and 100m beside a bare
    one must average, not pick a winner and not fall through to zero."""
    leafy, bare = face("F1"), face("F2")
    spanning = edge("a", "b", 200.0, first=(0.0, 0.0))
    index = fake_index(
        points={(-73.9, 40.7): leafy},
        spans={(0.0, 0.0): [(leafy, 100.0), (bare, 100.0)]})

    blocks.score_edges([spanning], [tree(-73.9, 40.7)], index)

    # leafy holds one whole tree over its 100m; bare holds none. The edge
    # covers all of both, so it takes the whole tree over its own 200m.
    assert spanning["tree_deciduous"] == pytest.approx(1.0)
    density = spanning["tree_deciduous"] / spanning["length_m"]
    assert density == pytest.approx(1.0 / 200.0)


def test_a_spanning_edge_shares_each_face_with_that_faces_other_edges():
    """The spanning edge must not take a whole face's trees when another
    edge also sits on that face -- each takes its own metres' worth."""
    shared = face("F1")
    other = face("F2")
    spanning = edge("a", "b", 100.0, first=(0.0, 0.0))
    local = edge("c", "d", 50.0, first=(0.1, 0.1))
    index = fake_index(
        points={(-73.9, 40.7): shared},
        spans={(0.0, 0.0): [(shared, 50.0), (other, 50.0)],
               (0.1, 0.1): [(shared, 50.0)]})

    blocks.score_edges([spanning, local], [tree(-73.9, 40.7)], index)

    # F1 has 100m of pavement on it and one tree; each edge contributes 50m
    assert spanning["tree_deciduous"] == pytest.approx(0.5)
    assert local["tree_deciduous"] == pytest.approx(0.5)


def test_a_faces_length_counts_only_the_metres_beside_it():
    """The starved-denominator bug. A 500m edge that runs 20m past a short
    face must add 20m to that face, not 500m and not nothing."""
    small = face("F1")
    elsewhere = face("F2")
    long_edge = edge("a", "b", 500.0, first=(0.0, 0.0))
    index = fake_index(
        points={(-73.9, 40.7): small},
        spans={(0.0, 0.0): [(small, 20.0), (elsewhere, 480.0)]})

    blocks.score_edges([long_edge], [tree(-73.9, 40.7)], index)

    # F1's whole pavement is that 20m, so the edge takes all of its one tree
    assert long_edge["tree_deciduous"] == pytest.approx(1.0)


def test_unmatched_metres_are_not_credited_to_a_neighbouring_block():
    """Half an edge beside a face, half beside nothing. The unattributable
    half scores zero rather than silently inheriting the face's shade."""
    f = face()
    half_on = edge("a", "b", 100.0, first=(0.0, 0.0))
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 50.0)]})

    blocks.score_edges([half_on], [tree(-73.9, 40.7)], index)

    # the face's pavement is 50m and holds one tree -> density 0.02/m.
    # the edge reports that over its full 100m: half the face's density.
    assert half_on["tree_deciduous"] == pytest.approx(1.0)
    assert half_on["tree_deciduous"] / half_on["length_m"] == pytest.approx(0.01)


# --- what scores zero -------------------------------------------------

def test_crossings_score_zero_and_do_not_dilute_the_block():
    """User decision 2026-08-24. The crossing's 12m must not enter the
    denominator, or it would water down the sidewalk's density."""
    f = face()
    sidewalk = edge("a", "b", 88.0, first=(0.0, 0.0))
    crossing = edge("b", "c", 12.0, kind="footway/crossing", first=(0.1, 0.1))
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 88.0)],
                              (0.1, 0.1): [(f, 12.0)]})

    blocks.score_edges([sidewalk, crossing], [tree(-73.9, 40.7)], index)

    assert crossing["tree_deciduous"] == 0.0
    assert crossing["tree_count"] == 0
    # the whole tree landed on the sidewalk, undiluted by the crossing
    assert sidewalk["tree_deciduous"] == pytest.approx(1.0)


def test_park_paths_and_steps_score_zero():
    f = face()
    edges = [edge("a", "b", 40.0, kind="footway", first=(0.0, 0.0)),
             edge("b", "c", 10.0, kind="steps", first=(0.1, 0.1))]
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 40.0)],
                              (0.1, 0.1): [(f, 10.0)]})
    blocks.score_edges(edges, [tree(-73.9, 40.7)], index)
    assert all(e["tree_deciduous"] == 0.0 for e in edges)


def test_an_edge_with_no_block_face_scores_zero_but_keeps_its_fields():
    """An absent key and a zero mean different things; every edge gets the
    fields so the export never has to guess."""
    edges = [edge("a", "b", 40.0, first=(9.9, 9.9))]
    blocks.score_edges(edges, [], fake_index())
    assert edges[0]["tree_deciduous"] == 0.0
    assert edges[0]["tree_evergreen"] == 0.0
    assert edges[0]["tree_count"] == 0


def test_a_tree_off_any_face_is_not_credited_anywhere():
    f = face()
    edges = [edge("a", "b", 40.0, first=(0.0, 0.0))]
    index = fake_index(spans={(0.0, 0.0): [(f, 40.0)]})  # no point mapping
    tally = blocks.score_edges(edges, [tree(-73.9, 40.7)], index)
    assert edges[0]["tree_deciduous"] == 0.0
    assert tally["face_without_trees"] == 1


def test_dead_trees_never_reach_a_block():
    f = face()
    edges = [edge("a", "b", 40.0, first=(0.0, 0.0))]
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 40.0)]})
    blocks.score_edges(edges, [tree(-73.9, 40.7, condition="Dead")], index)
    assert edges[0]["tree_deciduous"] == 0.0


# --- side, and the evergreen split ------------------------------------

def test_side_is_filled_from_the_block_face():
    """pedestrian.py leaves side as the placeholder "C" for this step."""
    edges = [edge("a", "b", 40.0, first=(0.0, 0.0))]
    assert edges[0]["side"] == "C"
    f = face(side="R")
    index = fake_index(points={(-73.9, 40.7): f},
                       spans={(0.0, 0.0): [(f, 40.0)]})
    blocks.score_edges(edges, [tree(-73.9, 40.7)], index)
    assert edges[0]["side"] == "R"


def test_a_spanning_edge_takes_the_side_of_its_dominant_face():
    """An edge across two blocks still reports one side: the one it has
    the most pavement on."""
    mostly = face("F1", side="R")
    barely = face("F2", side="L")
    spanning = edge("a", "b", 100.0, first=(0.0, 0.0))
    index = fake_index(points={(-73.9, 40.7): mostly},
                       spans={(0.0, 0.0): [(mostly, 90.0), (barely, 10.0)]})
    blocks.score_edges([spanning], [tree(-73.9, 40.7)], index)
    assert spanning["side"] == "R"


def test_evergreen_and_deciduous_stay_in_their_own_columns():
    f = face()
    edges = [edge("a", "b", 50.0, first=(0.0, 0.0))]
    index = fake_index(
        points={(-73.9, 40.7): f, (-73.8, 40.7): f},
        spans={(0.0, 0.0): [(f, 50.0)]})
    blocks.score_edges(
        edges,
        [tree(-73.9, 40.7),
         tree(-73.8, 40.7, species="Pinus strobus - eastern white pine")],
        index)
    assert edges[0]["tree_deciduous"] == pytest.approx(1.0)
    assert edges[0]["tree_evergreen"] == pytest.approx(1.0)
    assert edges[0]["tree_count"] == pytest.approx(2.0)


# --- two faces do not leak into each other ---------------------------

def test_two_faces_are_scored_independently():
    left, right = face("F1", side="L"), face("F2", side="R")
    edges = [edge("a", "b", 50.0, first=(0.0, 0.0)),
             edge("c", "d", 50.0, first=(0.1, 0.1))]
    index = fake_index(points={(-73.9, 40.7): left},
                       spans={(0.0, 0.0): [(left, 50.0)],
                              (0.1, 0.1): [(right, 50.0)]})

    blocks.score_edges(edges, [tree(-73.9, 40.7)], index)

    assert edges[0]["tree_deciduous"] == pytest.approx(1.0)
    assert edges[1]["tree_deciduous"] == 0.0   # the other side of the street
    assert edges[0]["side"] == "L"
    assert edges[1]["side"] == "R"


def test_the_tally_counts_spanning_edges():
    """Reported so a citywide run says how much pavement crosses blocks."""
    one, two = face("F1"), face("F2")
    edges = [edge("a", "b", 50.0, first=(0.0, 0.0)),
             edge("c", "d", 80.0, first=(0.1, 0.1))]
    index = fake_index(points={(-73.9, 40.7): one},
                       spans={(0.0, 0.0): [(one, 50.0)],
                              (0.1, 0.1): [(one, 40.0), (two, 40.0)]})
    tally = blocks.score_edges(edges, [tree(-73.9, 40.7)], index)
    assert tally["spanning"] == 1
    assert tally["scored"] == 2
