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

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString

from pipeline import config, run_tile


def test_run_fetches_with_a_bbox_padded_past_the_tiles_edges(monkeypatch):
    tile_bbox = config.get_tile_bbox("pilot")
    received = {}

    def fake_fetch_streets(bbox, tile_id, park_reach=None, refresh_raw=False):
        received["streets"] = bbox
        # A real (trivial) graph, not a bare sentinel: run() now calls
        # .number_of_edges() on whatever clip_to_nyc hands back (mocked to
        # identity below), so it needs an actual edge, not just a node, to
        # keep run() past the post-clipping early return.
        graph = nx.MultiDiGraph()
        graph.add_node(1, x=0.0, y=0.0)
        graph.add_node(2, x=0.001, y=0.0)
        graph.add_edge(1, 2)
        return graph

    def fake_fetch_trees(bbox, tile_id, refresh=False):
        received["trees"] = bbox
        return object()

    # An edge table whose real extent deliberately pokes ~500m past the
    # tile's own bbox on one side -- the tree fetch must be sized from
    # THIS, not from the tile's nominal padded bbox (see run()'s comment:
    # simplified street graphs can carry edges well past the fetched
    # area, and scoring them against a nominal-bbox tree fetch produced
    # real zero-tree copies of a 95-tree greenway edge).
    overshoot_lat = tile_bbox.lat_max + 0.0045  # ~500m past the top edge
    fake_edges = gpd.GeoDataFrame(
        {
            "geometry": [
                LineString([
                    (tile_bbox.lon_min, tile_bbox.lat_min),
                    (tile_bbox.lon_min, overshoot_lat),
                ])
            ]
        },
        crs="EPSG:4326",
    )

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
    monkeypatch.setattr(run_tile.centerline, "build_edge_table", lambda graph: (None, fake_edges))
    monkeypatch.setattr(run_tile.tree_scoring, "score_and_join", lambda edges, rows: None)
    # Whether the real (gitignored, manually-downloaded) canopy raster
    # happens to exist on the machine running this test is not this test's
    # concern -- stub it out the same way every other compute stage above
    # is, so the fetch-bbox wiring this test actually checks can't flip
    # pass/fail based on local disk contents.
    monkeypatch.setattr(run_tile.canopy_scoring, "raster_available", lambda: False)
    # Same reasoning one line up, for the same reason: the park-reach shape
    # the street fetch now takes is built from a real (disk-cached, but
    # network-backed on a cold machine) Socrata fetch, which this test has
    # no business triggering -- fake_fetch_streets ignores the value anyway.
    monkeypatch.setattr(run_tile.canopy_scoring, "citywide_park_reach_m", lambda: None)
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

    # Trees must cover the built edges' real extent -- including the
    # deliberate overshoot past the tile's own bbox -- with margin to
    # spare on every side, so no scored edge's corridor can reach past
    # the fetched tree data.
    trees_bbox = received["trees"]
    assert trees_bbox.lat_max > overshoot_lat
    assert trees_bbox.lat_min < tile_bbox.lat_min
    assert trees_bbox.lon_min < tile_bbox.lon_min
    assert trees_bbox.lon_max > tile_bbox.lon_min  # the edge runs along lon_min


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
    # run() builds the park-reach shape before fetching streets, and that
    # walks a real Socrata fetch (disk-cached, but network-backed on a cold
    # machine) -- not this test's concern, and the faked fetch ignores it.
    monkeypatch.setattr(run_tile.canopy_scoring, "citywide_park_reach_m", lambda: None)
    monkeypatch.setattr(run_tile.streets, "fetch_streets", lambda bbox, tile_id, park_reach=None, refresh_raw=False: graph)
    monkeypatch.setattr(run_tile.boundaries, "fetch_borough_boundaries", lambda: {})
    monkeypatch.setattr(run_tile.boundary, "nyc_boundary", lambda geojson: None)
    # Empties the tile entirely -- the exact condition that used to leave
    # a stale export behind.
    monkeypatch.setattr(run_tile.boundary, "clip_to_nyc", lambda graph, nyc_shape: nx.MultiDiGraph())

    run_tile.run("r9c8")

    assert not stale_path.exists()


def test_run_skips_a_tile_left_with_edgeless_nodes_after_boundary_clipping(monkeypatch, tmp_path):
    # Real case: Bronx r22c19 clipped from 985 nodes down to 3 -- each of
    # those 3 survived only because it sits inside NYC itself, but every
    # edge it had led to a node that got dropped, so networkx's
    # cascade-delete on removal left 3 nodes and zero edges, not an empty
    # graph. run() used to check number_of_nodes() == 0 only, so this fell
    # through into centerline.build_edge_table(), which crashes on an
    # edgeless graph (osmnx's to_undirected() raises "Graph contains no
    # edges").
    monkeypatch.setattr(config, "TILES_DIR", tmp_path)
    stale_path = tmp_path / "r22c19.json.gz"
    stale_path.write_bytes(b"stale export from before this edge case was handled")

    graph = nx.MultiDiGraph()
    graph.add_node(1, x=0.0, y=0.0)
    # run() builds the park-reach shape before fetching streets, and that
    # walks a real Socrata fetch (disk-cached, but network-backed on a cold
    # machine) -- not this test's concern, and the faked fetch ignores it.
    monkeypatch.setattr(run_tile.canopy_scoring, "citywide_park_reach_m", lambda: None)
    monkeypatch.setattr(run_tile.streets, "fetch_streets", lambda bbox, tile_id, park_reach=None, refresh_raw=False: graph)
    monkeypatch.setattr(run_tile.boundaries, "fetch_borough_boundaries", lambda: {})
    monkeypatch.setattr(run_tile.boundary, "nyc_boundary", lambda geojson: None)

    edgeless = nx.MultiDiGraph()
    edgeless.add_node(1, x=0.0, y=0.0)
    edgeless.add_node(2, x=0.0, y=0.0)
    edgeless.add_node(3, x=0.0, y=0.0)
    monkeypatch.setattr(run_tile.boundary, "clip_to_nyc", lambda graph, nyc_shape: edgeless)

    run_tile.run("r22c19")

    assert not stale_path.exists()
