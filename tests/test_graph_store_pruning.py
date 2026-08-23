"""Multi-component loading, exercised deterministically.

server/graph_store.py's load() used to prune everything but the largest
connected component. As of the borough-boundary polygon work it keeps
every component instead, trusting pipeline/graph/boundary.py's
clip_to_nyc() to have already excluded non-NYC territory before data ever
reaches data/tiles/ -- so a real disconnected place (Governors Island,
eventually Staten Island) is no longer collateral damage. The pilot
fixture the suite runs against (tests/fixtures/pilot.json.gz) is
single-component, so this path never actually runs on the suite's own
data. These tests fabricate a two-component dataset (a small
main network plus a genuinely disconnected "island", split over two tile
files the way a real border tile would be) so it's exercised on every
run, and its output -- both components present, arrays still aligned,
routing behaving correctly on and across them -- is checked directly.
"""

import gzip
import json

import pytest
from shapely.geometry import LinearRing, Point, Polygon

from pipeline import config
from server.graph_store import GraphStore

MAIN_NODES = {
    "m1": [-73.9900, 40.6800],
    "m2": [-73.9880, 40.6800],
    "m3": [-73.9880, 40.6820],
    "m4": [-73.9900, 40.6820],
}
# ~2.5km from the main cluster -- well past twice MAX_SNAP_DISTANCE_M
# (200m), so the two components' buffered coverage footprints stay
# genuinely disjoint rather than merging into one piece.
ISLAND_NODES = {
    "i1": [-74.0170, 40.6890],
    "i2": [-74.0150, 40.6890],
}

# (u, v, name, length_m, tree_count) -- distinct values per edge on purpose,
# so the alignment tests can prove each attribute stayed with its own edge.
# The lengths no longer have to clear any bar: they were sized to stay over
# the hide rule's 5km threshold, which was deleted 2026-08-23 (see
# test_graph_store_components.py). Left as they are because nothing here
# depends on them, and rewriting them would churn the alignment fixtures.
MAIN_EDGES = [
    ("m1", "m2", "Alpha Street", 1700.0, 3),
    ("m2", "m3", "Beta Avenue", 2200.0, 5),
    ("m3", "m4", "Gamma Road", 1300.0, 0),
]
# Stands in for a REAL disconnected place (Governors Island, 49km).
ISLAND_EDGES = [("i1", "i2", "Island Path", 6000.0, 9)]

# A tiny, unnamed, disconnected fragment sitting ~7m from m1 -- stands in
# for a real orphaned pedestrian crossing or plaza-interior path (see
# PLAN.md): not part of the street grid at all, but close enough to
# occasionally win a naive "nearest edge, regardless of reachability"
# comparison over the real street a few meters further out.
JUNK_NODES = {
    "j1": [-73.98995, 40.68005],
    "j2": [-73.98985, 40.68005],
}
JUNK_EDGES = [("j1", "j2", "", 8.0, 0)]


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
def multi_component_store(tmp_path, monkeypatch) -> GraphStore:
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", ISLAND_NODES, ISLAND_EDGES)
    store = GraphStore()
    store.load()
    return store


def test_load_keeps_every_component(multi_component_store):
    assert multi_component_store._graph.vcount() == len(MAIN_NODES) + len(ISLAND_NODES)
    assert multi_component_store._graph.ecount() == len(MAIN_EDGES) + len(ISLAND_EDGES)
    for node_id in {**MAIN_NODES, **ISLAND_NODES}:
        assert node_id in multi_component_store._id_to_idx

    components = multi_component_store._graph.connected_components(mode="weak")
    assert len(components) == 2


def test_load_keeps_every_edge_array_aligned(multi_component_store):
    n_edges = multi_component_store._graph.ecount()
    assert len(multi_component_store._length) == n_edges
    assert len(multi_component_store._names) == n_edges
    assert len(multi_component_store._tree_count) == n_edges
    assert len(multi_component_store._tree_deciduous) == n_edges
    assert len(multi_component_store._tree_evergreen) == n_edges
    assert len(multi_component_store._coord_offsets) == n_edges + 1

    # Look each edge up by name and check its companion values --
    # order-independent, so this fails on misalignment, not on ordering.
    expected = {name: (length_m, count) for _, _, name, length_m, count in MAIN_EDGES + ISLAND_EDGES}
    assert sorted(multi_component_store._names) == sorted(expected)
    for i, name in enumerate(multi_component_store._names):
        length_m, count = expected[name]
        assert multi_component_store._length[i] == pytest.approx(length_m)
        assert multi_component_store._tree_count[i] == count


def test_load_keeps_packed_geometry_aligned_with_endpoints(multi_component_store):
    # Same invariant the packed-geometry test pins for real tiles: every
    # edge's coordinate slice must start and end at its own two endpoint
    # nodes (in either order) -- across both components, with nothing
    # reindexed or dropped.
    for e in range(multi_component_store._graph.ecount()):
        u, v = multi_component_store._graph.es[e].tuple
        coords = multi_component_store._edge_coords(e)
        endpoints = {tuple(multi_component_store._node_lonlat[u]), tuple(multi_component_store._node_lonlat[v])}
        assert {tuple(coords[0]), tuple(coords[-1])} == endpoints


def test_coverage_includes_both_components_as_separate_pieces(multi_component_store):
    # The user-facing point of relaxing pruning: a genuinely disconnected
    # real place (this fixture stands in for Governors Island) gets its
    # own visible boundary now, instead of being silently left off the map
    # while still being fully routable.
    rings = multi_component_store.coverage_rings()
    assert len(rings) == 2

    m1_lon, m1_lat = MAIN_NODES["m1"]
    i1_lon, i1_lat = ISLAND_NODES["i1"]
    polys = [Polygon(ring) for ring in rings]
    assert any(poly.contains(Point(m1_lon, m1_lat)) for poly in polys)
    assert any(poly.contains(Point(i1_lon, i1_lat)) for poly in polys)

    # Each piece is closed and CCW -- the frontend's hole-punch contract
    # (MapView reverses each ring to cut a hole in the dimming mask).
    for ring in rings:
        assert ring[0] == ring[-1]
        assert LinearRing(ring).is_ccw


def test_routing_works_within_each_component(multi_component_store):
    lon_a, lat_a = MAIN_NODES["m1"]
    lon_b, lat_b = MAIN_NODES["m3"]
    start, end = multi_component_store.snap_pair(lat_a, lon_a, lat_b, lon_b)
    result = multi_component_store.route(start, end, tree_weight=0, month=7)
    assert result is not None
    assert result["length_m"] > 0


def test_snap_pair_returns_none_between_disconnected_components(multi_component_store):
    # The other half of relaxed pruning's contract: two real, in-coverage
    # points that legitimately can't reach each other (mainland <-> a
    # ferry-only island) must get a clean "no route" -- and without ever
    # calling route()'s Dijkstra at all, since no shared component within
    # snap range means the answer is already known.
    lon_a, lat_a = MAIN_NODES["m1"]
    lon_b, lat_b = ISLAND_NODES["i1"]
    assert multi_component_store.snap_pair(lat_a, lon_a, lat_b, lon_b) is None


@pytest.mark.filterwarnings("ignore:Couldn't reach some vertices:RuntimeWarning")
def test_route_still_refuses_a_cross_component_pair_directly(multi_component_store):
    # snap_pair() is what keeps the real /route flow from ever calling
    # route() with mismatched points (see the test above) -- this pins
    # that route() also refuses safely on its own, as a defense-in-depth
    # safety net for any other caller. igraph warns here by design;
    # pytest resets warning filters per test, so this expected warning
    # gets its own marker instead of relying on graph_store.py's
    # module-scope filter.
    lon_a, lat_a = MAIN_NODES["m1"]
    lon_b, lat_b = ISLAND_NODES["i1"]
    edge_a, _ = multi_component_store._nearest_edge(lat_a, lon_a)
    edge_b, _ = multi_component_store._nearest_edge(lat_b, lon_b)
    start = multi_component_store._snap_point_for_edge(lat_a, lon_a, edge_a)
    end = multi_component_store._snap_point_for_edge(lat_b, lon_b, edge_b)
    result = multi_component_store.route(start, end, tree_weight=0, month=7)
    assert result is None


@pytest.fixture()
def store_with_a_disconnected_fragment_near_a_real_street(tmp_path, monkeypatch) -> GraphStore:
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "junk.json.gz", JUNK_NODES, JUNK_EDGES)
    store = GraphStore()
    store.load()
    return store


def test_snap_pair_ignores_a_disconnected_fragment_closer_than_the_real_street(
    store_with_a_disconnected_fragment_near_a_real_street,
):
    # The real bug this fixture reproduces (Union Square, a Brooklyn
    # Bridge landing -- see PLAN.md): a click right on the junk fragment
    # (j1) is *closer* to it than to the real street (m1-m2, ~7m away).
    # Naive nearest-edge snapping picked the fragment, which can't reach
    # anywhere else -- routing to a distant real point (m3) failed even
    # though a real, reachable street sits a few meters further out.
    store = store_with_a_disconnected_fragment_near_a_real_street
    lon_junk, lat_junk = JUNK_NODES["j1"]
    lon_m3, lat_m3 = MAIN_NODES["m3"]

    pair = store.snap_pair(lat_junk, lon_junk, lat_m3, lon_m3)
    assert pair is not None
    start, end = pair
    # Landed on a real, named street -- not the unnamed junk fragment.
    assert store._names[start.edge] != ""

    result = store.route(start, end, tree_weight=0, month=7)
    assert result is not None
    assert result["length_m"] > 0
