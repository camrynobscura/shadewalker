"""Tests for graph_store.py's cross-tile merge behavior: which copy of a
border edge survives, and which parallel edges get deduplicated.

Border edges are exported by both neighboring tiles, each scored against
only its own tile's tree fetch -- so the two copies can disagree, and
load() must keep the better-scored one rather than whichever tile's
filename happens to sort first (the bug: 3,740 edges citywide silently
kept the worse copy, including a 1.7km greenway edge held at 0 trees
while its other copy had 95). Exercised with synthetic two-tile fixtures,
same pattern as test_graph_store_known_gaps.py, so it runs in CI without
real data on disk.
"""

import gzip
import json

import pytest

from pipeline import config
from server.graph_store import GraphStore

NODES = {
    "n1": [-73.9900, 40.6800],
    "n2": [-73.9880, 40.6800],
    "n3": [-73.9860, 40.6800],
}


def _edge_record(u: str, v: str, *, key: int = 0, tree_deciduous: float = 0.0,
                 tree_evergreen: float = 0.0, tree_count: int = 0,
                 coords: list | None = None, name: str = "Border Street") -> dict:
    return {
        "u": u, "v": v, "key": key, "side": "C",
        "length_m": 170.0, "name": name,
        "tree_deciduous": tree_deciduous, "tree_evergreen": tree_evergreen,
        "tree_count": tree_count,
        "coords": coords if coords is not None else [NODES[u], NODES[v]],
    }


def _write_tile(path, edges: list[dict]) -> None:
    with gzip.open(path, "wt") as f:
        json.dump({"nodes": NODES, "edges": edges}, f)


def _load_from(tmp_path, monkeypatch, tiles: dict[str, list[dict]]) -> GraphStore:
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    for tile_name, edges in tiles.items():
        _write_tile(tmp_path / f"{tile_name}.json.gz", edges)
    store = GraphStore()
    store.load()
    return store


def test_better_scored_copy_wins_when_it_loads_second(tmp_path, monkeypatch):
    """The alphabetically-first tile holds the undercounted copy (its tree
    fetch didn't cover this edge); the later tile's copy saw the trees."""
    store = _load_from(tmp_path, monkeypatch, {
        "a_first": [_edge_record("n1", "n2", tree_deciduous=0.0, tree_count=0)],
        "b_second": [_edge_record("n1", "n2", tree_deciduous=9.5, tree_count=12)],
    })
    assert len(store._length) == 1
    assert store._tree_count[0] == 12
    assert store._tree_deciduous[0] == pytest.approx(9.5)


def test_better_scored_copy_stays_when_it_loads_first(tmp_path, monkeypatch):
    store = _load_from(tmp_path, monkeypatch, {
        "a_first": [_edge_record("n1", "n2", tree_deciduous=9.5, tree_count=12)],
        "b_second": [_edge_record("n1", "n2", tree_deciduous=0.0, tree_count=0)],
    })
    assert len(store._length) == 1
    assert store._tree_count[0] == 12


def test_better_copy_brings_its_own_geometry_along(tmp_path, monkeypatch):
    """38 of the 4,495 disagreeing copies citywide also differ in geometry --
    the whole better copy is kept, not just its scores, so scores and shape
    can't end up mixed from two different copies."""
    detoured = [NODES["n1"], [-73.9890, 40.6805], NODES["n2"]]
    store = _load_from(tmp_path, monkeypatch, {
        "a_first": [_edge_record("n1", "n2", tree_count=0)],
        "b_second": [_edge_record("n1", "n2", tree_deciduous=4.0, tree_count=5,
                                  coords=detoured)],
    })
    assert len(store._length) == 1
    assert store._tree_count[0] == 5
    assert len(store._edge_coords(0)) == 3  # the detoured shape, not the 2-point one


def test_identical_stacked_ways_collapse_to_one_edge(tmp_path, monkeypatch):
    """OSM sometimes contains the same way twice; osmnx keeps both as
    parallel edges under different multigraph keys (159 confirmed citywide,
    all within a single tile). Only one should load."""
    store = _load_from(tmp_path, monkeypatch, {
        "only": [
            _edge_record("n1", "n2", key=0, tree_count=3, tree_deciduous=2.0),
            _edge_record("n1", "n2", key=1, tree_count=3, tree_deciduous=2.0),
        ],
    })
    assert len(store._length) == 1


def test_reversed_duplicate_geometry_still_collapses(tmp_path, monkeypatch):
    """The duplicate way can be digitized in the opposite direction; the
    dedupe is direction-insensitive."""
    store = _load_from(tmp_path, monkeypatch, {
        "only": [
            _edge_record("n1", "n2", key=0),
            _edge_record("n2", "n1", key=1, coords=[NODES["n2"], NODES["n1"]]),
        ],
    })
    assert len(store._length) == 1


def test_genuinely_different_parallel_edges_both_survive(tmp_path, monkeypatch):
    """Two real, distinct edges between the same nodes (a street and a
    separate path curving another way) must NOT be deduplicated -- the
    stacked-way rule keys on geometry, not just the node pair."""
    path_coords = [NODES["n1"], [-73.9890, 40.6810], NODES["n2"]]
    store = _load_from(tmp_path, monkeypatch, {
        "only": [
            _edge_record("n1", "n2", key=0, name="Border Street"),
            _edge_record("n1", "n2", key=1, name="Park Path", coords=path_coords),
        ],
    })
    assert len(store._length) == 2
