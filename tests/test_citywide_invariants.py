"""Invariants that only exist at multi-tile scale -- the shade-priority and
near-coincident sweeps over the FULL citywide graph, plus the merge-integrity
invariants, which run twice: always against the committed two-tile fixture
(tests/fixtures/merge_pair/, fast tier, CI-covered) and additionally against
the real citywide data when it's on disk.

Citywide-marked tests run automatically whenever the real data/tiles/ data
is present (the citywide_store fixture skips otherwise, so CI and a fresh
clone never fail for lack of it). In a tight edit-test loop, skip the ~15s
graph load with:

    uv run pytest -m "not citywide"
"""
import random

import numpy as np
import pytest
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from pipeline.graph.centerline import METRIC_CRS
from pipeline.scoring.trees import (
    SIBLING_MAX_BEARING_DIFF_DEG,
    SIBLING_MAX_LENGTH_RATIO,
    SIBLING_MAX_SEPARATION_M,
    SIBLING_MIN_LENGTH_M,
    SIBLING_MIN_OVERLAP_FRACTION,
    _along_track_overlap_fraction,
    _bearing_difference_deg,
)
from server.graph_store import _local_distance_m, clamp_shade_monotonic

WEIGHTS = [0.0, 5.0, 15.0, 40.0]


@pytest.mark.citywide
def test_more_shade_priority_never_reduces_shade_across_the_whole_city(citywide_store):
    """The same guarantee the pilot random-route test checks, swept over the
    entire citywide network. Applies the exact clamp_shade_monotonic the
    /route endpoint uses (not a reimplementation), so a green run means the
    shipping behavior holds on real, diverse geography -- and it doubles as a
    regression guard on the clamp function itself.

    Seeded, so it's reproducible despite sampling randomly. Months 4 and 7
    are pinned deliberately: April (partial canopy) is where the bug peaks
    and July is the live-server default -- see the pilot-tile companion in
    test_route_invariants.py for the season reasoning."""
    store = citywide_store
    nodes = list(max(store._graph.connected_components(mode="weak"), key=len))
    rng = random.Random(20260726)

    violations = []
    routed = 0
    attempts = 0
    while routed < 40 and attempts < 40 * 80:
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
        routed_any = False
        for month in (4, 7):
            results = [store.route(start, end, tree_weight=w, month=month) for w in WEIGHTS]
            if any(r is None for r in results):
                continue
            routed_any = True
            shades = [r["shade_fraction"] for r in clamp_shade_monotonic(results, WEIGHTS)]
            for i in range(1, len(shades)):
                if shades[i] < shades[i - 1] - 1e-9:
                    violations.append((month, (lat_a, lon_a), (lat_b, lon_b), shades))
                    break
        if routed_any:
            routed += 1

    assert routed >= 30, f"only routed {routed} citywide pairs -- sampling got too sparse"
    assert not violations, (
        f"{len(violations)} citywide route(s) where more shade priority reduced shade "
        f"(the clamp should make this impossible). First few:\n"
        + "\n".join(
            f"  month={m} from={a[0]:.5f},{a[1]:.5f} to={b[0]:.5f},{b[1]:.5f} "
            f"shades={[round(x, 3) for x in s]}"
            for m, a, b, s in violations[:5]
        )
    )


# --- Near-coincident disconnected nodes (FIXES.md, 2026-08-01) --------------
# A full citywide sweep found 16,260 real, distinct OSM node pairs sitting a
# few meters apart in reality that needed a huge detour in the graph because
# nothing connected them directly -- a widespread pattern, not a one-off (see
# tests/test_route_regressions.py's Ozone Park pin for one concrete example).
# This is a fast, seeded sample standing in for that ~3 hour full sweep, so it
# can run as part of the normal suite. Excludes genuine divided-carriageway
# pairs (Park Avenue, Hylan Boulevard, ...) using the exact sibling-
# carriageway logic pipeline/scoring/trees.py already validates for a
# different purpose (not double-counting median trees) -- a real median
# crossing must never count as a failure here, only a genuine missing
# connector.
#
# Of the 16,260, a four-signal verification pass (real buildings, OSM
# barrier/foot=no/access=no/railway tags, PLUTO industrial/utility lots,
# Osmose cross-reference -- see history/near-coincident-node-gaps.md) found
# ~14% have a real obstruction and left those deliberately unbridged
# (server/known_node_gaps.json holds only the verified-clean ~86%). A small
# residual of exactly those legitimately-blocked pairs landing in this
# sample is expected, not a bug -- MAX_EXPECTED_FLAGGED gives headroom for
# that without masking a real widespread regression, which would show up as
# dozens+, not a handful.

NEAR_NODE_MAX_M = 25.0  # matches SIBLING_MAX_SEPARATION_M -- same "how close
# counts as the same real spot" scale as the sibling-carriageway detector
# this test borrows from, so the two checks reason about the same distances.
DETOUR_FLAG_M = 200.0
SAMPLE_SIZE = 800
MAX_EXPECTED_FLAGGED = 20  # real-OSM pairs; residual was 7 when set, now 11
MAX_EXPECTED_SYNTHETIC_FLAGGED = 150  # synthetic-path pairs; measured 97 on
# 2026-08-13 -- see the budget comment inside the test for what each
# population means and what a breach of each would indicate


def _named_edge_lines(store, node, to_metric):
    """(name, LineString-in-metric-CRS, length_m) for real named edges
    incident to this node, long enough to plausibly be a carriageway."""
    out = []
    for e in store._graph.incident(node):
        name = store._names[e]
        if not name or store._length[e] < SIBLING_MIN_LENGTH_M:
            continue
        coords = store._edge_coords(e)
        metric_coords = [to_metric(lon, lat) for lon, lat in coords]
        out.append((name, LineString(metric_coords), float(store._length[e])))
    return out


def _looks_like_sibling_carriageway(store, node_a, node_b, to_metric):
    for name_a, line_a, len_a in _named_edge_lines(store, node_a, to_metric):
        for name_b, line_b, len_b in _named_edge_lines(store, node_b, to_metric):
            if name_a != name_b:
                continue
            long_len, short_len = max(len_a, len_b), min(len_a, len_b)
            if long_len > SIBLING_MAX_LENGTH_RATIO * short_len + 20.0:
                continue
            if line_a.distance(line_b) > SIBLING_MAX_SEPARATION_M:
                continue
            if _bearing_difference_deg(line_a, line_b) > SIBLING_MAX_BEARING_DIFF_DEG:
                continue
            if _along_track_overlap_fraction(line_a, line_b) < SIBLING_MIN_OVERLAP_FRACTION:
                continue
            return True
    return False


@pytest.mark.citywide
def test_no_widespread_near_coincident_disconnected_nodes(citywide_store):
    store = citywide_store
    lonlat = store._node_lonlat
    n = len(lonlat)
    to_metric = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform

    mean_lat = float(np.mean(lonlat[:, 1]))
    lat_scale = np.cos(np.radians(mean_lat))
    scaled = np.column_stack([lonlat[:, 0] * lat_scale, lonlat[:, 1]]) * 111320.0
    tree = cKDTree(scaled)

    rng = random.Random(20260801)
    sample_nodes = rng.sample(range(n), min(SAMPLE_SIZE, n))

    flagged = []
    for node in sample_nodes:
        candidates = tree.query_ball_point(scaled[node], r=NEAR_NODE_MAX_M)
        if len(candidates) < 2:
            continue
        direct_neighbors = set(store._graph.neighbors(node))
        lon_a, lat_a = lonlat[node]

        real_targets = []
        for other in candidates:
            if other == node or other in direct_neighbors:
                continue
            lon_b, lat_b = lonlat[other]
            real_dist = _local_distance_m(lat_a, lon_a, lat_b, lon_b)
            if 0.5 <= real_dist <= NEAR_NODE_MAX_M:
                real_targets.append((other, real_dist))
        if not real_targets:
            continue

        target_nodes = [t[0] for t in real_targets]
        graph_dists = store._graph.distances(source=[node], target=target_nodes, weights=store._length)[0]

        for (other, real_dist), graph_dist in zip(real_targets, graph_dists):
            if graph_dist == float("inf") or graph_dist <= DETOUR_FLAG_M:
                continue
            if _looks_like_sibling_carriageway(store, node, other, to_metric):
                continue
            flagged.append((node, other, real_dist, graph_dist))

    # Two separately-budgeted populations (2026-08-13), because they are
    # different phenomena with different owners:
    #
    # REAL pairs (both nodes real OSM ids) are what this test was built and
    # calibrated for -- missing street-corner connections, the bug class
    # behind server/known_node_gaps.json. Residual was 7 when the cap of 20
    # was set; currently 11.
    #
    # SYNTHETIC pairs (either node a namespaced synthetic id) are the ends
    # and crossings of imported path layers (interior sidewalks 1a, park
    # trails 1g) that the pipeline deliberately left unconnected: measured
    # composition 71 dangling ends (too far / unverifiable), 20 mid-line
    # crossings (paths only join at snapped ends, by design), 17 blocked by
    # a real mapped fence (correctly unconnected). All are missing-shortcut
    # cases, never false connections; a direct worst-case comparison against
    # OSRM's OSM-only routing measured ZERO route degradation from them.
    # Budgeted at 150 (measured 97 after load()'s duplicate-copy stitching):
    # a jump back toward ~180 means the stitch pass regressed, and growth
    # past 150 means new-path connection quality slipped -- both worth an
    # alarm. FIXES.md's connection-audit item is the plan for shrinking the
    # 97; tighten the budget as it lands.
    idx_to_id = {idx: node_id for node_id, idx in store._id_to_idx.items()}

    def _is_synthetic_pair(a, b):
        return ":" in idx_to_id.get(a, "") or ":" in idx_to_id.get(b, "")

    real_flagged = [f for f in flagged if not _is_synthetic_pair(f[0], f[1])]
    synthetic_flagged = [f for f in flagged if _is_synthetic_pair(f[0], f[1])]

    def _describe(pairs):
        return "\n".join(
            f"  nodes {idx_to_id.get(a, a)},{idx_to_id.get(b, b)}: real={real:.1f}m "
            f"graph={graph:.1f}m at ({lonlat[a][1]:.5f},{lonlat[a][0]:.5f})"
            for a, b, real, graph in pairs[:5]
        )

    assert len(real_flagged) <= MAX_EXPECTED_FLAGGED, (
        f"{len(real_flagged)} REAL-OSM node pair(s) sit within {NEAR_NODE_MAX_M}m of "
        f"each other in reality but need a >{DETOUR_FLAG_M}m detour in the graph, and "
        f"aren't a known divided-carriageway pattern -- well beyond the small, "
        f"legitimately-blocked residual expected (<= {MAX_EXPECTED_FLAGGED}). "
        f"First few:\n" + _describe(real_flagged)
    )
    assert len(synthetic_flagged) <= MAX_EXPECTED_SYNTHETIC_FLAGGED, (
        f"{len(synthetic_flagged)} synthetic-path pair(s) flagged -- measured 97 on "
        f"2026-08-13 with the duplicate-copy stitch working (~180 without it), so "
        f"either the stitch pass regressed or path-connection quality slipped. "
        f"First few:\n" + _describe(synthetic_flagged)
    )


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
# Each of the three runs against BOTH multi-tile stores: the committed
# two-tile fixture (merge_pair_store -- fast tier, so CI actually covers the
# merge path) and, when the gitignored citywide data is on disk, the real
# merged graph. One copy of the logic, two datasets, so the fast tier and
# the citywide sweep can't drift apart. The fixture's own preconditions are
# pinned separately below (test_merge_pair_fixture_...) so a regenerated
# fixture that loses its cross-tile synthetic data fails loudly instead of
# making all of this pass vacuously -- which is exactly how the collision
# stayed invisible the first time.

MERGE_STORES = [
    "merge_pair_store",
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


def test_merge_pair_fixture_contains_cross_tile_synthetic_data(merge_pair_store):
    """Precondition guard for the fast-tier merge fixture, NOT an invariant:
    the merge-integrity tests above only prove anything if the data they run
    on can actually exhibit a merge bug. A test whose fixture cannot show the
    bug class passes vacuously -- indistinguishable from a real pass, which
    is precisely how the synthetic-id collision survived a suite of 200+
    green tests. If someone regenerates tests/fixtures/merge_pair/ and the
    new tiles come out without synthetic nodes (or without the border
    duplicates that exercise the stitch pass), this fails loudly instead of
    letting the invariants above go quietly blind.

    Floors are deliberately far below the current values (1,417 synthetic
    nodes across the two tiles, 100+ stitches when this was pinned) -- they
    guard against the data class disappearing, not against normal drift."""
    store = merge_pair_store
    synthetic_ids = [node_id for node_id in store._id_to_idx if ":" in node_id]
    tiles = {node_id.split(":", 1)[0] for node_id in synthetic_ids}
    assert len(tiles) >= 2, (
        f"merge fixture has synthetic nodes from only {sorted(tiles)} -- a "
        f"single-tile fixture cannot exhibit a cross-tile merge bug"
    )
    assert len(synthetic_ids) >= 100, (
        f"merge fixture has only {len(synthetic_ids)} synthetic nodes -- too "
        f"few to meaningfully exercise the merge path (had 1,417 when pinned)"
    )
    assert store._stitch_count > 0, (
        "load() stitched nothing -- the fixture no longer contains cross-tile "
        "duplicate synthetic paths, so the stitch pass ran unexercised"
    )


@pytest.mark.citywide
def test_hide_rule_keeps_real_isolated_places_and_hides_junk(citywide_store):
    # The hide rule's two user-reviewed guarantees, pinned on real places
    # (FIXES item 1, 2026-08-17). Liberty Island: ferry-served public
    # paths, curated keep-visible -- clicks there must still route within
    # the island. North Brother Island: a closed bird sanctuary whose 52
    # bare OSM footways were the scraps arc's canonical trap (see HISTORY
    # 2026-08-16) -- it must be neither snappable nor advertised as
    # covered. If Liberty ever fails here, check that its component still
    # matches KEEP_VISIBLE_ISOLATED_PLACES' coordinate; if North Brother
    # ever fails, someone connected it -- verify that's real before
    # trusting it.
    store = citywide_store

    assert store.in_coverage(40.690830, -74.045350) is True  # Liberty
    liberty = store.snap_pair(40.690100, -74.046900, 40.691700, -74.043900)
    assert liberty is not None, "Liberty Island should route within itself"

    assert store.in_coverage(40.801850, -73.898830) is False  # North Brother
    assert store.snap_pair(40.801000, -73.899800, 40.802600, -73.897900) is None
