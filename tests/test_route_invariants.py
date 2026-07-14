"""General correctness properties that should hold for any request, not
just the specific historical bugs in test_route_regressions.py."""

import gzip
import json
import math

import pytest

from pipeline import config

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


def _a_real_node_coordinate() -> tuple[float, float]:
    """One real intersection's exact (lat, lon), read straight from the
    tile file -- independent of GraphStore's internal node ordering."""
    tile_path = next(config.TILES_DIR.glob("*.json.gz"))
    tile = json.loads(gzip.open(tile_path, "rt").read())
    lon, lat = next(iter(tile["nodes"].values()))
    return lat, lon


def _nearest_real_node_coordinate(approx_lat: float, approx_lon: float) -> tuple[float, float]:
    """The real node closest to an approximate point, read straight from
    the tile file -- lets a test target "near this corner" without
    assuming any specific node happens to sit exactly there."""
    tile_path = next(config.TILES_DIR.glob("*.json.gz"))
    tile = json.loads(gzip.open(tile_path, "rt").read())
    best_lon, best_lat = min(
        tile["nodes"].values(),
        key=lambda lonlat: _haversine_m(approx_lat, approx_lon, lonlat[1], lonlat[0]),
    )
    return best_lat, best_lon


def test_tree_weight_zero_reproduces_the_shortest_path(client):
    """The project plan's own definition of done for the routing server:
    tree_weight=0 must reproduce the plain shortest path exactly, since
    "shortest" is always computed the same way internally regardless of
    what tree_weight was requested."""
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weight": 0,
        },
    )
    body = res.json()
    green, shortest = body["green"]["properties"], body["shortest"]["properties"]
    assert green["length_m"] == shortest["length_m"]
    assert green["tree_count"] == shortest["tree_count"]
    assert green["segments"] == shortest["segments"]


def test_no_duplicate_consecutive_points_in_route_geometry(graph_store):
    """Guards the class of bug fixed earlier in graph_store.py's route
    stitching: a wrongly-assumed edge-geometry direction produced a
    there-and-back spike, which shows up as an exact duplicate point."""
    for tree_weight in (0, 5, 15, 40):
        start = graph_store.snap_to_edge(FROM["lat"], FROM["lon"])
        end = graph_store.snap_to_edge(TO["lat"], TO["lon"])
        result = graph_store.route(start, end, tree_weight=tree_weight, month=7)
        assert result is not None
        assert not _has_duplicate_consecutive_points(result["coords"])


def test_more_shade_priority_never_finds_fewer_trees(client):
    """A higher shade priority should never do worse than a lower one for
    the same walk -- the router is strictly seeking more trees as the
    weight increases, so tree_count should only go up or stay flat."""
    counts = []
    for tree_weight in (0, 5, 15, 40):
        res = client.get(
            "/route",
            params={
                "from_lat": FROM["lat"], "from_lon": FROM["lon"],
                "to_lat": TO["lat"], "to_lon": TO["lon"],
                "tree_weight": tree_weight,
            },
        )
        counts.append(res.json()["green"]["properties"]["tree_count"])
    assert counts == sorted(counts)


def test_route_length_is_never_shorter_than_the_straight_line_distance(client):
    res = client.get(
        "/route",
        params={
            "from_lat": FROM["lat"], "from_lon": FROM["lon"],
            "to_lat": TO["lat"], "to_lon": TO["lon"],
            "tree_weight": 15,
        },
    )
    airline_m = _haversine_m(FROM["lat"], FROM["lon"], TO["lat"], TO["lon"])
    assert res.json()["green"]["properties"]["length_m"] >= airline_m


def test_snapping_onto_a_real_intersection_is_essentially_exact(graph_store):
    """A point placed exactly on a known graph node should snap right back
    to (almost) that same point -- confirms the projection math doesn't
    introduce meaningful drift for the simplest possible case."""
    lat, lon = _a_real_node_coordinate()
    snap = graph_store.snap_to_edge(lat, lon)
    assert abs(snap.point[0] - lon) < 1e-6
    assert abs(snap.point[1] - lat) < 1e-6


def test_tree_weight_out_of_range_is_rejected(client):
    """A negative tree_weight can push cost = length / (1 + tree_weight *
    density) toward or below zero on dense edges -- breaking Dijkstra's
    non-negative-edge-weight assumption instead of just erroring. The
    server rejects anything outside the frontend's own 0-40 range."""
    for tree_weight in (-5, 1000):
        res = client.get(
            "/route",
            params={
                "from_lat": FROM["lat"], "from_lon": FROM["lon"],
                "to_lat": TO["lat"], "to_lon": TO["lon"],
                "tree_weight": tree_weight,
            },
        )
        assert res.status_code == 400
        assert "tree_weight" in res.json()["detail"]


def test_two_points_on_the_same_block_route_directly_not_via_a_corner(graph_store):
    """Without the same-edge "direct" candidate in route(), two nearby
    clicks on one block would be forced through a real intersection and
    back instead of routing straight between them."""
    # The longest edge in the tile, so there's plenty of room to pick two
    # clearly-separated points that both still snap back to it.
    edge = max(range(len(graph_store._coords)), key=lambda e: graph_store._length[e])
    coords = graph_store._coords[edge]
    lon_a, lat_a = coords[len(coords) // 4]
    lon_b, lat_b = coords[3 * len(coords) // 4]

    start = graph_store.snap_to_edge(lat_a, lon_a)
    end = graph_store.snap_to_edge(lat_b, lon_b)
    assert start.edge == end.edge  # sanity check on the test's own setup

    result = graph_store.route(start, end, tree_weight=0, month=7)
    airline_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
    # A direct hop along one edge should be short -- nowhere near the cost
    # of detouring out to an intersection and back.
    assert result["length_m"] < airline_m * 3


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
            "tree_weight": 15,
        },
    )
    assert res.status_code == 200
    body = res.json()
    airline_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
    assert body["green"]["properties"]["length_m"] >= airline_m
    assert not _has_duplicate_consecutive_points(body["green"]["geometry"]["coordinates"])


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
            "tree_weight": 15,
        },
    )
    assert res.status_code == 200
    # Nowhere near the cost of a real detour -- a block is 80-100 m, so
    # even one bad corner-and-back shouldn't reach this.
    assert res.json()["green"]["properties"]["length_m"] < 300


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
            "tree_weight": 15,
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
        for e in range(len(graph_store._coords))
        if graph_store._graph.es[e].tuple[0] == graph_store._graph.es[e].tuple[1]
    ]
    assert self_loop_edges, "expected at least one self-loop edge in the pilot tile"

    edge = self_loop_edges[0]
    coords = graph_store._coords[edge]
    lon_a, lat_a = coords[len(coords) // 4]
    lon_b, lat_b = coords[3 * len(coords) // 4]

    start = graph_store.snap_to_edge(lat_a, lon_a)
    end = graph_store.snap_to_edge(lat_b, lon_b)
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
            "tree_weight": tree_weight,
        },
    )
    assert 0.0 <= res.json()["green"]["properties"]["shade_fraction"] <= 1.0


def test_shade_fraction_can_rise_even_when_tree_count_plateaus(client):
    """The exact real case that motivated shade_fraction: for this walk,
    MED (w=15) and MAX (w=40) land on the same tree_count (397) -- raising
    tree_weight bought no extra trees at all -- yet MAX spends noticeably
    more of the walk actually under cover (verified directly against the
    server at SHADE_DENSITY_THRESHOLD=0.025: 73.6% shaded at w=15 vs 78.2%
    at w=40). tree_count alone can't show that difference; shade_fraction
    should. The margin below is intentionally looser than the measured
    4.6-point gap -- tight enough to catch a real regression, loose enough
    to not break every time the threshold gets recalibrated."""
    from_lat, from_lon = 40.68354, -74.00009
    to_lat, to_lon = 40.66674, -73.98442

    med = client.get(
        "/route",
        params={
            "from_lat": from_lat, "from_lon": from_lon,
            "to_lat": to_lat, "to_lon": to_lon,
            "tree_weight": 15,
        },
    ).json()["green"]["properties"]
    max_ = client.get(
        "/route",
        params={
            "from_lat": from_lat, "from_lon": from_lon,
            "to_lat": to_lat, "to_lon": to_lon,
            "tree_weight": 40,
        },
    ).json()["green"]["properties"]

    assert med["tree_count"] == max_["tree_count"]  # the original plateau
    assert max_["shade_fraction"] > med["shade_fraction"] + 0.03  # but a real shade gain
