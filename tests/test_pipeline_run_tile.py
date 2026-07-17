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

from pipeline import config, run_tile


def test_run_fetches_with_a_bbox_padded_past_the_tiles_edges(monkeypatch):
    tile_bbox = config.get_tile_bbox("pilot")
    received = {}

    def fake_fetch_streets(bbox, tile_id):
        received["streets"] = bbox
        return object()  # non-None, so run() doesn't take the open-water early return

    def fake_fetch_trees(bbox, tile_id, refresh=False):
        received["trees"] = bbox
        return object()

    monkeypatch.setattr(run_tile.streets, "fetch_streets", fake_fetch_streets)
    monkeypatch.setattr(run_tile.trees, "fetch_trees", fake_fetch_trees)
    # The compute stages aren't under test -- stub them out so the fakes'
    # sentinel returns never reach real geometry code.
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
