"""Tests for graph_store.py's hide rule (FIXES item 1, the scraps arc):
disconnected components under HIDDEN_COMPONENT_MAX_LEN_M are excluded
from click-snapping and the drawn coverage, because the 2026-08-15/16
citywide cause audit showed they're orphaned fragments (sidewalk orphans,
ghost slivers, policy-excluded meshes) that can only trap a click or
advertise coverage routing can't honor. Guarantees pinned here:

  - a small isolated fragment can't be snapped onto and isn't drawn;
  - the LARGEST component is always visible, however small (toy datasets,
    sliver tiles);
  - a big isolated place (the Governors Island class) stays visible;
  - KEEP_VISIBLE_ISOLATED_PLACES overrides hiding by coordinate
    (Liberty Island class -- user-reviewed 2026-08-16/17);
  - the rule's parameters are part of the coverage-cache fingerprint, so
    editing them can never serve rings computed under the old rule.

Synthetic two-tile fixtures, same pattern as test_graph_store_pruning.py.
"""

import gzip
import json

import pytest
from shapely.geometry import Point, Polygon

from pipeline import config
from server import graph_store
from server.graph_store import GraphStore

MAIN_NODES = {
    "m1": [-73.9900, 40.6800],
    "m2": [-73.9880, 40.6800],
    "m3": [-73.9880, 40.6820],
}
# Total length clears HIDDEN_COMPONENT_MAX_LEN_M, like the real network
# does -- so the fragment fixtures below test the rule, not an artifact
# of a toy main network itself falling under the bar.
MAIN_EDGES = [
    ("m1", "m2", "Alpha Street", 2700.0, 3),
    ("m2", "m3", "Beta Avenue", 2600.0, 5),
]

# A genuinely isolated fragment ~2.5km away (well past MAX_SNAP_DISTANCE_M,
# so hiding it leaves NOTHING snappable near it), under the 5km bar.
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
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "fragment.json.gz", FRAGMENT_NODES, fragment_edges)
    store = GraphStore()
    store.load()
    return store


def test_small_isolated_fragment_is_not_snappable(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    f1_lon, f1_lat = FRAGMENT_NODES["f1"]
    f2_lon, f2_lat = FRAGMENT_NODES["f2"]
    # before the rule, two clicks on the fragment routed within it -- the
    # click-trap this rule exists to remove
    assert store.snap_pair(f1_lat, f1_lon, f2_lat, f2_lon) is None
    assert store.in_coverage(f1_lat, f1_lon) is False


def test_hidden_fragment_is_not_drawn_in_coverage(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    rings = store.coverage_rings()
    assert len(rings) == 1  # main network only; the fragment gets no ring
    f1_lon, f1_lat = FRAGMENT_NODES["f1"]
    assert not Polygon(rings[0]).contains(Point(f1_lon, f1_lat))


def test_big_isolated_place_stays_visible(tmp_path, monkeypatch):
    # the Governors Island class: disconnected but over the bar
    big = [("f1", "f2", "Island Loop", 6000.0, 9)]
    store = _store(tmp_path, monkeypatch, fragment_edges=big)
    f1_lon, f1_lat = FRAGMENT_NODES["f1"]
    f2_lon, f2_lat = FRAGMENT_NODES["f2"]
    assert store.snap_pair(f1_lat, f1_lon, f2_lat, f2_lon) is not None
    assert store.in_coverage(f1_lat, f1_lon) is True
    assert len(store.coverage_rings()) == 2


def test_largest_component_is_always_visible_however_small(tmp_path, monkeypatch):
    # a toy dataset whose whole network is under the bar must keep working
    tiny_edges = [
        ("m1", "m2", "Alpha Street", 170.0, 3),
        ("m2", "m3", "Beta Avenue", 220.0, 5),
    ]
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, tiny_edges)
    store = GraphStore()
    store.load()
    m1_lon, m1_lat = MAIN_NODES["m1"]
    m3_lon, m3_lat = MAIN_NODES["m3"]
    assert store.snap_pair(m1_lat, m1_lon, m3_lat, m3_lon) is not None
    assert store.in_coverage(m1_lat, m1_lon) is True


def test_keep_visible_list_overrides_hiding(tmp_path, monkeypatch):
    f1_lon, f1_lat = FRAGMENT_NODES["f1"]
    monkeypatch.setattr(
        graph_store, "KEEP_VISIBLE_ISOLATED_PLACES",
        [(f1_lat, f1_lon, "Test Island")],
    )
    store = _store(tmp_path, monkeypatch)
    f2_lon, f2_lat = FRAGMENT_NODES["f2"]
    assert store.snap_pair(f1_lat, f1_lon, f2_lat, f2_lon) is not None
    assert store.in_coverage(f1_lat, f1_lon) is True
    assert len(store.coverage_rings()) == 2


def test_keep_visible_entry_missing_from_dataset_is_ignored(tmp_path, monkeypatch):
    # partial datasets (the pilot fixture, single-tile loads) don't contain
    # every keep-listed place -- the entry must be skipped, not crash or
    # accidentally unhide something else
    monkeypatch.setattr(
        graph_store, "KEEP_VISIBLE_ISOLATED_PLACES",
        [(40.690830, -74.045350, "Liberty Island")],  # nowhere near this fixture
    )
    store = _store(tmp_path, monkeypatch)
    f1_lon, f1_lat = FRAGMENT_NODES["f1"]
    assert store.in_coverage(f1_lat, f1_lon) is False


def test_rule_parameters_are_part_of_the_coverage_fingerprint(tmp_path, monkeypatch):
    # the stale-cache trap FIXES item 1 warned about: editing the rule
    # must invalidate cached rings exactly like a re-exported tile does
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    paths = sorted(tmp_path.glob("*.json.gz"))
    before = graph_store._tiles_fingerprint(paths)
    monkeypatch.setattr(
        graph_store, "KEEP_VISIBLE_ISOLATED_PLACES",
        [(1.0, 2.0, "Somewhere New")],
    )
    assert graph_store._tiles_fingerprint(paths) != before
    monkeypatch.setattr(graph_store, "HIDDEN_COMPONENT_MAX_LEN_M", 123.0)
    changed_again = graph_store._tiles_fingerprint(paths)
    assert changed_again != before
