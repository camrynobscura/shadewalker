"""Tests for pipeline/export.py -- the contract between the pipeline and
the routing server.

Two writers, tested separately below:

  write_tile()      the retired centerline model's per-tile writer
  write_citywide()  the sidewalk model's single-file writer

The module's own docstring flags a hard-won rule: everything written must be
lat/lon degrees (EPSG:4326), never the meter-based geometry_m -- that exact
bug shipped once in the v1 prototype. The main test below pins it directly by
giving write_tile() an edge with *both* geometry columns present (as real
edges have) and asserting the export used the degrees one.

Both writers share _write_atomically(), so its crash/temp-file behaviour is
covered on each -- the shared helper is exactly the kind of thing that gets
refactored later by someone testing only one caller.
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
            # node_ids: one per geometry vertex (FIXES 13); a two-point edge
            # is just [u, v].
            "node_ids": [[1, 2]],
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
            # node_ids carry synthetic ids too, so they must be namespaced by
            # the same rule as u/v (FIXES 13) -- asserted below.
            "node_ids": [[42424089, -1], [-1, -2]],
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

    # node_ids get the same namespacing as u/v, and every id in them
    # resolves to an exported node -- so the server can split on them.
    assert connector["node_ids"] == ["42424089", "test_tile:-1"]
    assert interior["node_ids"] == ["test_tile:-1", "test_tile:-2"]
    for edge in tile["edges"]:
        for nid in edge["node_ids"]:
            assert nid in tile["nodes"]


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
            "node_ids": [[1, 2]],
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


# ── write_citywide(): the sidewalk model's single-file export ────────────
#
# This is the file server/graph_store.py:301 globs and loads. Nothing else
# stands between pipeline output and the routing server, so its shape IS
# the contract.


def _minimal_citywide():
    """The smallest nodes/edges pair write_citywide() accepts, in the
    vocabulary pipeline/graph/pedestrian.py's build() emits -- note it
    carries NO tree fields, because scoring is a later step."""
    nodes = {
        "10135442390": (-74.0027881, 40.6805974),
        "10135442393": (-74.0013902, 40.6802051),
    }
    edges = [{
        "u": "10135442390",
        "v": "10135442393",
        "key": 0,
        "side": "C",
        "length_m": 84.2,
        "name": "Court Street",
        "coords": [[-74.002788, 40.680597], [-74.001390, 40.680205]],
    }]
    return nodes, edges


def _read_back(tmp_path):
    out = tmp_path / "tiles" / f"{export.CITYWIDE_NAME}.json.gz"
    assert out.exists(), f"nothing written to {out}"
    with gzip.open(out, "rt") as fh:
        return json.load(fh)


@pytest.fixture
def citywide_dir(tmp_path, monkeypatch):
    # REPO_ROOT alongside TILES_DIR for the same reason write_tile's tests
    # patch it: the log line does relative_to(REPO_ROOT), which raises for a
    # tmp_path outside the repo.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")
    return tmp_path


def test_write_citywide_round_trips_nodes_and_edges(citywide_dir):
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    payload = _read_back(citywide_dir)

    assert set(payload["nodes"]) == {"10135442390", "10135442393"}
    edge = payload["edges"][0]
    assert edge["u"] == "10135442390"
    assert edge["v"] == "10135442393"
    assert edge["name"] == "Court Street"
    assert edge["length_m"] == 84.2
    assert edge["side"] == "C"
    assert edge["coords"] == [[-74.002788, 40.680597], [-74.001390, 40.680205]]


def test_write_citywide_lands_where_graph_store_globs_for_it(citywide_dir):
    """server/graph_store.py:301 does TILES_DIR.glob("*.json.gz"). A file
    written anywhere else, or under any other suffix, is invisible to the
    server no matter how correct its contents are."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    assert sorted(p.name for p in (citywide_dir / "tiles").glob("*.json.gz")) \
        == ["citywide.json.gz"]


def test_write_citywide_defaults_every_tree_field_to_zero(citywide_dir):
    """The spine exports an UNSCORED graph. graph_store.py:399-401 reads
    tree_deciduous/tree_evergreen/tree_count with bracket access, so a
    missing key is a KeyError at server startup, not a soft failure."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]

    assert edge["tree_deciduous"] == 0.0
    assert edge["tree_evergreen"] == 0.0
    assert edge["tree_count"] == 0
    assert edge["tree_park_canopy"] == 0.0


def test_write_citywide_keeps_real_tree_scores_when_they_exist(citywide_dir):
    """The defaults above must not clobber a scored graph -- this is the
    same writer once the scoring step lands."""
    nodes, edges = _minimal_citywide()
    edges[0].update(tree_deciduous=3.5, tree_evergreen=1.25,
                    tree_count=7, tree_park_canopy=0.5)
    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]

    assert edge["tree_deciduous"] == 3.5
    assert edge["tree_evergreen"] == 1.25
    assert edge["tree_count"] == 7
    assert edge["tree_park_canopy"] == 0.5


def test_write_citywide_rounds_node_coordinates_to_six_places(citywide_dir):
    """Six decimal places is ~11cm -- finer than any of this data is
    accurate to, and it keeps the file from carrying float noise for
    386,576 nodes."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    assert _read_back(citywide_dir)["nodes"]["10135442390"] == [-74.002788, 40.680597]


def test_write_citywide_meta_counts_match_the_real_contents(citywide_dir):
    """A meta block that disagrees with the payload sends anyone debugging
    a load problem after the wrong thing."""
    nodes, edges = _minimal_citywide()
    nodes["999"] = (-73.99, 40.70)
    export.write_citywide(nodes, edges)
    payload = _read_back(citywide_dir)

    assert payload["meta"]["node_count"] == len(payload["nodes"]) == 3
    assert payload["meta"]["edge_count"] == len(payload["edges"]) == 1
    assert payload["meta"]["crs"] == "EPSG:4326"
    assert payload["meta"]["coord_order"] == "lon,lat"


def test_write_citywide_coerces_numpy_integers(citywide_dir):
    """build() computes `key` in Python but lengths come back from numpy,
    and a numpy int64 is not JSON-serialisable -- json.dump raises
    TypeError on it. write_tile hit exactly this via iterrows(); the int()
    calls here are what keep it from recurring."""
    np = pytest.importorskip("numpy")
    nodes, edges = _minimal_citywide()
    edges[0]["key"] = np.int64(3)
    edges[0]["tree_count"] = np.int64(11)

    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]
    assert edge["key"] == 3
    assert edge["tree_count"] == 11


def test_write_citywide_leaves_no_temp_file_after_a_clean_write(citywide_dir):
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    assert list((citywide_dir / "tiles").glob("*.tmp")) == []


def test_write_citywide_crash_mid_write_leaves_no_partial_file(citywide_dir):
    """The atomic-write guarantee (FIXES item 8) applied to the citywide
    writer: a crash must leave either the complete previous version or
    nothing -- never a truncated file that GraphStore.load()'s glob would
    happily pick up."""
    nodes, edges = _minimal_citywide()

    def explode(*args, **kwargs):
        raise OSError("disk full halfway through")

    monkeypatch_target = export.json
    original_dump = monkeypatch_target.dump
    monkeypatch_target.dump = explode
    try:
        with pytest.raises(OSError):
            export.write_citywide(nodes, edges)
    finally:
        monkeypatch_target.dump = original_dump

    assert not (citywide_dir / "tiles" / "citywide.json.gz").exists()
    assert list((citywide_dir / "tiles").glob("*")) == []


def test_write_citywide_overwrites_a_previous_export_in_place(citywide_dir):
    """Rebuilds are routine. The second run must replace the first, not
    accumulate a second file the server would also load."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    edges[0]["name"] = "Union Street"
    export.write_citywide(nodes, edges)

    assert len(list((citywide_dir / "tiles").glob("*.json.gz"))) == 1
    assert _read_back(citywide_dir)["edges"][0]["name"] == "Union Street"
