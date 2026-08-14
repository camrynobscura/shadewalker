"""Tests for graph_store.py's PHANTOM_CONNECTORS blocklist -- the inverse
of KNOWN_NODE_GAPS: edges the imported-path layers created that provably
do NOT exist as real-world walks (a street node snapped onto a bridge
deck overhead -- see FIXES.md item 0 and the 2026-08-13 reverse-OSRM
audit). Matching is by endpoint COORDINATES, not node ids, because the
phantom edges' synthetic ids renumber on any re-export and an id-keyed
blocklist would go silently stale.
"""

import gzip
import json

import pytest

from pipeline import config
from server import graph_store
from server.graph_store import GraphStore

# A fabricated street corner plus a "bridge deck" node 30m up in 2D --
# same shape as the real DUMBO / Manhattan Bridge phantom.
NODES = {
    "s1": [-73.99000, 40.70400],
    "s2": [-73.98960, 40.70410],  # street corner the phantom attaches to
    "s3": [-73.98920, 40.70420],
    "deck:-1": [-73.98955, 40.70435],  # synthetic path node on the deck
    "deck:-2": [-73.98900, 40.70470],
}


def _edge(u, v, name, length_m):
    return {
        "u": u, "v": v, "key": 0, "side": "C",
        "length_m": length_m, "name": name,
        "tree_deciduous": 0.0, "tree_evergreen": 0.0, "tree_count": 0,
        "coords": [NODES[u], NODES[v]],
    }


EDGES = [
    _edge("s1", "s2", "Street", 40.0),
    _edge("s2", "s3", "Street", 40.0),
    _edge("s2", "deck:-1", "", 30.0),  # the phantom connector
    _edge("deck:-1", "deck:-2", "", 55.0),  # the deck path itself
]


@pytest.fixture()
def tiles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    with gzip.open(tmp_path / "t.json.gz", "wt") as f:
        json.dump({"nodes": NODES, "edges": EDGES}, f)
    return tmp_path


def test_a_blocklisted_connector_never_enters_the_graph(tiles_dir, monkeypatch):
    monkeypatch.setattr(graph_store, "PHANTOM_CONNECTORS", [
        (NODES["s2"], NODES["deck:-1"], "test phantom"),
    ])
    store = GraphStore()
    store.load()

    # 3 of the 4 edges survive; the street grid and the deck path are now
    # honestly separate components.
    assert store._graph.ecount() == 3
    components = store._graph.connected_components(mode="weak")
    assert len(components) == 2


def test_blocklist_matches_either_endpoint_orientation(tiles_dir, monkeypatch):
    monkeypatch.setattr(graph_store, "PHANTOM_CONNECTORS", [
        (NODES["deck:-1"], NODES["s2"], "test phantom, reversed"),
    ])
    store = GraphStore()
    store.load()
    assert store._graph.ecount() == 3


def test_without_blocklist_entries_nothing_is_skipped(tiles_dir, monkeypatch):
    monkeypatch.setattr(graph_store, "PHANTOM_CONNECTORS", [])
    store = GraphStore()
    store.load()
    assert store._graph.ecount() == 4
    assert len(store._graph.connected_components(mode="weak")) == 1


def test_nearby_but_different_edges_do_not_match(tiles_dir, monkeypatch):
    # An entry ~30m away from any real edge endpoint must not fire -- the
    # 2m tolerance is for coordinate rounding, not neighborhood matching.
    monkeypatch.setattr(graph_store, "PHANTOM_CONNECTORS", [
        ([-73.98930, 40.70412], NODES["deck:-1"], "close but not this edge"),
    ])
    store = GraphStore()
    store.load()
    assert store._graph.ecount() == 4


def test_every_real_blocklist_entry_is_well_formed():
    for point_a, point_b, note in graph_store.PHANTOM_CONNECTORS:
        assert note, "each phantom entry needs a note saying what it is"
        for lon, lat in (point_a, point_b):
            # NYC bounds -- catches swapped lat/lon, the classic mistake
            assert -74.5 < lon < -73.5, f"lon out of NYC range: {lon}"
            assert 40.4 < lat < 41.0, f"lat out of NYC range: {lat}"
