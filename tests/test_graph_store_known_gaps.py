"""Tests for graph_store.py's KNOWN_NODE_GAPS patch -- a short, manually
verified list of real OSM node-id pairs that should be reachable from
each other but load as separate components. The original entry (index 0)
is a genuine digitization gap in OSM's own data (see PLAN.md's
Rockaway/Cross Bay Bridge finding); the batch added 2026-07-19 has a
different cause -- pipeline/fetch/streets.py truncating a long way at a
different real vertex in each of two adjacent tiles (see HISTORY.md's
compose-before-simplify-sweep entry) -- but the same server-side fix
applies either way. Exercised here with synthetic two-tile fixtures
reusing real node ids from the list, so this runs in CI without needing
the real data on disk.
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
GAP_NODE_A, GAP_NODE_B, GAP_NAME, _ = graph_store.KNOWN_NODE_GAPS[0]

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


def test_every_known_node_gap_entry_is_well_formed():
    # Cheap structural check on the whole manually-curated list (25
    # entries as of the 2026-07-19 tile-boundary-truncation batch) --
    # catches a typo'd or accidentally-empty entry that the behavioral
    # tests above, which only exercise entry [0], wouldn't.
    seen_pairs = set()
    for node_a, node_b, name, length_m in graph_store.KNOWN_NODE_GAPS:
        assert node_a and node_b and name
        assert node_a != node_b
        # Measured lengths (the 2026-08-13 OSRM-verified batch) must be a
        # sane bridge scale -- anything past 500m shouldn't be a bridge.
        # One deliberate exception: Roosevelt Island Bridge (672m along
        # the real roadway chain) -- the island's ONLY walking access,
        # whose OSM sidewalk chain is mapped on the span but broken
        # mid-chain (a digitization gap, the Cross Bay Bridge class), so
        # the whole crossing needs one long bridge.
        if frozenset((node_a, node_b)) == frozenset(("1242879136", "3785702957")):
            assert length_m == 672.0
        else:
            assert length_m is None or 0.0 < length_m <= 500.0
        pair = frozenset((node_a, node_b))
        assert pair not in seen_pairs, f"duplicate gap entry: {node_a} <-> {node_b}"
        seen_pairs.add(pair)


# A second real entry, distinct from KNOWN_NODE_GAPS[0] -- proves load()
# bridges every listed gap, not just the first one it encounters (a real
# risk given the list grew from 1 entry to 25 in the same change: a
# future refactor that stops after the first match would pass every
# existing test above while silently leaving the other 24 unbridged).
SECOND_GAP_NODE_A, SECOND_GAP_NODE_B, SECOND_GAP_NAME, _ = next(
    entry for entry in graph_store.KNOWN_NODE_GAPS if entry[2] == "Bronx River Greenway"
)

SECOND_MAIN_NODES = {
    "s1": [-73.87, 40.84],
    SECOND_GAP_NODE_B: [-73.868, 40.84],
}
SECOND_MAIN_EDGES = [("s1", SECOND_GAP_NODE_B, "Shore Road", 150.0, 1)]

SECOND_ISLAND_NODES = {
    SECOND_GAP_NODE_A: [-73.90, 40.86],  # ~2.5km away, same isolation margin as above
    "s3": [-73.898, 40.86],
}
SECOND_ISLAND_EDGES = [(SECOND_GAP_NODE_A, "s3", "Greenway Spur", 140.0, 4)]


def test_load_bridges_a_second_known_node_gap_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", SECOND_MAIN_NODES, SECOND_MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", SECOND_ISLAND_NODES, SECOND_ISLAND_EDGES)

    store = GraphStore()
    store.load()

    components = store._graph.connected_components(mode="weak")
    assert len(components) == 1
    assert SECOND_GAP_NAME in store._names


# The 2026-08-01 near-coincident-node sweep found ~14k more real gaps --
# too many to stay a Python list literal the way the curated batch above
# does, so they load from a separate committed JSON file instead. The
# curated batch (with its per-entry provenance comments) stays inline;
# only the bulk-verified batch moves to a file.


def test_known_node_gaps_load_from_an_external_file(tmp_path):
    # Both entry shapes: the 3-element four-signal batch (length comes
    # from the chord at load time) and the 4-element OSRM-verified batch
    # (length measured, stored explicitly).
    data_path = tmp_path / "gaps.json"
    data_path.write_text(json.dumps([
        ["111", "222", "Test Gap"],
        ["333", "444", "Measured Gap", 87.0],
    ]))

    loaded = graph_store._load_known_node_gaps(data_path)

    assert loaded == [
        ("111", "222", "Test Gap", None),
        ("333", "444", "Measured Gap", 87.0),
    ]


def test_load_prints_one_summary_line_not_one_per_gap(tmp_path, monkeypatch, capsys):
    # Regression guard for a real problem the bulk batch would otherwise
    # cause: the old per-entry print (fine at 25 entries) would print
    # ~14,000 lines at every server startup once the bulk batch is loaded.
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", ISLAND_NODES, ISLAND_EDGES)

    store = GraphStore()
    store.load()

    captured = capsys.readouterr()
    gap_lines = [line for line in captured.out.splitlines() if "known node gap" in line]
    assert len(gap_lines) == 1, f"expected one summary line, got {gap_lines!r}"
    assert GAP_NODE_A not in gap_lines[0]
    assert GAP_NODE_B not in gap_lines[0]


def test_bulk_verified_gaps_load_from_file_with_a_plain_label():
    # Bridge names leak into real user-facing route descriptions, so every
    # bulk entry must carry either the plain "unnamed path" fallback (the
    # 2026-08-01 four-signal batch, not individually named) or a real
    # street/path name taken from the gap's own incident edges (the
    # 2026-08-13 OSRM-verified batch) -- never an invented debug string.
    curated_names = {name for _, _, name in graph_store._CURATED_KNOWN_NODE_GAPS}
    assert "unnamed path" not in curated_names

    loaded_from_file = graph_store.KNOWN_NODE_GAPS[len(graph_store._CURATED_KNOWN_NODE_GAPS):]
    assert loaded_from_file, "expected the bulk-verified batch to be present"
    for _, _, name, length_m in loaded_from_file:
        assert name, "a gap bridge must have a displayable name"
        if length_m is None:
            # four-signal batch: anonymous by design
            assert name == "unnamed path"
        else:
            # OSRM-verified batch: real names allowed, debug strings not
            assert "audit" not in name.lower() and "2026" not in name


def test_load_warns_loudly_when_a_mostly_bridged_dataset_has_dead_entries(
    tmp_path, monkeypatch, capsys
):
    # The v18 refetch silently orphaned 11 hand-curated bridges (incl.
    # "Manhattan Bridge Pedestrian Path") and nothing noticed for a day
    # (found 2026-08-14). On a dataset where MOST entries bridge, a
    # dangling entry means a shipped fix has gone dead -- that must be
    # loud and name the entry.
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", ISLAND_NODES, ISLAND_EDGES)
    monkeypatch.setattr(graph_store, "KNOWN_NODE_GAPS", [
        (GAP_NODE_A, GAP_NODE_B, GAP_NAME, None),          # bridges
        ("m1", "i2", "Second Bridge", None),               # bridges
        ("999999991", "999999992", "Ghost Fix", None),     # dead
    ])

    store = GraphStore()
    store.load()

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Ghost Fix" in out
    assert "999999991" in out


def test_load_stays_quiet_about_dangling_entries_on_a_partial_dataset(
    tmp_path, monkeypatch, capsys
):
    # The pilot/CI tile legitimately misses almost every entry -- that's
    # a partial dataset, not dead fixes, and must not scream per entry.
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    _write_tile(tmp_path / "main.json.gz", MAIN_NODES, MAIN_EDGES)
    _write_tile(tmp_path / "island.json.gz", ISLAND_NODES, ISLAND_EDGES)
    monkeypatch.setattr(graph_store, "KNOWN_NODE_GAPS", [
        (GAP_NODE_A, GAP_NODE_B, GAP_NAME, None),          # bridges
        ("999999991", "999999992", "Ghost One", None),     # absent
        ("999999993", "999999994", "Ghost Two", None),     # absent
    ])

    store = GraphStore()
    store.load()

    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "not in this dataset" in out
