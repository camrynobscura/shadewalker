"""Graph-wide invariants, run against the real citywide export.

Three checks that say nothing about HOW the graph was built: an edge is
never shorter than its own chord, geometry ends where its nodes are, and a
route's drawn length matches its reported length.

The near-coincident-disconnected-nodes sweep lived here until 2026-08-23 and
was removed with the centerline scoring code: it excluded divided-carriageway
pairs using pipeline/scoring/trees.py's sibling detector, and a divided
carriageway only exists as a concept when a street is a single centerline.
Under per-sidewalk edges each pavement is already its own edge, so the test
could not be ported -- it would have to be rebuilt around a different
question. See history/centerline-scoring-constants.md.

These run automatically whenever data/tiles/ holds a built export, and skip
cleanly otherwise so CI and a fresh clone never fail for lack of the
gitignored data. They are the ONLY tests left that exercise a real graph --
the pilot fixture and everything pinned to it went on 2026-08-23 -- so a
run without them proves considerably less than it used to. Loading the full
graph costs ~35s; in a tight edit-test loop skip it with:

    uv run pytest -m "not citywide"
"""
import random

import pytest

from server.graph_store import _local_distance_m

# The citywide shade-monotonicity sweep lived here until 2026-08-23 and was
# deleted with the centerline scoring code, alongside its pilot-tile
# companion in test_route_invariants.py. It swept the whole city asserting
# that raising Shade_priority never lowers shade_fraction -- a real and still
# correct invariant, but one that cannot be checked at all until sidewalk
# scoring produces non-zero shade. Rebuild it then; see
# history/centerline-scoring-constants.md.


# --- Multi-tile merge integrity (FIXES.md, 2026-08-13) ----------------------
# A synthetic-node id collision (interior sidewalks 1a + park trails 1g number
# synthetic nodes per-tile from -1, and graph_store.load() merges on id,
# first-tile-wins) wormholed 94.5k edges into false cross-city shortcuts:
# plausible length_m, geometry teleporting between boroughs (up to 49km). The
# invariant that should have caught it existed (test_route_invariants.py's
# alignment test) but was silently narrowed to the pilot fixture on
# 2026-07-17 -- one tile, no synthetic nodes, no merge, so the bug could not
# appear there.
#
# The invariants below outlived the centerline model -- they say nothing
# about HOW the graph was built, only that an edge is never shorter than
# its own chord, that geometry ends where its nodes are, and that a route's
# drawn length matches its reported length.
#
# They RUN as of 2026-08-23, against the real citywide export. They had been
# skipping silently for weeks: citywide_store required 10+ tile files, a
# threshold from the 276-tile centerline grid, and the sidewalk model writes
# exactly one -- so the gate could never be satisfied and present data
# yielded zero coverage. Exactly the vacuous pass that let the id collision
# survive 200+ tests. There is no fast-tier fixture behind them any more, so
# `-m "not citywide"` genuinely skips them; don't read that run as green.

MERGE_STORES = [
    pytest.param("citywide_store", marks=pytest.mark.citywide),
]

EDGE_LENGTH_SLACK_M = 2.0  # 6dp coord + 0.1m length rounding leaves real
# edges violating length_m >= chord by at most rounding-scale fractions of a
# meter; a wormhole understates length_m relative to its endpoints by tens of
# meters to kilometers, far past this.

EDGE_LENGTH_SLACK_RELATIVE = 0.002  # plus 0.2% of length_m: length_m comes
# from summed UTM-projected segment distances, the chord below from a
# flat-earth degree approximation, and neither matches a true ellipsoid
# distance exactly -- on a multi-km DEAD-STRAIGHT edge (real case: 2.7km
# boardwalk-style synthetic paths, where chord == polyline) those
# approximations disagree by ~0.15%, tripping a purely absolute slack. A
# wormhole's length_m is short while its chord is huge, so a fraction OF
# LENGTH_M adds essentially nothing to what a wormhole is allowed.

GEOMETRY_ENDPOINT_TOL_M = 1.0  # metric, not the pilot test's 1e-6 deg: a few
# hundred citywide edges sit rounding-scale (<=0.2m) off their nodes; 1m
# clears those while a wormhole endpoint is >>25m off.


@pytest.mark.parametrize("store_fixture", MERGE_STORES)
def test_no_edge_is_shorter_than_the_straight_line_between_its_endpoints(store_fixture, request):
    """Physical invariant: an edge's stored length_m can never be less than
    the straight-line distance between its own two endpoint nodes -- a path
    is at least its chord. A merge that remaps an endpoint to a different
    tile's node (the synthetic-id collision) leaves a plausible short
    length_m attached to endpoints kilometers apart, which this catches
    decisively. Independent of geometry storage, so it cross-checks the
    alignment invariant below rather than restating it."""
    store = request.getfixturevalue(store_fixture)
    idx_to_id = {idx: node_id for node_id, idx in store._id_to_idx.items()}
    worst = []
    for edge in range(len(store._length)):
        u, v = store._graph.es[edge].tuple
        lon_u, lat_u = store._node_lonlat[u]
        lon_v, lat_v = store._node_lonlat[v]
        straight = _local_distance_m(lat_u, lon_u, lat_v, lon_v)
        length_m = float(store._length[edge])
        if straight - length_m > EDGE_LENGTH_SLACK_M + EDGE_LENGTH_SLACK_RELATIVE * length_m:
            worst.append((edge, straight, length_m, u, v))
    worst.sort(key=lambda t: -(t[1] - t[2]))
    assert not worst, (
        f"{len(worst)} edge(s) shorter than the straight line between their "
        f"endpoints -- impossible for a real edge, the signature of a merge "
        f"remapping an endpoint to the wrong node. Worst:\n" + "\n".join(
            f"  edge {e}: length_m={length:.1f} but endpoints {chord:.1f}m apart "
            f"(nodes {idx_to_id.get(u, u)} / {idx_to_id.get(v, v)})"
            for e, chord, length, u, v in worst[:5]
        )
    )


@pytest.mark.parametrize("store_fixture", MERGE_STORES)
def test_every_edge_geometry_starts_and_ends_at_its_own_nodes_citywide(store_fixture, request):
    """Multi-tile sibling of test_route_invariants.py's pilot-scoped alignment
    test -- the merge coverage that test's own docstring promised ('once
    Stage 2 adds more tiles it also validates the packing across the
    multi-tile merge') but never got once fixture isolation pinned it to the
    single pilot tile. Metric tolerance instead of 1e-6 deg for the same
    rounding reason as the length invariant above."""
    store = request.getfixturevalue(store_fixture)
    misaligned = []
    for edge in range(len(store._length)):
        coords = store._edge_coords(edge)
        if len(coords) < 2:
            misaligned.append((edge, "fewer than 2 points"))
            continue
        u, v = store._graph.es[edge].tuple
        nu = store._node_lonlat[u]
        nv = store._node_lonlat[v]
        start_to_u = _local_distance_m(coords[0][1], coords[0][0], nu[1], nu[0])
        end_to_v = _local_distance_m(coords[-1][1], coords[-1][0], nv[1], nv[0])
        start_to_v = _local_distance_m(coords[0][1], coords[0][0], nv[1], nv[0])
        end_to_u = _local_distance_m(coords[-1][1], coords[-1][0], nu[1], nu[0])
        runs_uv = max(start_to_u, end_to_v) <= GEOMETRY_ENDPOINT_TOL_M
        runs_vu = max(start_to_v, end_to_u) <= GEOMETRY_ENDPOINT_TOL_M
        if not (runs_uv or runs_vu):
            off_m = min(max(start_to_u, end_to_v), max(start_to_v, end_to_u))
            misaligned.append((edge, f"{off_m:.1f}m off (nodes {u}/{v})"))
    assert not misaligned, (
        f"{len(misaligned)} edge(s) whose geometry doesn't land on their own "
        f"endpoint nodes -- packed-buffer or merge misalignment. First few: "
        f"{misaligned[:5]}"
    )


@pytest.mark.parametrize("store_fixture", MERGE_STORES)
def test_route_geometry_length_matches_reported_length(store_fixture, request):
    """A returned route's drawn polyline and its reported length_m must
    describe the same path. The synthetic-id collision produced routes with
    a plausible length_m but geometry teleporting across the city -- caught
    here because the polyline summed from the returned coords then dwarfs
    length_m. The user-visible half of the invariant, complementing the
    per-edge checks above. Same sampling shape as the shade sweep."""
    store = request.getfixturevalue(store_fixture)
    nodes = list(max(store._graph.connected_components(mode="weak"), key=len))
    rng = random.Random(20260813)
    checked = 0
    bad = []
    attempts = 0
    while checked < 40 and attempts < 40 * 80:
        attempts += 1
        a, b = rng.choice(nodes), rng.choice(nodes)
        if a == b:
            continue
        lon_a, lat_a = store._node_lonlat[a]
        lon_b, lat_b = store._node_lonlat[b]
        if not (500.0 <= _local_distance_m(lat_a, lon_a, lat_b, lon_b) <= 2500.0):
            continue
        pair = store.snap_pair(lat_a, lon_a, lat_b, lon_b)
        if pair is None:
            continue
        start, end = pair
        result = store.route(start, end, tree_weight=0.0, month=7)
        if result is None:
            continue
        coords = result["coords"]
        polyline_m = sum(
            _local_distance_m(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0])
            for i in range(1, len(coords))
        )
        reported = result["length_m"]
        checked += 1
        # Very loose: a legit polyline matches length_m to well under 1%; a
        # wormhole overshoots by orders of magnitude, so even 25%+25m catches
        # it without any risk of flagging a real route.
        if abs(polyline_m - reported) > 0.25 * reported + 25.0:
            bad.append((reported, polyline_m, (lat_a, lon_a), (lat_b, lon_b)))
    assert checked >= 30, f"only checked {checked} routes -- sampling got too sparse"
    assert not bad, (
        f"{len(bad)} route(s) whose drawn polyline length disagrees with the "
        f"reported length_m (geometry and stats describe different paths). "
        f"First few:\n" + "\n".join(
            f"  reported={r:.0f}m polyline={p:.0f}m from {a} to {b}"
            for r, p, a, b in bad[:5]
        )
    )


