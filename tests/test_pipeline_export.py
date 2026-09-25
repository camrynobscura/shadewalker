"""Tests for pipeline/export.py -- the contract between the pipeline and
the routing server.

One writer: write_citywide(), the sidewalk model's single-file export.

The centerline model's write_tile() and its four tests were deleted on
2026-08-23 with the tile grid. Two of those tests (clean-write leaves no
temp file, crash mid-write leaves no partial file) covered the shared
_write_atomically() helper; both have direct write_citywide() equivalents
below, so deleting the tile writer cost no coverage of live code.

The module's own docstring flags a hard-won rule: everything written must
be lat/lon degrees (EPSG:4326), never meter-based geometry -- that exact
bug shipped once in the v1 prototype.
"""

import gzip
import json

import pytest

from pipeline import config, export


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
        "side": "",
        "kind": "footway/sidewalk",
        "length_m": 84.2,
        "name": "Court Street",
        "coords": [[-74.002788, 40.680597], [-74.001390, 40.680205]],
    }]
    return nodes, edges


def _read_back(tmp_path):
    out = tmp_path / "export" / f"{export.CITYWIDE_NAME}.json.gz"
    assert out.exists(), f"nothing written to {out}"
    with gzip.open(out, "rt") as fh:
        return json.load(fh)


@pytest.fixture
def citywide_dir(tmp_path, monkeypatch):
    # REPO_ROOT alongside EXPORT_DIR for the same reason write_tile's tests
    # patch it: the log line does relative_to(REPO_ROOT), which raises for a
    # tmp_path outside the repo.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "export")
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
    assert edge["side"] == ""
    assert edge["kind"] == "footway/sidewalk"
    assert edge["coords"] == [[-74.002788, 40.680597], [-74.001390, 40.680205]]


def test_fold_names_are_written_only_when_present(citywide_dir):
    """The folding evidence rides along on unnamed edges and stays off
    named ones -- an absent key is the contract, not an empty list."""
    nodes, edges = _minimal_citywide()
    edges[0]["name"] = ""
    edges[0]["fold_names"] = ["Court Street", "Union Street"]
    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]
    assert edge["fold_names"] == ["Court Street", "Union Street"]

    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]
    assert "fold_names" not in edge


def test_write_citywide_lands_where_graph_store_globs_for_it(citywide_dir):
    """server/graph_store.py:301 does EXPORT_DIR.glob("*.json.gz"). A file
    written anywhere else, or under any other suffix, is invisible to the
    server no matter how correct its contents are."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    assert sorted(p.name for p in (citywide_dir / "export").glob("*.json.gz")) \
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


def test_write_citywide_keeps_a_fractional_tree_count(citywide_dir):
    """Block-face scoring hands an edge a SHARE of its block's trees, in
    proportion to its own length, so fractional counts are the normal case
    rather than an oddity. This field used to be cast with int(), which
    truncated: a face with 3 trees spread over 10 edges gave each 0.3, and
    all three trees vanished from the export.

    The whole-number count the walker sees is produced later --
    graph_store.py sums these shares along a route and rounds once at the
    end -- so rounding here would lose the trees a second way.
    """
    nodes, edges = _minimal_citywide()
    edges[0]["tree_count"] = 0.3
    edges[0]["tree_deciduous"] = 0.084
    edges[0]["tree_evergreen"] = 0.0

    export.write_citywide(nodes, edges)
    edge = _read_back(citywide_dir)["edges"][0]
    assert edge["tree_count"] == pytest.approx(0.3)


def test_write_citywide_leaves_no_temp_file_after_a_clean_write(citywide_dir):
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    assert list((citywide_dir / "export").glob("*.tmp")) == []


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

    assert not (citywide_dir / "export" / "citywide.json.gz").exists()
    assert list((citywide_dir / "export").glob("*")) == []


def test_write_citywide_overwrites_a_previous_export_in_place(citywide_dir):
    """Rebuilds are routine. The second run must replace the first, not
    accumulate a second file the server would also load."""
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    edges[0]["name"] = "Union Street"
    export.write_citywide(nodes, edges)

    assert len(list((citywide_dir / "export").glob("*.json.gz"))) == 1
    assert _read_back(citywide_dir)["edges"][0]["name"] == "Union Street"


# ── building shade (PLAN `building-shadows`, interim base64 packing) ────

def _shaded_edges():
    nodes, edges = _minimal_citywide()
    row = bytes([0] * 12 + [255, 170, 85] + [0] * 273)     # three lit slots
    edges[0]["building_shade"] = row
    dark = dict(edges[0], key=1, building_shade=bytes(288))  # all-zero row
    absent = dict(edges[0], key=2)                          # no row at all
    del absent["building_shade"]
    return nodes, [edges[0], dark, absent], row


def test_building_shade_is_written_as_base64_only_when_lit(citywide_dir):
    import base64
    nodes, edges, row = _shaded_edges()
    export.write_citywide(nodes, edges)
    written = _read_back(citywide_dir)["edges"]
    assert base64.b64decode(written[0]["building_shade"]) == row
    assert "building_shade" not in written[1]      # all-zero: omitted, 488k edges pay per key
    assert "building_shade" not in written[2]      # never scored: omitted


def test_sun_table_and_slot_layout_land_in_meta_when_given(citywide_dir):
    nodes, edges, _ = _shaded_edges()
    table = [[None] * 24 for _ in range(12)]
    table[6][13] = [178.6, 70.7]
    export.write_citywide(nodes, edges, sun_table=table)
    meta = _read_back(citywide_dir)["meta"]
    assert meta["sun_table"] == table
    assert meta["shade_slots"]["months"] == 12 and meta["shade_slots"]["hours"] == 24
    assert "month-major" in meta["shade_slots"]["layout"]


def test_meta_has_no_shade_keys_without_a_sun_table(citywide_dir):
    nodes, edges = _minimal_citywide()
    export.write_citywide(nodes, edges)
    meta = _read_back(citywide_dir)["meta"]
    assert "sun_table" not in meta and "shade_slots" not in meta
