"""Every loaded component is visible, and reachability is what protects a
click -- not a size heuristic.

This file used to pin the opposite. The hide rule (2026-08-17) excluded any
disconnected component under 5km from click-snapping and the drawn coverage,
on the theory that such components were orphaned scraps that could only trap
a click. Measured against the sidewalk model on 2026-08-23, that theory did
not hold: the rule suppressed 674.3km across 5,128 components, including 124
networks of 1-5km made of named residential streets -- 217th Street in
Queens, Baychester Avenue in the Bronx, Arthur Kill Road on Staten Island.
Clicking your own block there returned "outside our current coverage area",
which was false: we had the street and declined to serve it. It also sat
badly with the governing rule, overriding OSM's own answer with a size bar.

So the protection has to come from somewhere else, and it already did:
snap_pair() requires a component reachable from BOTH endpoints. That is a
reachability test rather than a size test, so it cannot be wrong about which
places are real -- it only ever answers "can these two points reach each
other", which is the actual question.

What is pinned here:

  - a small isolated fragment IS snappable and accepted (the inversion);
  - a big isolated place stays snappable and accepted (unchanged);
  - the only component stays clickable however small (unchanged).

The drawn-coverage half of these guarantees -- and the ring cache +
fingerprint machinery that existed to serve it -- went with the coverage
outline (deleted 2026-09-03): the rings were computed only to be drawn,
never for routing, so in_coverage/snap_pair are the whole surviving
contract.

There is deliberately NO test here asserting "prefer the larger component
when both are in range". That was written and then deleted on 2026-08-23:
it is a size rule, which is the thing this change removed, and it would
have reintroduced it through the test suite. It was also chasing a defect
that turned out to be elsewhere -- the one bad route found while measuring
this (a 149m request answered with 12m) reproduces identically on the main
citywide component, so it is a snap-distance issue, not a component-choice
one, and it predates this change.

Synthetic fixtures, same pattern as test_graph_store_pruning.py.
"""

import gzip
import json

from pipeline import config
from server.graph_store import GraphStore

MAIN_NODES = {
    "m1": [-73.9900, 40.6800],
    "m2": [-73.9880, 40.6800],
    "m3": [-73.9880, 40.6820],
}
MAIN_EDGES = [
    ("m1", "m2", "Alpha Street", 2700.0, 3),
    ("m2", "m3", "Beta Avenue", 2600.0, 5),
]

# A genuinely isolated fragment ~2.5km away -- well past MAX_SNAP_DISTANCE_M,
# so nothing else is in range of it and it stays its own component.
FRAGMENT_NODES = {
    "f1": [-74.0170, 40.6890],
    "f2": [-74.0150, 40.6890],
}
FRAGMENT_EDGES = [("f1", "f2", "Orphan Path", 165.0, 9)]


def _edge_record(u, v, name, length_m, tree_count, nodes):
    return {
        "u": u, "v": v, "key": 0, "side": "C",
        "length_m": length_m, "name": name,
        "tree_deciduous": float(tree_count), "tree_evergreen": 0.0,
        "tree_count": tree_count,
        "coords": [nodes[u], nodes[v]],
    }


def _write_tile(path, nodes, edges):
    tile = {
        "nodes": nodes,
        "edges": [_edge_record(u, v, name, length_m, count, nodes)
                  for u, v, name, length_m, count in edges],
    }
    with gzip.open(path, "wt") as f:
        json.dump(tile, f)


def _store(tmp_path, monkeypatch, fragment_edges=FRAGMENT_EDGES) -> GraphStore:
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "fragment.json.gz", FRAGMENT_NODES, fragment_edges)
    store = GraphStore()
    store.load()
    return store


# ── Small components are real places, not scraps to suppress ─────────────

def test_small_isolated_fragment_is_snappable(tmp_path, monkeypatch):
    """The inversion. 165m of disconnected pavement is somewhere a person
    can genuinely walk, so two clicks on it get the route it can honestly
    serve rather than a coverage rejection.
    """
    store = _store(tmp_path, monkeypatch)
    f1_lat, f1_lon = FRAGMENT_NODES["f1"][1], FRAGMENT_NODES["f1"][0]
    f2_lat, f2_lon = FRAGMENT_NODES["f2"][1], FRAGMENT_NODES["f2"][0]
    assert store.snap_pair(f1_lat, f1_lon, f2_lat, f2_lon) is not None
    assert store.in_coverage(f1_lat, f1_lon) is True


def test_a_big_isolated_place_stays_snappable(tmp_path, monkeypatch):
    """The Governors Island class -- always worked, must keep working."""
    big = [("f1", "f2", "Island Loop", 6000.0, 9)]
    store = _store(tmp_path, monkeypatch, fragment_edges=big)
    f1_lat, f1_lon = FRAGMENT_NODES["f1"][1], FRAGMENT_NODES["f1"][0]
    f2_lat, f2_lon = FRAGMENT_NODES["f2"][1], FRAGMENT_NODES["f2"][0]
    assert store.snap_pair(f1_lat, f1_lon, f2_lat, f2_lon) is not None
    assert store.in_coverage(f1_lat, f1_lon) is True


def test_the_only_component_is_clickable_however_small(tmp_path, monkeypatch):
    """A toy dataset or a sliver tile must stay clickable."""
    tiny = [("m1", "m2", "Alpha Street", 170.0, 3),
            ("m2", "m3", "Beta Avenue", 220.0, 5)]
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, tiny)
    store = GraphStore()
    store.load()
    m1_lon, m1_lat = MAIN_NODES["m1"]
    m3_lon, m3_lat = MAIN_NODES["m3"]
    assert store.snap_pair(m1_lat, m1_lon, m3_lat, m3_lon) is not None
    assert store.in_coverage(m1_lat, m1_lon) is True
