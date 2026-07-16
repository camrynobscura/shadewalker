"""Load-time pruning of unreachable components, exercised deterministically.

The standing invariant in test_route_invariants.py asserts the loaded graph
is one connected component -- but in CI, where data/tiles/ holds only the
pilot tile, the data is already one component and the pruning branch never
actually runs. These tests fabricate a two-component dataset (a small main
network plus a disconnected fragment across "water", split over two tile
files the way a real border tile would be) so the prune-and-reindex path
executes on every run. The reindexing filters seven parallel node/edge
structures in lockstep; the alignment tests below are what catch an
off-by-one that would silently pair one edge's name with another's geometry.
"""

import gzip
import json

import pytest

from pipeline import config
from server.graph_store import GraphStore

MAIN_NODES = {
    "m1": [-73.9900, 40.6800],
    "m2": [-73.9880, 40.6800],
    "m3": [-73.9880, 40.6820],
    "m4": [-73.9900, 40.6820],
}
FRAGMENT_NODES = {
    "f1": [-74.0500, 40.7200],
    "f2": [-74.0480, 40.7200],
}

# (u, v, name, length_m, tree_count) -- distinct values per edge on purpose,
# so the alignment tests can prove each attribute stayed with its own edge.
MAIN_EDGES = [
    ("m1", "m2", "Alpha Street", 170.0, 3),
    ("m2", "m3", "Beta Avenue", 220.0, 5),
    ("m3", "m4", "Gamma Road", 130.0, 0),
]
FRAGMENT_EDGES = [("f1", "f2", "Foreign Lane", 165.0, 9)]


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
def pruned_store(tmp_path, monkeypatch) -> GraphStore:
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "fragment.json.gz", FRAGMENT_NODES, FRAGMENT_EDGES)
    store = GraphStore()
    store.load()
    return store


def test_pruning_drops_the_disconnected_fragment(pruned_store):
    assert pruned_store._graph.vcount() == len(MAIN_NODES)
    assert pruned_store._graph.ecount() == len(MAIN_EDGES)
    for node_id in MAIN_NODES:
        assert node_id in pruned_store._id_to_idx
    for node_id in FRAGMENT_NODES:
        assert node_id not in pruned_store._id_to_idx


def test_pruning_keeps_every_edge_array_aligned(pruned_store):
    n_edges = pruned_store._graph.ecount()
    assert len(pruned_store._length) == n_edges
    assert len(pruned_store._names) == n_edges
    assert len(pruned_store._tree_count) == n_edges
    assert len(pruned_store._tree_deciduous) == n_edges
    assert len(pruned_store._tree_evergreen) == n_edges
    assert len(pruned_store._coord_offsets) == n_edges + 1

    # Look each kept edge up by name and check its companion values --
    # order-independent, so this fails on misalignment, not on reordering.
    expected = {name: (length_m, count) for _, _, name, length_m, count in MAIN_EDGES}
    assert sorted(pruned_store._names) == sorted(expected)
    for i, name in enumerate(pruned_store._names):
        length_m, count = expected[name]
        assert pruned_store._length[i] == pytest.approx(length_m)
        assert pruned_store._tree_count[i] == count


def test_pruning_keeps_packed_geometry_aligned_with_endpoints(pruned_store):
    # Same invariant the packed-geometry test pins for real tiles: every
    # edge's coordinate slice must start and end at its own two endpoint
    # nodes (in either order) -- here it proves reindexing didn't shift
    # the coordinate buffer relative to the edge list.
    for e in range(pruned_store._graph.ecount()):
        u, v = pruned_store._graph.es[e].tuple
        coords = pruned_store._edge_coords(e)
        endpoints = {tuple(pruned_store._node_lonlat[u]), tuple(pruned_store._node_lonlat[v])}
        assert {tuple(coords[0]), tuple(coords[-1])} == endpoints


def test_coverage_shrinks_to_the_kept_component(pruned_store):
    # The served coverage area must not advertise the pruned fragment --
    # that's the user-facing point of pruning (a click there should be
    # rejected as out-of-coverage, not routed around a foreign island).
    lon_min, lat_min, lon_max, lat_max = pruned_store._bounds
    f_lon, f_lat = FRAGMENT_NODES["f1"]
    assert not (lon_min <= f_lon <= lon_max and lat_min <= f_lat <= lat_max)

    # Same promise for the drawn boundary ring /coverage serves.
    from shapely.geometry import Point, Polygon

    ring = pruned_store.coverage_ring()
    assert ring[0] == ring[-1]
    assert not Polygon(ring).contains(Point(f_lon, f_lat))


def test_routing_still_works_on_the_kept_component(pruned_store):
    lon_a, lat_a = MAIN_NODES["m1"]
    lon_b, lat_b = MAIN_NODES["m3"]
    start = pruned_store.snap_to_edge(lat_a, lon_a)
    end = pruned_store.snap_to_edge(lat_b, lon_b)
    result = pruned_store.route(start, end, tree_weight=0, month=7)
    assert result is not None
    assert result["length_m"] > 0
