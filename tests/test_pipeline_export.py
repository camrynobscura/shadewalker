"""Tests for pipeline/export.py's write_tile() -- the contract between the
pipeline and the routing server.

The module's own docstring flags a hard-won rule: everything written must be
lat/lon degrees (EPSG:4326), never the meter-based geometry_m -- that exact
bug shipped once in the v1 prototype. The main test below pins it directly by
giving write_tile() an edge with *both* geometry columns present (as real
edges have) and asserting the export used the degrees one.
"""

import gzip
import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from pipeline import config, export


def test_write_tile_exports_lon_lat_degrees_not_utm_meters(tmp_path, monkeypatch):
    # write_tile()'s own log line does out_path.relative_to(config.REPO_ROOT),
    # so REPO_ROOT needs to be patched alongside TILES_DIR -- otherwise a
    # tmp_path outside the real repo makes that relative_to() raise.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")

    nodes = gpd.GeoDataFrame({"x": [-73.99, -73.989], "y": [40.68, 40.681]}, index=[1, 2])
    edges = gpd.GeoDataFrame(
        {
            "length_m": [150.0],
            "name": ["Test St"],
            "tree_deciduous": [2.5],
            "tree_evergreen": [0.0],
            "tree_count": [3],
            "tree_park_canopy": [1.25],
            "geometry": [LineString([(-73.99, 40.68), (-73.989, 40.681)])],
            # A real edge also carries geometry_m -- present here on purpose,
            # so this test would catch write_tile() ever reading the wrong
            # one of the two columns.
            "geometry_m": [LineString([(585435.9, 4503837.4), (585352.7, 4503950.1)])],
        },
        index=pd.MultiIndex.from_tuples([(1, 2, 0)], names=["u", "v", "key"]),
        geometry="geometry",
    )

    export.write_tile("test_tile", nodes, edges)

    out_path = tmp_path / "tiles" / "test_tile.json.gz"
    assert out_path.exists()
    with gzip.open(out_path, "rt") as f:
        tile = json.load(f)

    assert tile["meta"]["tile_id"] == "test_tile"
    assert tile["meta"]["crs"] == "EPSG:4326"
    assert tile["meta"]["coord_order"] == "lon,lat"
    assert tile["meta"]["node_count"] == 2
    assert tile["meta"]["edge_count"] == 1

    assert tile["nodes"]["1"] == [-73.99, 40.68]

    edge = tile["edges"][0]
    assert edge["name"] == "Test St"
    assert edge["tree_count"] == 3
    # v19: the park-canopy slice of tree_deciduous rides along explicitly
    # (FIXES item 4 -- "which shade is canopy area, not countable trees")
    assert edge["tree_park_canopy"] == 1.25
    for lon, lat in edge["coords"]:
        # lon/lat degrees, not UTM 18N meters (which would be in the
        # hundreds of thousands) -- this is what would fail if export.py
        # ever read geometry_m instead of geometry.
        assert -75 < lon < -73
        assert 40 < lat < 41


def test_write_tile_namespaces_synthetic_node_ids_per_tile(tmp_path, monkeypatch):
    """Synthetic (negative) node ids must come out namespaced with the tile
    id -- "-1" -> "test_tile:-1" -- on BOTH the nodes dict and edge u/v,
    while real OSM ids (positive) stay exactly as they were.

    Why: every tile numbers its synthetic nodes (interior sidewalks 1a,
    park trails 1g) from -1 independently, and graph_store.load() merges
    all tiles on the id string, first-tile-wins -- so un-namespaced ids
    made one tile's "-1" swallow every other tile's "-1", wiring 94.5k
    edges to nodes in the wrong borough (routes teleporting up to 49km;
    see FIXES.md). Namespacing at export makes cross-tile collision
    structurally impossible.

    The negative check must survive numpy integer types: iterrows() yields
    np.int64 index values, not Python ints, so an isinstance(int) guard
    would silently skip namespacing entirely and resurrect the bug -- this
    test exists to fail in that case too.
    """
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")

    # One real OSM node, two synthetic ones -- the index is int64, so
    # iterrows() hands back np.int64 values, exactly like real data.
    nodes = gpd.GeoDataFrame(
        {"x": [-73.99, -73.989, -73.988], "y": [40.68, 40.681, 40.682]},
        index=[42424089, -1, -2],
    )
    # One street-to-synthetic connector and one synthetic-to-synthetic
    # segment -- u and v each get namespaced independently, so both
    # positions need at least one synthetic id across the two edges.
    edges = gpd.GeoDataFrame(
        {
            "length_m": [150.0, 80.0],
            "name": ["", ""],
            "tree_deciduous": [0.0, 0.0],
            "tree_park_canopy": [0.0, 0.0],
            "tree_evergreen": [0.0, 0.0],
            "tree_count": [0, 0],
            "geometry": [
                LineString([(-73.99, 40.68), (-73.989, 40.681)]),
                LineString([(-73.989, 40.681), (-73.988, 40.682)]),
            ],
            "geometry_m": [
                LineString([(585435.9, 4503837.4), (585352.7, 4503950.1)]),
                LineString([(585352.7, 4503950.1), (585269.5, 4504062.8)]),
            ],
        },
        index=pd.MultiIndex.from_tuples(
            [(42424089, -1, 0), (-1, -2, 0)], names=["u", "v", "key"]
        ),
        geometry="geometry",
    )

    export.write_tile("test_tile", nodes, edges)

    with gzip.open(tmp_path / "tiles" / "test_tile.json.gz", "rt") as f:
        tile = json.load(f)

    # Real OSM id: untouched. Synthetic ids: tile-prefixed.
    assert set(tile["nodes"]) == {"42424089", "test_tile:-1", "test_tile:-2"}

    connector, interior = tile["edges"]
    assert (connector["u"], connector["v"]) == ("42424089", "test_tile:-1")
    assert (interior["u"], interior["v"]) == ("test_tile:-1", "test_tile:-2")

    # Every edge endpoint must resolve to an exported node -- namespacing
    # applied to the nodes dict but not edge u/v (or vice versa) would
    # otherwise ship a tile the server can't load.
    for edge in tile["edges"]:
        assert edge["u"] in tile["nodes"]
        assert edge["v"] in tile["nodes"]


def _minimal_tile_frames():
    """The smallest nodes/edges pair write_tile() accepts -- for tests
    about the WRITE mechanics (atomicity), not the exported content."""
    nodes = gpd.GeoDataFrame({"x": [-73.99, -73.989], "y": [40.68, 40.681]}, index=[1, 2])
    edges = gpd.GeoDataFrame(
        {
            "length_m": [150.0],
            "name": ["Test St"],
            "tree_deciduous": [2.5],
            "tree_evergreen": [0.0],
            "tree_count": [3],
            "tree_park_canopy": [0.0],
            "geometry": [LineString([(-73.99, 40.68), (-73.989, 40.681)])],
        },
        index=pd.MultiIndex.from_tuples([(1, 2, 0)], names=["u", "v", "key"]),
        geometry="geometry",
    )
    return nodes, edges


def test_write_tile_leaves_no_temp_file_after_a_clean_write(tmp_path, monkeypatch):
    # The atomic-write path (FIXES item 8) goes through <name>.json.gz.tmp
    # + os.replace(); a leftover .tmp after success would accumulate one
    # stray file per tile per run.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")
    nodes, edges = _minimal_tile_frames()

    export.write_tile("test_tile", nodes, edges)

    leftovers = list((tmp_path / "tiles").glob("*.tmp"))
    assert leftovers == []
    assert (tmp_path / "tiles" / "test_tile.json.gz").exists()


def test_write_tile_crash_mid_write_leaves_no_partial_final_file(tmp_path, monkeypatch):
    # The bug the atomic write exists for (audit §2.5): the old direct
    # write could die halfway and leave a truncated test_tile.json.gz that
    # GraphStore.load()'s glob would happily pick up. Now the final name
    # must either not exist at all or be the complete previous version --
    # and the crashed attempt must clean up its own .tmp.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")
    nodes, edges = _minimal_tile_frames()

    def explode(*args, **kwargs):
        raise OSError("disk full halfway through")

    monkeypatch.setattr(export.json, "dump", explode)
    with pytest.raises(OSError):
        export.write_tile("test_tile", nodes, edges)

    assert not (tmp_path / "tiles" / "test_tile.json.gz").exists()
    assert list((tmp_path / "tiles").glob("*")) == []
