"""Tests for graph_store.py's KNOWN_NODE_GAPS patch -- a short, manually
verified list of real OSM node-id pairs that are the same physical corner
but got recorded as two different nodes (a genuine digitization gap; see
PLAN.md's Rockaway/Cross Bay Bridge finding). Exercised here with a
synthetic two-tile fixture reusing the real node ids from the first entry,
so this runs in CI without needing the real Queens data on disk.
"""

import gzip
import json

import pytest

from pipeline import config
from server import graph_store
from server.graph_store import GraphStore

# The real ids from KNOWN_NODE_GAPS[0] -- reused here so the patch logic
# under test actually fires, rather than testing a stand-in pair it would
# never touch in production.
GAP_NODE_A, GAP_NODE_B, GAP_NAME = graph_store.KNOWN_NODE_GAPS[0]

MAIN_NODES = {
    "m1": [-73.9900, 40.6800],
    "m2": [-73.9880, 40.6800],
    GAP_NODE_B: [-73.9860, 40.6800],  # the "East 21st Road" side, real id
}
MAIN_EDGES = [
    ("m1", "m2", "Alpha Street", 170.0, 3),
    ("m2", GAP_NODE_B, "Beta Avenue", 150.0, 2),
]

# ~2.5km away -- well past 2x MAX_SNAP_DISTANCE_M, so this starts out a
# genuinely separate component, the same shape as the real Rockaway piece.
ISLAND_NODES = {
    GAP_NODE_A: [-74.0170, 40.6890],  # the "Cross Bay Bridge" side, real id
    "i2": [-74.0150, 40.6890],
}
ISLAND_EDGES = [(GAP_NODE_A, "i2", "Island Path", 165.0, 9)]


def _edge_record(u: str, v: str, name: str, length_m: float, tree_count: int, nodes: dict) -> dict:
    return {
        "u": u, "v": v, "key": 0, "side": "C",
        "length_m": length_m, "name": name,
        "tree_deciduous": float(tree_count), "tree_evergreen": 0.0,
        "tree_count": tree_count,
        "coords": [nodes[u], nodes[v]],
    }


def _write_tile(path, nodes: dict, edges: list) -> None:
    tile = {
        "nodes": nodes,
        "edges": [_edge_record(u, v, name, length_m, count, nodes)
                  for u, v, name, length_m, count in edges],
    }
    with gzip.open(path, "wt") as f:
        json.dump(tile, f)


@pytest.fixture()
def store_with_the_real_gap_ids(tmp_path, monkeypatch) -> GraphStore:
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", ISLAND_NODES, ISLAND_EDGES)
    store = GraphStore()
    store.load()
    return store


def test_load_bridges_a_known_node_gap(store_with_the_real_gap_ids):
    store = store_with_the_real_gap_ids
    components = store._graph.connected_components(mode="weak")
    assert len(components) == 1

    lon_a, lat_a = MAIN_NODES["m1"]
    lon_b, lat_b = ISLAND_NODES["i2"]
    pair = store.snap_pair(lat_a, lon_a, lat_b, lon_b)
    assert pair is not None
    start, end = pair
    result = store.route(start, end, tree_weight=0, month=7)
    assert result is not None
    assert result["length_m"] > 0


def test_load_names_the_bridging_edge(store_with_the_real_gap_ids):
    store = store_with_the_real_gap_ids
    assert GAP_NAME in store._names


def test_load_skips_a_known_gap_whose_node_ids_are_absent(tmp_path, monkeypatch):
    # Neither real gap-node id appears anywhere in this fixture (it's the
    # same shape as test_graph_store_pruning.py's fixtures) -- load() must
    # not crash, and must not add a synthetic edge that doesn't belong.
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    nodes = {"m1": [-73.99, 40.68], "m2": [-73.988, 40.68]}
    _write_tile(tmp_path / "main.json.gz", nodes, [("m1", "m2", "Alpha Street", 170.0, 3)])

    store = GraphStore()
    store.load()

    assert store._graph.ecount() == 1
    assert GAP_NAME not in store._names
