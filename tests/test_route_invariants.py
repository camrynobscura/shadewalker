"""General correctness properties that should hold for any request, not
just the specific historical bugs in test_route_regressions.py."""

import gzip
import json
import math
import random

import pytest

from pipeline import config
from server.graph_store import clamp_shade_monotonic
from tests.conftest import PILOT_FIXTURE

# A real, well-connected pair of points inside the pilot tile -- used
# wherever a test just needs *some* valid route, not a specific bug case.
FROM = {"lat": 40.6800, "lon": -73.9980}
TO = {"lat": 40.6720, "lon": -73.9880}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _has_duplicate_consecutive_points(coords: list[list[float]]) -> bool:
    return any(a == b for a, b in zip(coords, coords[1:]))


def _main_component_nodes() -> dict[str, list[float]]:
    """The pilot fixture's nodes restricted to its largest connected
    component. The fixture carries every component the pipeline keeps --
    143 of them, mostly tiny fragments (a pier, a fenced path) -- and a
    test that picks "the node nearest X" without this filter can land on a
    fragment no route can leave, turning a routing test into a
    connectivity-lottery 422."""
    tile = json.loads(gzip.open(PILOT_FIXTURE, "rt").read())

    neighbors: dict[str, set[str]] = {}
    for edge in tile["edges"]:
        neighbors.setdefault(edge["u"], set()).add(edge["v"])
        neighbors.setdefault(edge["v"], set()).add(edge["u"])

    seen: set[str] = set()
    largest: set[str] = set()
    for start in neighbors:
        if start in seen:
            continue
        component = {start}
        frontier = [start]
        while frontier:
            node = frontier.pop()
            for other in neighbors[node]:
                if other not in component:
                    component.add(other)
                    frontier.append(other)
        seen |= component
        if len(component) > len(largest):
            largest = component

    return {node_id: lonlat for node_id, lonlat in tile["nodes"].items() if node_id in largest}


def _a_real_node_coordinate() -> tuple[float, float]:
    """One real intersection's exact (lat, lon), read straight from the
    pilot tile file -- independent of GraphStore's internal node ordering.

    Pinned to the committed pilot fixture (same reasoning as
    _nearest_real_node_coordinate below): "whichever tile file globs first"
    broke twice once real borough tiles existed locally -- first by reading
    an arbitrary Brooklyn tile, then by picking a node from a tile whose
    whole area gets pruned at load time (Rockaway fragments, unreachable
    from the main network)."""
    lon, lat = next(iter(_main_component_nodes().values()))
    return lat, lon


def _nearest_real_node_coordinate(approx_lat: float, approx_lon: float) -> tuple[float, float]:
    """The real node closest to an approximate point, read straight from
    the pilot tile file -- lets a test target "near this corner" without
    assuming any specific node happens to sit exactly there.

    Pinned to the committed pilot fixture specifically, not "whichever tile
    file exists" -- every caller passes a PILOT_BBOX-derived point and wants
    the pilot tile's own nearest node, not the nearest node in some
    arbitrary other tile (which is exactly what
    `next(config.TILES_DIR.glob("*.json.gz"))` silently broke into the
    moment real Brooklyn data existed alongside it -- it only ever "worked"
    because pilot.json.gz used to be the only file present). Restricted to
    the main component (see _main_component_nodes) so routing tests can't
    land on an unreachable fragment.
    """
    best_lon, best_lat = min(
        _main_component_nodes().values(),
        key=lambda lonlat: _haversine_m(approx_lat, approx_lon, lonlat[1], lonlat[0]),
    )
    return best_lat, best_lon


def test_batching_multiple_weights_matches_requesting_them_individually(client):
    """/route computes every requested tree_weight in one call now (so the
    frontend can cache all four Shade_priority presets from a single
    request instead of re-fetching per click) -- this pins that batching
    many weights together in one request can't accidentally cross-
    contaminate between loop iterations (e.g. shared mutable state) by
    checking it against the same weights requested one at a time."""
    coords = {
        "from_lat": FROM["lat"], "from_lon": FROM["lon"],
        "to_lat": TO["lat"], "to_lon": TO["lon"],
    }
    batched = client.get("/route", params={**coords, "tree_weights": [0, 15]}).json()["routes"]

    solo_0 = client.get("/route", params={**coords, "tree_weights": [0]}).json()["routes"][0]
    solo_15 = client.get("/route", params={**coords, "tree_weights": [15]}).json()["routes"][0]

    assert batched[0]["properties"] == solo_0["properties"]
    assert batched[1]["properties"] == solo_15["properties"]


def test_no_duplicate_consecutive_points_in_route_geometry(graph_store):
    """Guards the class of bug fixed earlier in graph_store.py's route
    stitching: a wrongly-assumed edge-geometry direction produced a
    there-and-back spike, which shows up as an exact duplicate point."""
    for tree_weight in (0, 5, 15, 40):
        start, end = graph_store.snap_pair(FROM["lat"], FROM["lon"], TO["lat"], TO["lon"])
        result = graph_store.route(start, end, tree_weight=tree_weight, month=7)
        assert result is not None
        assert not _has_duplicate_consecutive_points(result["coords"])


def test_tree_count_does_not_drop_for_the_reference_pair(client):
    """A spot check that tree_count holds-or-rises as Shade_priority
    increases FOR THIS ONE well-behaved pair -- NOT a universal invariant.
    Tree count is not guaranteed monotonic in general: a genuinely shadier
    route can pass fewer individual trees (denser but shorter), and the
    server clamps only shade_fraction to be monotonic, never tree_count. The
    real, guaranteed invariant is shade -- see
    test_more_shade_priority_never_reduces_shade_over_many_random_routes and
    the citywide sweep. This pair just happens to stay tree-monotonic in
    every month, so it's kept as a cheap regression signal on one fixed
    walk; if a future fixture regen makes it non-monotonic, replace it
    rather than re-asserting a false universal."""
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": [0, 5, 15, 40],
        },
    )
    counts = [feature["properties"]["tree_count"] for feature in res.json()["routes"]]
    assert counts == sorted(counts)


def test_more_shade_priority_never_reduces_shade_over_many_random_routes(client):
    """The core Shade_priority promise, checked over many routes instead of
    one hand-picked pair: raising the priority (0 -> 5 -> 15 -> 40) must
    never LOWER a route's reported shade_fraction. Each preset's shade must
    be >= the preset before it.

    This is the invariant the monotonic clamp exists to guarantee. Without
    it the property genuinely fails on real geography: the router's cost
    formula rewards density without limit, but shade_fraction saturates it
    at SHADE_SATURATION_DENSITY -- so a higher weight can chain
    already-saturated blocks the stat can't credit any further and report
    less shade than a lower weight's route. Much rarer since the stat went
    continuous (2026-08-17; the old per-edge threshold + crossing deduction
    also violated it through two extra mechanisms, both retired), but the
    saturation mechanism is still real. The
    clamp resolves it by never serving a higher preset a route whose shade a
    lower preset already beat -- all four presets are computed per /route
    call, so it's pure post-processing over routes the request already has.

    Random but SEEDED, so it's deterministic in CI while still sweeping far
    more of the graph than the fixed-pair tests above. Months are pinned
    (not left to the server clock) both for determinism and because the bug
    is worst at partial canopy -- spring and fall, not high summer (measured
    in the pilot tile: ~4% of routes non-monotonic in January vs ~21% in
    April), so a summer-only check would understate it.

    Note it asserts shade only, not tree_count: a genuinely shadier route
    can legitimately pass fewer individual trees (denser-but-shorter), so
    tree_count is not a monotonic invariant and the clamp doesn't force it
    to be -- see test_tree_count_does_not_drop_for_the_reference_pair above,
    which only holds for its one fixed pair, not in general."""
    node_coords = list(_main_component_nodes().values())  # [lon, lat] each
    rng = random.Random(20260724)

    violations = []
    routed = 0
    attempts = 0
    while routed < 60 and attempts < 60 * 40:
        attempts += 1
        a, b = rng.choice(node_coords), rng.choice(node_coords)
        if a == b:
            continue
        routed_this_pair = False
        for month in (4, 7, 10):
            res = client.get(
                "/route",
                params={
                    "from_lat": a[1], "from_lon": a[0],
                    "to_lat": b[1], "to_lon": b[0],
                    "tree_weights": [0, 5, 15, 40],
                    "month": month,
                },
            )
            if res.status_code != 200:
                continue  # a pair that doesn't resolve -- doesn't exercise the invariant
            routed_this_pair = True
            shades = [f["properties"]["shade_fraction"] for f in res.json()["routes"]]
            for i in range(1, len(shades)):
                if shades[i] < shades[i - 1] - 1e-9:
                    violations.append((month, a, b, shades))
                    break
        if routed_this_pair:
            routed += 1

    assert routed >= 40, f"only routed {routed} pairs -- sampling got too sparse to be a real check"
    assert not violations, (
        f"{len(violations)} route(s) where raising Shade_priority reduced shade_fraction "
        f"(should be impossible after the monotonic clamp). First few:\n"
        + "\n".join(
            f"  month={m} from={a[1]:.5f},{a[0]:.5f} to={b[1]:.5f},{b[0]:.5f} "
            f"shades={[round(x, 3) for x in s]}"
            for m, a, b, s in violations[:5]
        )
    )


# The clamp that makes the invariant above hold, unit-tested directly on
# synthetic route dicts -- fast, deterministic, and covering the edge cases
# a random route sweep might not happen to hit.

def _fake_route(shade: float) -> dict:
    """A minimal stand-in for a GraphStore.route() result. The clamp only
    reads shade_fraction and swaps whole dicts, so identity (`is`) lets a
    test assert exactly WHICH route object it fell back to."""
    return {"shade_fraction": shade, "marker": object()}


def test_clamp_replaces_a_dropping_preset_with_the_shadier_lower_one():
    routes = [_fake_route(0.5), _fake_route(0.3), _fake_route(0.4), _fake_route(0.2)]
    clamped = clamp_shade_monotonic(routes, [0.0, 5.0, 15.0, 40.0])
    assert [r["shade_fraction"] for r in clamped] == [0.5, 0.5, 0.5, 0.5]
    # each clamped-down slot is the SAME object it fell back to (whole-route swap)
    assert clamped[1] is routes[0] and clamped[2] is routes[0] and clamped[3] is routes[0]


def test_clamp_leaves_an_already_rising_sequence_untouched():
    routes = [_fake_route(0.1), _fake_route(0.2), _fake_route(0.2), _fake_route(0.4)]
    clamped = clamp_shade_monotonic(routes, [0.0, 5.0, 15.0, 40.0])
    assert all(clamped[i] is routes[i] for i in range(len(routes)))  # same objects, same order


def test_clamp_only_falls_back_as_far_as_needed():
    # rises to 0.5, dips at 15, then genuinely beats it at 40
    routes = [_fake_route(0.1), _fake_route(0.5), _fake_route(0.3), _fake_route(0.6)]
    clamped = clamp_shade_monotonic(routes, [0.0, 5.0, 15.0, 40.0])
    assert [r["shade_fraction"] for r in clamped] == [0.1, 0.5, 0.5, 0.6]
    assert clamped[2] is routes[1]  # fell back to the 0.5 route, not all the way to NONE
    assert clamped[3] is routes[3]  # kept its own -- it's genuinely shadier


def test_clamp_handles_a_single_weight():
    routes = [_fake_route(0.3)]
    assert clamp_shade_monotonic(routes, [15.0]) == routes


def test_clamp_is_robust_to_weights_requested_out_of_order():
    # Requested descending; the clamp must reason in ascending-weight space
    # and return results in the caller's original positions.
    weights = [40.0, 0.0, 15.0, 5.0]
    routes = [_fake_route(0.2), _fake_route(0.5), _fake_route(0.4), _fake_route(0.3)]
    clamped = clamp_shade_monotonic(routes, weights)
    by_ascending_weight = [clamped[i]["shade_fraction"] for i in sorted(range(4), key=lambda i: weights[i])]
    assert by_ascending_weight == sorted(by_ascending_weight)  # non-decreasing in weight
    assert by_ascending_weight[0] == 0.5  # NONE is shadiest here, so all clamp up to it


def test_route_length_is_never_shorter_than_the_straight_line_distance(client):
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": [15],
        },
    )
    airline_m = _haversine_m(FROM["lat"], FROM["lon"], TO["lat"], TO["lon"])
    assert res.json()["routes"][0]["properties"]["length_m"] >= airline_m


def test_snapping_onto_a_real_intersection_is_essentially_exact(graph_store):
    """A point placed exactly on a known graph node should snap right back
    to (almost) that same point -- confirms the projection math doesn't
    introduce meaningful drift for the simplest possible case."""
    lat, lon = _a_real_node_coordinate()
    edge, _ = graph_store._nearest_edge(lat, lon)
    snap = graph_store._snap_point_for_edge(lat, lon, edge)
    assert abs(snap.point[0] - lon) < 1e-6
    assert abs(snap.point[1] - lat) < 1e-6


def test_tree_weight_out_of_range_is_rejected(client):
    """A negative tree_weight can push cost = length / (1 + tree_weight *
    density) toward or below zero on dense edges -- breaking Dijkstra's
    non-negative-edge-weight assumption instead of just erroring. The
    server rejects anything outside the frontend's own 0-40 range -- for
    any weight in the batch, not just the first one checked."""
    for tree_weight in (-5, 1000):
        res = client.get(
            "/route",
            params={
                "from_lat": FROM["lat"], "from_lon": FROM["lon"],
                "to_lat": TO["lat"], "to_lon": TO["lon"],
                "tree_weights": [15, tree_weight],
            },
        )
        assert res.status_code == 400
        assert "tree_weight" in res.json()["detail"]


def test_oversized_tree_weights_list_is_rejected(client):
    """Each requested weight costs a real ~13ms Dijkstra pass on a worker
    thread, so an uncapped list is a denial-of-service hole once public
    (FIXES item 7): one request with hundreds of weights blocks a worker
    for seconds. The frontend sends 4; the cap is 8 (config.
    MAX_TREE_WEIGHTS_PER_REQUEST). Exactly-at-cap must still work -- the
    guard is about unbounded lists, not about breaking legitimate
    experimentation."""
    at_cap = list(range(config.MAX_TREE_WEIGHTS_PER_REQUEST))
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": at_cap,
        },
    )
    assert res.status_code == 200

    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": at_cap + [15],
        },
    )
    assert res.status_code == 400
    assert "at most" in res.json()["detail"]


def test_two_points_on_the_same_block_route_directly_not_via_a_corner(graph_store):
    """Without the same-edge "direct" candidate in route(), two nearby
    clicks on one block would be forced through a real intersection and
    back instead of routing straight between them."""
    # The longest edge in the tile, so there's plenty of room to pick two
    # clearly-separated points that both still snap back to it.
    edge = max(range(len(graph_store._length)), key=lambda e: graph_store._length[e])
    coords = graph_store._edge_coords(edge)
    lon_a, lat_a = coords[len(coords) // 4]
    lon_b, lat_b = coords[3 * len(coords) // 4]

    start, end = graph_store.snap_pair(lat_a, lon_a, lat_b, lon_b)
    assert start.edge == end.edge  # sanity check on the test's own setup

    result = graph_store.route(start, end, tree_weight=0, month=7)
    airline_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
    # A direct hop along one edge should be short -- nowhere near the cost
    # of detouring out to an intersection and back.
    assert result["length_m"] < airline_m * 3


def test_coverage_polygon_traces_the_street_network_not_its_bounding_box(client):
    """/coverage serves a MultiPolygon tracing the buffered street network,
    not a min/max rectangle -- with Brooklyn-sized data a rectangle claimed
    water and Lower Manhattan as clickable area that /route would then
    reject. The drawn boundary must be one users can trust.

    Checks whichever piece actually contains the known-routable FROM/TO
    points, not just coordinates[0]: GraphStore.load() keeps every
    component now (not just the largest), so with real, non-pilot data
    loaded there can be many disjoint pieces in no particular order --
    which one a real place lands in isn't a fixed index."""
    from shapely.geometry import LinearRing, Point as ShapelyPoint, Polygon

    polygons = client.get("/coverage").json()["geometry"]["coordinates"]
    from_point = ShapelyPoint(FROM["lon"], FROM["lat"])
    to_point = ShapelyPoint(TO["lon"], TO["lat"])

    matches = [polygon[0] for polygon in polygons if Polygon(polygon[0]).contains(from_point)]
    assert len(matches) == 1  # disjoint pieces -- a real point belongs to exactly one
    ring = matches[0]

    assert ring[0] == ring[-1]  # closed GeoJSON ring
    assert len(ring) >= 5

    poly = Polygon(ring)
    assert poly.is_valid
    # Winding is part of the frontend contract: MapView punches its
    # map-dimming holes by reversing these rings, which assumes CCW.
    assert LinearRing(ring).is_ccw
    # FROM and TO are both real, known-routable Carroll Gardens points --
    # they must land in the very same piece.
    assert poly.contains(to_point)
    # A genuinely traced outline is strictly smaller than its own bbox
    # (equality would mean it IS the rectangle).
    lon_min, lat_min, lon_max, lat_max = poly.bounds
    assert poly.area < (lon_max - lon_min) * (lat_max - lat_min)


# Every test above this point routes between one of a small handful of
# fixed, hand-picked points. The tests below cover geometry shapes those
# fixed points never touch -- diverse real coordinates, not just the ones
# that happened to come from a bug report.


def test_route_spans_the_full_pilot_tile_between_real_corners(client):
    """The longest realistic route in this tile -- stresses multi-edge
    stitching across many blocks, unlike every other test's one reused
    nearby pair."""
    lat_a, lon_a = _nearest_real_node_coordinate(config.PILOT_BBOX.lat_min, config.PILOT_BBOX.lon_min)
    lat_b, lon_b = _nearest_real_node_coordinate(config.PILOT_BBOX.lat_max, config.PILOT_BBOX.lon_max)

    res = client.get(
        "/route",
        params={
            "from_lat": lat_a, "from_lon": lon_a,
            "to_lat": lat_b, "to_lon": lon_b,
            "tree_weights": [15],
        },
    )
    assert res.status_code == 200
    feature = res.json()["routes"][0]
    airline_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
    assert feature["properties"]["length_m"] >= airline_m
    assert not _has_duplicate_consecutive_points(feature["geometry"]["coordinates"])


def test_two_points_a_few_meters_apart_still_route_successfully(client):
    """A near-degenerate request -- two clicks close enough together that
    they might snap to the same edge or to adjacent ones. Should still
    return a short, sane route rather than erroring or looping."""
    lat_a, lon_a = FROM["lat"], FROM["lon"]
    lat_b, lon_b = lat_a + 0.000045, lon_a  # ~5 m north

    res = client.get(
        "/route",
        params={
            "from_lat": lat_a, "from_lon": lon_a,
            "to_lat": lat_b, "to_lon": lon_b,
            "tree_weights": [15],
        },
    )
    assert res.status_code == 200
    # Nowhere near the cost of a real detour -- a block is 80-100 m, so
    # even one bad corner-and-back shouldn't reach this.
    assert res.json()["routes"][0]["properties"]["length_m"] < 300


def test_a_point_near_the_coverage_boundary_still_routes(client):
    """Distinct from the existing far-outside rejection test: a point near
    the *edge* of real coverage, not deep inside it, should still resolve
    to a real route instead of being (wrongly) rejected."""
    lat_edge, lon_edge = _nearest_real_node_coordinate(
        config.PILOT_BBOX.lat_min, (config.PILOT_BBOX.lon_min + config.PILOT_BBOX.lon_max) / 2
    )

    res = client.get(
        "/route",
        params={
            "from_lat": lat_edge, "from_lon": lon_edge,
            "to_lat": FROM["lat"], "to_lon": FROM["lon"],
            "tree_weights": [15],
        },
    )
    assert res.status_code == 200


def test_a_self_loop_edge_routes_without_error(graph_store):
    """Some real streets in this tile loop back to their own start node
    (u == v for that edge) -- confirmed 6 such edges exist in the pilot
    tile. SnapPoint's dist_to_u_m/dist_to_v_m are deliberately derived
    from the projection fraction rather than by re-projecting node
    coordinates, specifically so this case isn't ambiguous about "which
    way around the loop" -- this is the one test that actually exercises
    it, using the same fractional-point technique as the same-block test
    above."""
    self_loop_edges = [
        e
        for e in range(len(graph_store._length))
        if graph_store._graph.es[e].tuple[0] == graph_store._graph.es[e].tuple[1]
    ]
    assert self_loop_edges, "expected at least one self-loop edge in the pilot tile"

    edge = self_loop_edges[0]
    coords = graph_store._edge_coords(edge)
    lon_a, lat_a = coords[len(coords) // 4]
    lon_b, lat_b = coords[3 * len(coords) // 4]

    start, end = graph_store.snap_pair(lat_a, lon_a, lat_b, lon_b)
    assert start.edge == edge  # sanity check on the test's own setup
    assert end.edge == edge

    result = graph_store.route(start, end, tree_weight=0, month=7)
    assert result is not None
    assert not _has_duplicate_consecutive_points(result["coords"])
    # A quarter-to-three-quarters hop along one loop shouldn't blow up to
    # anywhere near the full loop length.
    assert result["length_m"] < graph_store._length[edge]


@pytest.mark.parametrize("tree_weight", [0, 5, 15, 40])
def test_shade_fraction_is_always_between_zero_and_one(client, tree_weight):
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": [tree_weight],
        },
    )
    assert 0.0 <= res.json()["routes"][0]["properties"]["shade_fraction"] <= 1.0


def test_shade_fraction_carries_signal_tree_count_cannot(client):
    """Why shade_fraction exists: tree_count alone can't say how much of a
    walk is actually under cover. These two walks, re-derived against the
    14m-buffer fixture (2026-07-23), count exactly the same 205 trees yet
    differ hugely in shaded length -- 19.6% vs 75.6%. A regression that
    made shade_fraction a function of tree_count, or broke its wiring,
    can't get both right.

    (This test originally pinned a single OD where MED and MAX plateaued
    on tree_count while shade_fraction still rose. At the 14m buffer that
    phenomenon no longer occurs anywhere in the pilot tile -- whenever MED
    and MAX plateau on tree_count now, they're taking the identical route
    -- so the same intent is pinned across two walks instead.)"""
    walk_leafy_park_slope = {"from_lat": 40.67842, "from_lon": -73.97900,
                             "to_lat": 40.68520, "to_lon": -73.98865}
    walk_industrial_red_hook = {"from_lat": 40.67243, "from_lon": -74.00973,
                                "to_lat": 40.68845, "to_lon": -74.00118}

    shades = {}
    counts = {}
    for label, walk in (("leafy", walk_leafy_park_slope),
                        ("industrial", walk_industrial_red_hook)):
        props = client.get(
            "/route", params={**walk, "tree_weights": [15]},
        ).json()["routes"][0]["properties"]
        shades[label] = props["shade_fraction"]
        counts[label] = props["tree_count"]

    assert counts["leafy"] == counts["industrial"]  # identical tree_count...
    # ...but wildly different real coverage (measured 0.756 vs 0.196 under
    # the original binary definition; the margin is loose so saturation
    # recalibrations -- like 2026-08-17's continuous redesign -- don't
    # break it, while a wiring regression still does).
    assert shades["leafy"] > shades["industrial"] + 0.3


def test_shade_fraction_gives_partial_credit_below_saturation(client):
    """Pins a lightly-shaded route's exact shade_fraction under the
    continuous definition (FIXES item 2, 2026-08-17): every edge
    contributes min(density / SHADE_SATURATION_DENSITY, 1) of its length,
    so blocks below saturation earn PARTIAL credit instead of the old
    all-or-nothing threshold's zero. This exact pin guards two things at
    once: the saturation constant (a silent recalibration moves this
    number) and the length-weighting wiring (a regression back to
    per-edge yes/no snaps it to the old binary reading -- this same pair
    read 0.258 under the retired threshold+crossing-gap definition,
    re-derived to 0.318 when the stat went continuous)."""
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weights": [15],
        },
    )
    assert res.json()["routes"][0]["properties"]["shade_fraction"] == 0.318


def test_every_edge_geometry_starts_and_ends_at_its_own_nodes(graph_store):
    """Pins the packed-geometry alignment (graph_store._coord_buf /
    _coord_offsets, read via _edge_coords): every edge's slice of the
    flat coordinate buffer must begin and end at that edge's own two
    endpoint nodes -- in either order, since stored geometry direction
    isn't guaranteed to run u->v. An off-by-one in the packing (edge 7's
    offsets pointing at edge 8's points) would otherwise only surface as
    confusing downstream failures (weird route shapes, snap mismatches);
    this names the actual problem. Iterates every loaded edge, so once
    Stage 2 adds more tiles it also validates the packing across the
    multi-tile merge + dedupe path, which no test exercises today.

    Tolerance: nodes and geometry are both exported rounded to 6
    decimals (pipeline/export.py), so matching endpoints agree to ~1e-6
    degrees; a misaligned edge's endpoints would be whole intersections
    (>>0.1m) away.

    Pilot-scoped by fixture: the citywide sibling that actually exercises
    the multi-tile merge this docstring's last sentence used to promise
    lives in test_citywide_invariants.py. A synthetic-id merge bug can't
    appear in a single-tile fixture, so this one alone is not that guard --
    see FIXES.md."""

    def _matches(point, node) -> bool:
        return abs(point[0] - node[0]) <= 1e-6 and abs(point[1] - node[1]) <= 1e-6

    for edge in range(len(graph_store._length)):
        coords = graph_store._edge_coords(edge)
        assert len(coords) >= 2, f"edge {edge}: fewer than 2 geometry points"

        u, v = graph_store._graph.es[edge].tuple
        node_u = graph_store._node_lonlat[u]
        node_v = graph_store._node_lonlat[v]
        runs_u_to_v = _matches(coords[0], node_u) and _matches(coords[-1], node_v)
        runs_v_to_u = _matches(coords[0], node_v) and _matches(coords[-1], node_u)
        assert runs_u_to_v or runs_v_to_u, (
            f"edge {edge}: geometry endpoints {coords[0]}..{coords[-1]} don't "
            f"land on its nodes {node_u} / {node_v} -- packed buffer misaligned?"
        )


# --- Route length can only shrink as the graph grows (citywide) -------------
# A structural invariant, not a bug pin: every fix in fix-interior-park-paths
# only ADMITS previously-excluded ways, and adding edges to a graph can never
# lengthen a shortest path between two fixed nodes. Verified by hand for this
# branch (route_invariant_check.py: 360 routes across Central Park, Prospect
# Park, and a park-dense control area, zero got longer) -- this pins that
# property going forward instead of re-deriving it by hand for the next
# filter change.
#
# Golden-baseline, not a live before/after diff: at test time there is only
# one graph, so the "before" side is this hardcoded table, captured from real
# production data on 2026-07-30. A future run whose shortest path exceeds its
# baseline either found a genuine regression (an edge/way disappeared) or
# made a deliberate change that legitimately shortens routes further -- in
# the latter case, re-run gen_golden_baseline.py-style and update the table,
# the same way test_pipeline_config.py's golden grid gets updated: "last",
# after confirming the change is intentional.
#
# Node ids are real OSM ids (stable across re-fetches -- fetching more ways
# doesn't renumber existing nodes), pinned to two of the areas most affected
# by this branch's filters. Distances are graph-shortest-path length in
# meters (tree_weight=0 cost equals raw length), not straight-line.
GOLDEN_ROUTE_LENGTHS_M = [
    ("10173346255", "7792403895", 1093.2),  # Central Park
    ("12859262636", "10168029011", 4611.8),  # Central Park
    ("422438842", "10160790441", 2348.9),  # Central Park
    ("3809536797", "10171089657", 1696.9),  # Central Park
    ("10058808476", "3875114041", 1395.9),  # Central Park
    ("7424069298", "6254630385", 1741.4),  # Central Park
    ("9634645806", "4256322566", 1315.8),  # Central Park
    ("8782897315", "7094731933", 1032.5),  # Central Park
    ("10597902945", "9897810834", 2775.4),  # Central Park
    ("42427036", "4254492560", 2176.6),  # Central Park
    ("10172953889", "10125139627", 2211.4),  # Central Park
    ("6746991446", "4254503840", 2818.9),  # Central Park
    ("3998699937", "6267096939", 4608.5),  # Central Park
    ("10042613562", "10061835802", 839.4),  # Central Park
    ("4254513238", "42424851", 1456.8),  # Central Park
    ("2645474860", "10617167151", 320.0),  # Prospect Park
    ("10709356781", "8218642199", 983.2),  # Prospect Park
    ("42487594", "6629833466", 1256.4),  # Prospect Park
    ("4877126282", "7263499409", 809.0),  # Prospect Park
    ("8025743455", "10291728481", 1357.0),  # Prospect Park
    ("9876563345", "10035103116", 563.4),  # Prospect Park
    ("42478511", "11350040314", 2048.3),  # Prospect Park
    ("10642847745", "6913843080", 1439.3),  # Prospect Park
    ("9370735632", "10596479306", 1513.8),  # Prospect Park
    ("755897194", "42481443", 2326.6),  # Prospect Park
    ("4575701548", "758811765", 3310.7),  # Prospect Park
    ("10556968370", "541998127", 948.3),  # Prospect Park
    ("6604177794", "2377594454", 3022.1),  # Prospect Park
    ("42481443", "10742553722", 1829.9),  # Prospect Park
    ("9634487166", "2645335202", 1473.7),  # Prospect Park
]

# A tiny amount of slack for floating-point accumulation in path summation,
# not for tolerating real regressions.
_SLACK_M = 0.5


@pytest.mark.citywide
def test_golden_route_lengths_never_get_longer(citywide_store):
    """See module comment above the GOLDEN_ROUTE_LENGTHS_M table."""
    checked = 0
    for node_a, node_b, baseline_m in GOLDEN_ROUTE_LENGTHS_M:
        if node_a not in citywide_store._id_to_idx or node_b not in citywide_store._id_to_idx:
            continue  # node pruned by a real OSM edit -- not this invariant's concern
        idx_a = citywide_store._id_to_idx[node_a]
        idx_b = citywide_store._id_to_idx[node_b]
        current_m = citywide_store._graph.distances(
            source=[idx_a], target=[idx_b], weights=citywide_store._length
        )[0][0]
        assert current_m != float("inf"), (
            f"{node_a} -> {node_b} used to route ({baseline_m}m) and is now unreachable "
            "-- a fetch/filter change disconnected it"
        )
        assert current_m <= baseline_m + _SLACK_M, (
            f"{node_a} -> {node_b} got LONGER: {baseline_m}m -> {current_m:.1f}m. "
            "Adding ways should never lengthen a shortest path -- investigate before "
            "assuming this baseline just needs bumping."
        )
        checked += 1

    assert checked >= len(GOLDEN_ROUTE_LENGTHS_M) - 3, (
        f"only {checked}/{len(GOLDEN_ROUTE_LENGTHS_M)} golden pairs still resolve -- "
        "too many nodes gone to trust this run; investigate rather than re-baselining blind"
    )
