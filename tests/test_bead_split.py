"""Unit tests for graph_store._split_edge_at_beads -- the core of FIXES 13
(severed-overlap / bead-identity reconciliation).

The split is what rejoins a street severed at a tile border: an edge is cut
at every interior geometry vertex whose OSM node id was ALSO loaded as an
endpoint by some other tile. These tests pin the arithmetic (length and tree
credit must be conserved) and the guards (never split where there is nothing
real to attach to), independent of any tile fixture.
"""
import pytest

from server.graph_store import _bead_split_points, _split_edge_at_beads

# A four-vertex edge A -> B -> C -> D, ~stepping east. Coordinates are
# [lon, lat]; the metric spacing between them doesn't need to be uniform.
COORDS = [[-74.0000, 40.6000], [-74.0010, 40.6005],
          [-74.0020, 40.6010], [-74.0030, 40.6015]]
NODE_IDS = ["A", "B", "C", "D"]
LEN_M = 300.0
DECID, EVERG, CNT, CANOPY = 12.0, 3.0, 15, 4.5


def _split(id_to_idx):
    return _split_edge_at_beads(
        "A", "D", 0, COORDS, NODE_IDS, LEN_M, DECID, EVERG, CNT, CANOPY, id_to_idx
    )


def test_no_interior_bead_loaded_returns_whole_edge():
    # Only the endpoints are loaded nodes -- the common case, and the
    # single-tile case: interior beads B, C aren't endpoints anywhere.
    pieces = _split({"A": 0, "D": 1})
    assert len(pieces) == 1
    u, v, key, length, *_ = pieces[0]
    assert (u, v, length) == ("A", "D", LEN_M)


def test_missing_node_ids_is_a_noop():
    # Pre-v21 tiles carry no node_ids -- backward-compatible: one whole edge.
    pieces = _split_edge_at_beads(
        "A", "D", 0, COORDS, None, LEN_M, DECID, EVERG, CNT, CANOPY, {"A": 0, "D": 1}
    )
    assert len(pieces) == 1


def test_two_point_edge_never_splits():
    coords = [[-74.0, 40.6], [-74.001, 40.6005]]
    pieces = _split_edge_at_beads(
        "A", "B", 0, coords, ["A", "B"], 100.0, 1.0, 0.0, 2, 0.0,
        {"A": 0, "B": 1},
    )
    assert len(pieces) == 1


def test_none_endpoint_declines_to_split():
    # A synthetic connector whose real-street end didn't map (None): even
    # with a loaded interior bead, leave it whole rather than guess.
    node_ids = ["A", "B", None]
    pieces = _split_edge_at_beads(
        "A", None, 0, COORDS[:3], node_ids, 200.0, 1.0, 0.0, 2, 0.0,
        {"A": 0, "B": 1},
    )
    assert len(pieces) == 1


def test_splits_at_a_loaded_interior_bead():
    # B is loaded (some other tile exported it as an endpoint) -> split at B.
    pieces = _split({"A": 0, "B": 1, "D": 2})
    assert len(pieces) == 2
    assert pieces[0][0] == "A" and pieces[0][1] == "B"
    assert pieces[1][0] == "B" and pieces[1][1] == "D"


def test_splits_at_every_loaded_interior_bead():
    pieces = _split({"A": 0, "B": 1, "C": 2, "D": 3})
    assert len(pieces) == 3
    assert [(p[0], p[1]) for p in pieces] == [("A", "B"), ("B", "C"), ("C", "D")]


def test_length_and_tree_floats_are_conserved():
    pieces = _split({"A": 0, "B": 1, "C": 2, "D": 3})
    assert sum(p[3] for p in pieces) == pytest.approx(LEN_M)
    assert sum(p[4] for p in pieces) == pytest.approx(DECID)   # deciduous
    assert sum(p[5] for p in pieces) == pytest.approx(EVERG)   # evergreen
    assert sum(p[7] for p in pieces) == pytest.approx(CANOPY)  # park canopy


def test_tree_count_pieces_sum_close_to_original():
    pieces = _split({"A": 0, "B": 1, "C": 2, "D": 3})
    total = sum(p[6] for p in pieces)
    assert abs(total - CNT) <= len(pieces)  # per-piece integer rounding only


def test_pieces_tile_the_original_geometry_contiguously():
    pieces = _split({"A": 0, "B": 1, "C": 2, "D": 3})
    # First piece starts at the original start; last ends at the original end.
    assert pieces[0][8][0] == COORDS[0]
    assert pieces[-1][8][-1] == COORDS[-1]
    # Each piece begins exactly where the previous ended (shared bead vertex).
    for earlier, later in zip(pieces, pieces[1:]):
        assert earlier[8][-1] == later[8][0]


def test_matching_pieces_from_two_tiles_get_the_same_key():
    # The same physical span, cut identically, must hash to one dedupe key so
    # the border-dedupe collapses the copies -- even reversed (other tile's
    # geometry may run the other way).
    forward = _split_edge_at_beads(
        "A", "C", 0, COORDS[:3], ["A", "B", "C"], 200.0, 1.0, 0.0, 2, 0.0,
        {"A": 0, "B": 1, "C": 2},
    )
    rev_coords = COORDS[:3][::-1]
    reverse = _split_edge_at_beads(
        "C", "A", 7, rev_coords, ["C", "B", "A"], 200.0, 9.0, 0.0, 9, 0.0,
        {"A": 0, "B": 1, "C": 2},
    )
    fwd_keys = sorted(p[2] for p in forward)
    rev_keys = sorted(p[2] for p in reverse)
    assert fwd_keys == rev_keys


def test_bead_split_points_only_returns_loaded_interior_indices():
    assert _bead_split_points(NODE_IDS, COORDS, {"A": 0, "D": 1}) == []
    assert _bead_split_points(NODE_IDS, COORDS, {"A": 0, "B": 1, "D": 2}) == [1]
    assert _bead_split_points(NODE_IDS, COORDS, {"A": 0, "B": 1, "C": 2, "D": 3}) == [1, 2]
