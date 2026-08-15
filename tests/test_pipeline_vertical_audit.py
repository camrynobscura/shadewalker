"""pipeline/graph/vertical_audit.py -- the report-only vertical-suspects
detector (FIXES item 0). Each test fabricates the smallest graph that
exhibits one clause of the validated rule; coordinates are real NYC-area
lon/lat so the metric transform behaves like production.

Scale cheat sheet: at this latitude 0.0005 deg of longitude is ~42m and
0.0003 deg of latitude is ~33m.
"""

import json

import networkx as nx
import pytest

from pipeline.graph import vertical_audit

LAT = 40.7
LON = -73.99
DLON = 0.0005   # ~42m
DLAT = 0.0003   # ~33m


def _deck_and_ground_graph(deck_tags=None):
    """An elevated footway (osmid 111, 4 spans of ~42m -- mid nodes sit
    well past TERMINUS_M from either end) with a synthetic imported path
    (~33m south of it) whose loose end is welded onto the deck's middle
    node. Returns (graph, deck_mid_node, loose_end_node)."""
    tags = {"highway": "footway", "bridge": "yes", "layer": "1"}
    if deck_tags is not None:
        tags = deck_tags
    g = nx.MultiDiGraph()
    deck_nodes = [100, 101, 102, 103, 104]
    for i, n in enumerate(deck_nodes):
        g.add_node(n, x=LON + i * DLON, y=LAT)
    for a, b in zip(deck_nodes, deck_nodes[1:]):
        g.add_edge(a, b, osmid=111, length=42.0, **tags)

    # the imported synthetic path, at ground, south of the deck
    g.add_node(-1, x=LON + 2 * DLON, y=LAT - DLAT)
    g.add_node(-2, x=LON + 3 * DLON, y=LAT - DLAT)
    g.add_edge(-1, -2, osmid=-5, length=42.0)

    # the weld the pipeline would have manufactured: loose end -> deck mid
    g.add_edge(-1, 102, osmid=-9, length=33.0, weld=True)
    return g, 102, -1


def test_mid_deck_weld_with_no_real_walk_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, deck_mid, loose_end = _deck_and_ground_graph()
    report = vertical_audit.report_vertical_suspects(g, None, "testtile")
    assert len(report) == 1
    entry = report[0]
    assert entry["u"] == str(loose_end) or entry["v"] == str(loose_end)
    assert entry["way"] == 111
    assert entry["shape"] == "i"
    assert entry["arbiter_walk_m"] is None  # no other way between the levels
    # and the report landed on disk for the review workflow
    written = json.loads((tmp_path / "testtile.json").read_text())
    assert written == report


def test_detector_never_removes_anything(tmp_path, monkeypatch):
    # Report-only is the whole design (see the module docstring): the
    # graph must come back byte-identical, suspects and all.
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, _, _ = _deck_and_ground_graph()
    edges_before = set(g.edges(keys=True))
    nodes_before = set(g.nodes)
    vertical_audit.report_vertical_suspects(g, None, "testtile")
    assert set(g.edges(keys=True)) == edges_before
    assert set(g.nodes) == nodes_before


def test_weld_at_deck_terminus_is_a_landing_not_a_suspect(tmp_path, monkeypatch):
    # Structures come down to grade at their ends -- a weld near the way's
    # terminus is how a real landing connects.
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, _, _ = _deck_and_ground_graph()
    g.remove_edge(-1, 102)
    g.add_edge(-1, 100, osmid=-9, length=90.0, weld=True)  # node 100 = way end
    assert vertical_audit.report_vertical_suspects(g, None, "testtile") == []


def test_sidewalk_layer_arbitration_clears_a_shortcut_weld(tmp_path, monkeypatch):
    # The full sidewalk layer carries the ramps our centerline filters
    # exclude (measured: Kosciuszko Bridge landing). If it walks between
    # the weld's endpoints within the harm bar, the weld merely shortcuts
    # a real connection and must NOT be reported.
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, deck_mid, loose_end = _deck_and_ground_graph()
    sidewalks = nx.MultiDiGraph()
    for n in (deck_mid, loose_end):
        sidewalks.add_node(n, x=g.nodes[n]["x"], y=g.nodes[n]["y"])
    sidewalks.add_edge(deck_mid, loose_end, osmid=333, length=60.0,
                       highway="footway", footway="sidewalk")
    assert vertical_audit.report_vertical_suspects(g, sidewalks, "testtile") == []


def test_non_walkable_elevated_way_is_ignored(tmp_path, monkeypatch):
    # A motorway viaduct overhead is not a snap target and can't be a
    # deck a person walks on -- geometric proximity to it is meaningless
    # (measured: it produced only false alarms in the offline audit).
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, _, _ = _deck_and_ground_graph(
        deck_tags={"highway": "motorway", "bridge": "yes", "layer": "1", "foot": "no"})
    assert vertical_audit.report_vertical_suspects(g, None, "testtile") == []


def test_phantom_mesh_members_cannot_alibi_each_other(tmp_path, monkeypatch):
    # The arbitration excludes ALL suspects jointly: with two welds onto
    # the same deck, neither may pass by routing through the other
    # (measured at the Manhattan Bridge: this alibi is exactly how a
    # one-at-a-time check wrongly clears a phantom mesh).
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, deck_mid, _ = _deck_and_ground_graph()
    # second loose end on the same synthetic path, welded one deck node over
    g.add_edge(-2, 103, osmid=-10, length=33.0, weld=True)
    report = vertical_audit.report_vertical_suspects(g, None, "testtile")
    assert len(report) == 2


def test_no_welds_means_no_work_and_no_report(tmp_path, monkeypatch):
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g, _, loose_end = _deck_and_ground_graph()
    g.remove_edge(loose_end, 102)
    assert vertical_audit.report_vertical_suspects(g, None, "testtile") == []
    assert not (tmp_path / "testtile.json").exists()


def test_a_single_edge_elevated_way_does_not_crash_the_audit(tmp_path, monkeypatch):
    # Real crash on the pilot tile (2026-08-14): a one-edge elevated way's
    # segments union straight to a bare LineString, which shapely's
    # linemerge() refuses. The audit must handle it like any other way.
    monkeypatch.setattr(vertical_audit, "REPORTS_DIR", tmp_path)
    g = nx.MultiDiGraph()
    g.add_node(200, x=LON, y=LAT + 0.01)
    g.add_node(201, x=LON + DLON, y=LAT + 0.01)
    g.add_edge(200, 201, osmid=555, length=42.0,
               highway="footway", bridge="yes", layer="1")
    base, deck_mid, loose_end = _deck_and_ground_graph()
    g = nx.compose(g, base)
    report = vertical_audit.report_vertical_suspects(g, None, "testtile")
    assert len(report) == 1  # the base graph's suspect still found, no crash
