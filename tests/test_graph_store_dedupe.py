"""The loader's two duplicate rules, pinned on a tiny synthetic export.

server/graph_store.py's load() collapses duplicates two ways, and neither
was covered by a test until `server-memory` (2026-09-26) rewrote the loader
around flat arrays:

1. The SAME edge twice (same unordered node pair, key and side) -- the
   tiled era's border copies. The better-scored copy wins and takes the
   first copy's position; every per-edge attribute (length, trees, name,
   kind, fold names, geometry, building shade) must come from the winner.
2. An OSM way mapped twice: same node pair and side, different multigraph
   key, identical geometry (either direction). The later copy is dropped.
   A genuinely different parallel way between the same nodes survives.

The tests were written against the pre-rewrite loader first, so they
describe its behaviour, not the new code's.
"""
import base64
import gzip
import json

import numpy as np

from pipeline import config
from server.graph_store import SHADE_SLOTS, GraphStore

A, B, C = [-73.9900, 40.6800], [-73.9880, 40.6800], [-73.9880, 40.6820]
NODES = {"a": A, "b": B, "c": C}
MID_1 = [-73.9890, 40.68005]      # two different bends between a and b
MID_2 = [-73.9890, 40.67995]


def _shade(value: int) -> str:
    return base64.b64encode(bytes([value]) * SHADE_SLOTS).decode()


def _edge(u, v, *, key=0, side="N", trees=1.0, name="", coords=None,
          kind="footway/sidewalk", folds=(), shade=None, length=200.0):
    record = {
        "u": u, "v": v, "key": key, "side": side, "length_m": length,
        "name": name, "kind": kind, "fold_names": list(folds),
        "tree_deciduous": trees, "tree_evergreen": 0.0,
        "tree_count": int(trees), "tree_park_canopy": 0.0,
        "coords": coords or [NODES[u], NODES[v]],
    }
    if shade is not None:
        record["building_shade"] = _shade(shade)
    return record


def _load(tmp_path, monkeypatch, *files) -> GraphStore:
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    for i, edges in enumerate(files):
        with gzip.open(tmp_path / f"part{i}.json.gz", "wt") as f:
            json.dump({"nodes": NODES, "edges": edges}, f)
    store = GraphStore()
    store.load()
    return store


def _arrays_aligned(store: GraphStore) -> bool:
    n = store._graph.ecount()
    per_edge = [store._length, store._tree_deciduous, store._tree_evergreen,
                store._tree_count, store._tree_park_canopy, store._edge_component]
    lists = [store._names, store._kinds, store._sides, store._fold_names]
    return (all(len(a) == n for a in per_edge) and all(len(x) == n for x in lists)
            and store._building_shade.shape == (SHADE_SLOTS, n)
            and len(store._coord_offsets) == n + 1)


def test_a_better_scored_duplicate_replaces_every_attribute_in_place(tmp_path, monkeypatch):
    first = [_edge("a", "b", trees=1.0, name="First", coords=[A, MID_1, B], shade=10,
                   folds=("F1",), length=210.0),
             _edge("b", "c", trees=2.0, name="Other")]
    # Same edge, reversed endpoints (b, a): the dedupe key is the UNORDERED
    # pair, so this is a duplicate. More trees -> it wins.
    second = [_edge("b", "a", trees=5.0, name="Second", coords=[B, MID_2, A], shade=200,
                    kind="footway/crossing", folds=("S1", "S2"), length=190.0)]
    store = _load(tmp_path, monkeypatch, first, second)

    assert store._graph.ecount() == 2
    assert _arrays_aligned(store)
    e = 0                                     # the winner keeps the first copy's position
    assert store._names[e] == "Second"
    assert store._kinds[e] == "footway/crossing"
    assert store._fold_names[e] == ("S1", "S2")
    assert store._tree_deciduous[e] == 5.0
    assert store._length[e] == np.float32(190.0)
    assert np.array_equal(store._edge_coords(e), np.array([B, MID_2, A]))
    assert set(store._building_shade[:, e]) == {200}
    assert store._names[1] == "Other"         # the neighbour is untouched
    assert np.array_equal(store._edge_coords(1), np.array([B, C]))


def test_a_worse_scored_duplicate_is_ignored(tmp_path, monkeypatch):
    first = [_edge("a", "b", trees=5.0, name="First", coords=[A, MID_1, B], shade=10)]
    second = [_edge("a", "b", trees=1.0, name="Second", coords=[A, MID_2, B], shade=200)]
    store = _load(tmp_path, monkeypatch, first, second)

    assert store._graph.ecount() == 1
    assert store._names[0] == "First"
    assert np.array_equal(store._edge_coords(0), np.array([A, MID_1, B]))
    assert set(store._building_shade[:, 0]) == {10}


def test_a_duplicate_can_bring_shade_to_an_edge_that_had_none(tmp_path, monkeypatch):
    first = [_edge("a", "b", trees=1.0)]                 # no building_shade field
    second = [_edge("a", "b", trees=3.0, shade=77)]
    store = _load(tmp_path, monkeypatch, first, second)
    assert set(store._building_shade[:, 0]) == {77}


def test_an_identical_parallel_way_is_dropped_in_either_direction(tmp_path, monkeypatch):
    edges = [_edge("a", "b", key=0, name="Kept", coords=[A, MID_1, B]),
             _edge("a", "b", key=1, name="Same way again", coords=[A, MID_1, B]),
             _edge("b", "a", key=2, name="Same way, reversed", coords=[B, MID_1, A])]
    store = _load(tmp_path, monkeypatch, edges)
    assert store._graph.ecount() == 1
    assert store._names == ["Kept"]
    assert _arrays_aligned(store)


def test_a_different_parallel_way_survives(tmp_path, monkeypatch):
    edges = [_edge("a", "b", key=0, name="North bend", coords=[A, MID_1, B]),
             _edge("a", "b", key=1, name="South bend", coords=[A, MID_2, B])]
    store = _load(tmp_path, monkeypatch, edges)
    assert store._graph.ecount() == 2
    assert store._names == ["North bend", "South bend"]
    assert _arrays_aligned(store)


def test_the_same_way_on_the_other_side_is_not_a_duplicate(tmp_path, monkeypatch):
    edges = [_edge("a", "b", side="N", name="North side"),
             _edge("a", "b", side="S", name="South side")]
    store = _load(tmp_path, monkeypatch, edges)
    assert store._graph.ecount() == 2
    assert store._sides == ["N", "S"]
