"""Turning shade OFF must ignore tree data completely.

THE INVARIANT
-------------
server/graph_store.py costs every edge as

    length_m / (1.0 + tree_weight * density)

so at tree_weight = 0 the divisor is exactly 1 and the cost is exactly the
length, whatever the trees say. A NONE-priority route is therefore the plain
shortest walk, and adding, rescoring or removing tree data cannot move it.

That is arithmetic rather than a hope -- which is precisely why it deserves
a test. The property is easy to break by accident: any future term that
touches cost outside the `tree_weight *` product (a floor, a bonus, a
penalty, a saturation applied in the wrong place) would silently make plain
walking directions depend on tree data. DENSITY_LENGTH_FLOOR_M was exactly
such a term until it was deleted on 2026-08-24; it divided by
max(length, floor) rather than length, and a floor applied to the COST
instead of the density would have broken this.

WHY NOT COMPARE AGAINST AN EXTERNAL ENGINE
------------------------------------------
That was the original plan for this step and it answers a different
question. OSRM and Valhalla tell us whether our routing is *reasonable*, not
whether tree data perturbed it -- and their comparison carries real noise
from ferries, coverage gaps and snapping. This is a closed question with an
exact answer, so it gets an exact test: same graph, same request, tree data
zeroed, byte-identical result.

The external harness still exists and still earns its keep
(tests/test_external_validation.py, opt-in); it just cannot pin this.
"""

import gzip
import json

import pytest

from pipeline import config
from server.graph_store import GraphStore

# A diamond with two ways from n1 to n3: a SHORT bare pair of edges, and a
# LONGER pair carrying heavy tree cover. Shade-off must take the short one;
# shade-on must take the leafy one. Without that second fact the first would
# pass on a graph that offers no choice at all, which would make this test
# look green while proving nothing.
NODES = {
    "n1": [-73.99000, 40.68000],
    "n2": [-73.98882, 40.68000],
    "n3": [-73.98764, 40.68000],
    "n4": [-73.98882, 40.68054],   # ~60m north of n2 -- the leafy detour
}

# (u, v, name, length_m, deciduous, evergreen, count)
EDGES = [
    ("n1", "n2", "Bare Street", 100.0, 0.0, 0.0, 0),
    ("n2", "n3", "Bare Street", 100.0, 0.0, 0.0, 0),
    ("n1", "n4", "Leafy Lane", 120.0, 3.0, 0.0, 6),
    ("n4", "n3", "Leafy Lane", 120.0, 3.0, 0.0, 6),
]
# Bare route 200m, leafy route 240m.
# At weight 0:  200 < 240                       -> bare wins on length.
# At weight 40: density 3.0/120 = 0.025, so each leafy edge costs
#               120 / (1 + 40*0.025) = 60, total 120 < 200 -> leafy wins.

START = (40.68000, -73.99000)   # n1, as (lat, lon)
END = (40.68000, -73.98764)     # n3


def _edge_record(u, v, name, length_m, deciduous, evergreen, count):
    return {
        "u": u, "v": v, "key": 0, "side": "L",
        "length_m": length_m, "name": name,
        "tree_deciduous": deciduous, "tree_evergreen": evergreen,
        "tree_count": count, "tree_park_canopy": 0.0,
        "coords": [NODES[u], NODES[v]],
    }


@pytest.fixture()
def store(tmp_path, monkeypatch) -> GraphStore:
    """A fresh store per test -- these tests MUTATE the tree arrays, so a
    shared instance would leak zeroed data into whichever test ran next."""
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    with gzip.open(tmp_path / "tile.json.gz", "wt") as fh:
        json.dump({"nodes": NODES,
                   "edges": [_edge_record(*e) for e in EDGES]}, fh)
    loaded = GraphStore()
    loaded.load()
    return loaded


def _route(store, tree_weight):
    pair = store.snap_pair(*START, *END)
    assert pair is not None, "fixture graph should connect START to END"
    result = store.route(pair[0], pair[1], tree_weight=tree_weight, month=7)
    assert result is not None
    return result


def _strip_trees(store):
    store._tree_deciduous[:] = 0.0
    store._tree_evergreen[:] = 0.0
    store._tree_count[:] = 0.0
    # Densities are cached per time (graph_store._density_cache); mutating
    # the arrays underneath it in place must drop what it holds.
    store._density_cache.clear()


# --- the guard against a vacuous test ---------------------------------

def test_the_fixture_actually_offers_a_shadier_alternative(store):
    """If both routes were the same, the invariant below would hold for a
    reason that has nothing to do with tree_weight = 0."""
    shade_off = _route(store, 0.0)
    shade_on = _route(store, 40.0)

    assert shade_off["length_m"] == pytest.approx(200.0)
    assert shade_on["length_m"] == pytest.approx(240.0)
    assert shade_on["coords"] != shade_off["coords"]
    assert shade_on["tree_count"] > shade_off["tree_count"]


# --- the invariant ----------------------------------------------------

def test_shade_off_route_is_identical_with_and_without_tree_data(store):
    """THE POINT OF THIS FILE. Same graph, same request, trees deleted."""
    before = _route(store, 0.0)
    _strip_trees(store)
    after = _route(store, 0.0)

    assert after["coords"] == before["coords"]
    assert after["length_m"] == before["length_m"]
    assert after["minutes"] == before["minutes"]
    assert [s["name"] for s in after["segments"]] == \
           [s["name"] for s in before["segments"]]


def test_shade_off_costs_are_exactly_the_edge_lengths(store):
    """The mechanism behind the invariant, asserted directly: at weight 0
    the cost array IS the length array, so no tree value can enter it."""
    import numpy as np

    costs = store.edge_costs(tree_weight=0.0, month=7)
    assert np.allclose(costs, store._length)

    # ... and still is once the trees are gone, which is the same claim
    # from the other side.
    _strip_trees(store)
    assert np.allclose(store.edge_costs(tree_weight=0.0, month=7), store._length)


def test_shade_off_is_unaffected_by_the_month(store):
    """CANOPY_BY_MONTH scales deciduous credit 1.0 in July to 0.2 in
    January. That must not reach a NONE-priority route either -- if it did,
    plain walking directions would change with the calendar."""
    july = _route(store, 0.0)
    january = store.route(*store.snap_pair(*START, *END),
                          tree_weight=0.0, month=1)

    assert january["coords"] == july["coords"]
    assert january["length_m"] == july["length_m"]


def test_stripping_trees_DOES_change_the_shaded_route(store):
    """The negative control. If zeroing the trees changed nothing anywhere,
    the invariant test above would be passing because the tree data was
    never wired in -- not because tree_weight = 0 excludes it."""
    before = _route(store, 40.0)
    assert before["length_m"] == pytest.approx(240.0)

    _strip_trees(store)
    after = _route(store, 40.0)

    assert after["length_m"] == pytest.approx(200.0), (
        "with no trees, the shadiest preset must fall back to the shortest "
        "walk -- if it did not, tree data is not reaching edge_costs at all"
    )
    assert after["coords"] != before["coords"]
