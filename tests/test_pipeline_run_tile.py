"""Wiring test for pipeline/run_tile.py's fetch stage.

This exists because of a bug where every individual function was correct
and tested, but the wiring between them was dead: FETCH_BUFFER_M sat in
config from the first commit while nothing referenced it, so tiles were
fetched at their exact edges, shared no border nodes, and the server's
load()-time merge had nothing to stitch on -- 89 disconnected components
across Brooklyn. Unit tests on buffered_bbox() can never catch that class
of regression; only a test that run() actually *calls* the fetchers with
a padded bbox can.
"""

import networkx as nx

from pipeline import config, run_tile


def test_run_fetches_with_a_bbox_padded_past_the_tiles_edges(monkeypatch):
    tile_bbox = config.get_tile_bbox("pilot")
    received = {}

    def fake_fetch_streets(bbox, tile_id):
        received["streets"] = bbox
        # A real (trivial) graph, not a bare sentinel: run() now calls
        # .number_of_nodes() on whatever clip_to_nyc hands back (mocked to
        # identity below), and a non-empty graph also keeps run() past the
        # open-water early return.
        graph = nx.MultiDiGraph()
        graph.add_node(1, x=0.0, y=0.0)
        return graph

    def fake_fetch_trees(bbox, tile_id, refresh=False):
        received["trees"] = bbox
        return object()

    monkeypatch.setattr(run_tile.streets, "fetch_streets", fake_fetch_streets)
    monkeypatch.setattr(run_tile.trees, "fetch_trees", fake_fetch_trees)
    # The compute stages aren't under test -- stub them out so the fakes'
    # sentinel returns never reach real geometry code. Boundary clipping is
    # also a no-op here: it isn't this test's concern (see
    # test_pipeline_graph_boundary.py), and it would otherwise choke on the
    # plain object() sentinel fake_fetch_streets returns (no real .nodes())
    # or hit the real disk-cached borough-boundary fetch.
    monkeypatch.setattr(run_tile.boundaries, "fetch_borough_boundaries", lambda: {})
    monkeypatch.setattr(run_tile.boundary, "nyc_boundary", lambda geojson: None)
    monkeypatch.setattr(run_tile.boundary, "clip_to_nyc", lambda graph, nyc_shape: graph)
    monkeypatch.setattr(run_tile.centerline, "build_edge_table", lambda graph: (None, None))
    monkeypatch.setattr(run_tile.tree_scoring, "score_and_join", lambda edges, rows: None)
    monkeypatch.setattr(run_tile.export, "write_tile", lambda tile_id, nodes, edges: None)

    run_tile.run("pilot")

    # Strictly wider than the tile on all four sides -- the invariant is
    # "adjacent tiles' fetched data genuinely overlaps," not any particular
    # buffer size, so a deliberate change to FETCH_BUFFER_M shouldn't fail
    # this test but dropping the padding entirely must.
    fetched = received["streets"]
    assert fetched.lat_min < tile_bbox.lat_min
    assert fetched.lat_max > tile_bbox.lat_max
    assert fetched.lon_min < tile_bbox.lon_min
    assert fetched.lon_max > tile_bbox.lon_max

    # Trees must cover the identical padded area, not the bare tile: a tree
    # just past a tile's true edge still shades edges inside it, and the
    # exported tile spans the whole padded area.
    assert received["trees"] == fetched


def test_run_removes_a_stale_export_when_boundary_clipping_empties_the_tile(monkeypatch, tmp_path):
    # A tile that had real data on a previous run (old rectangle-overreach
    # pipeline, before clip_to_nyc existed) can legitimately clip down to
    # zero nodes on a later run -- entirely foreign territory. Without
    # cleanup, GraphStore.load() would keep merging in that stale export
    # forever, since it merges every *.json.gz file it finds regardless of
    # age. Real case that surfaced this: r9c8/r12c8/r13c8/r13c9 during the
    # Brooklyn re-run after the water-included boundary dataset landed.
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    stale_path = tmp_path / "r9c8.json.gz"
    stale_path.write_bytes(b"stale export from before boundary clipping existed")

    graph = nx.MultiDiGraph()
    graph.add_node(1, x=0.0, y=0.0)
    monkeypatch.setattr(run_tile.streets, "fetch_streets", lambda bbox, tile_id: graph)
    monkeypatch.setattr(run_tile.boundaries, "fetch_borough_boundaries", lambda: {})
    monkeypatch.setattr(run_tile.boundary, "nyc_boundary", lambda geojson: None)
    # Empties the tile entirely -- the exact condition that used to leave
    # a stale export behind.
    monkeypatch.setattr(run_tile.boundary, "clip_to_nyc", lambda graph, nyc_shape: nx.MultiDiGraph())

    run_tile.run("r9c8")

    assert not stale_path.exists()
