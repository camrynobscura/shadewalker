"""Regression tests pinned to real bugs found and fixed during development.

Each case asserts the specific property that was wrong, not exact output --
exact numbers would break the moment tree-weight tuning legitimately
changes, but "the route uses the correct street" or "gets rejected instead
of silently mis-snapping" should hold forever.
"""

import pytest

from pipeline import config
from server.graph_store import clamp_shade_monotonic


def test_click_near_a_plaza_snaps_to_the_real_street_not_its_path_network(client):
    """A click ~34m from 3rd Street was snapping 100+m away into a small
    plaza's dense internal path network instead, forcing a detour through
    "unnamed path" / "4th Street Plaza" before heading toward the
    destination. Fixed by snapping to the nearest point on the nearest
    edge instead of the nearest graph node (which only ever exist at
    intersections, and the plaza has far more of those per square meter
    than a normal block)."""
    res = client.get(
        "/route",
        params={
            "from_lat": 40.67363, "from_lon": -73.98425,
            "to_lat": 40.67460, "to_lon": -73.98714,
            "tree_weights": [0],
        },
    )
    assert res.status_code == 200
    segments = res.json()["routes"][0]["properties"]["segments"]
    assert segments[0]["name"] == "3rd Street"
    assert all(s["name"] not in ("unnamed path", "4th Street Plaza") for s in segments)
    assert res.json()["routes"][0]["properties"]["length_m"] < 350  # was 619m before the fix


def test_click_near_2nd_street_actually_reaches_2nd_street(client):
    """A click 5.1m from 2nd Street -- essentially on it -- was snapping
    100.7m away into that same plaza cluster, missing the requested
    destination street entirely."""
    res = client.get(
        "/route",
        params={
            "from_lat": 40.67333, "from_lon": -73.98631,
            "to_lat": 40.67409, "to_lon": -73.98410,
            "tree_weights": [0],
        },
    )
    assert res.status_code == 200
    segments = res.json()["routes"][0]["properties"]["segments"]
    assert segments[-1]["name"] == "2nd Street"
    assert res.json()["routes"][0]["properties"]["length_m"] < 350  # was 571.5m before the fix


def test_a_heavily_shaded_route_never_reads_as_exactly_full_shade(client):
    """A route from 40.68627,-73.99906 to 40.68595,-73.98429 crosses 10
    different Cobble Hill blocks, each of which independently clears
    SHADE_DENSITY_THRESHOLD -- shade_fraction read exactly 1.0 (100%) at
    every tree_weight above 0, which overstates real coverage: a walker
    is still exposed at each of the corners along the way, regardless of
    how tree-lined the blocks bordering them are."""
    res = client.get(
        "/route",
        params={
            "from_lat": 40.68627, "from_lon": -73.99906,
            "to_lat": 40.68595, "to_lon": -73.98429,
            "tree_weights": [5, 15, 40],
        },
    )
    for feature in res.json()["routes"]:
        assert feature["properties"]["shade_fraction"] < 1.0


def test_destination_outside_coverage_is_rejected_not_silently_mis_snapped(client):
    """A destination just past the pilot tile's edge was silently snapping
    ~235m short of where it was actually asked for, instead of telling the
    user their destination isn't covered.

    Destination sits well north of CITY_BBOX entirely (Stage 2's full
    eventual citywide bound), not just past the pilot tile -- a point that
    was merely outside the pilot tile stopped being "outside coverage" the
    moment real Brooklyn tiles loaded locally alongside it (see PLAN.md /
    HISTORY.md's 2026-07-16 entries), so this needs to stay out of coverage
    through every future borough, not just today's.
    """
    res = client.get(
        "/route",
        params={
            "from_lat": 40.67621, "from_lon": -74.00279,
            "to_lat": config.CITY_BBOX.lat_max + 0.5, "to_lon": -73.99,
            "tree_weights": [0],
        },
    )
    assert res.status_code == 422
    assert "coverage" in res.json()["detail"].lower()


# --- Park-canopy regressions (citywide) -------------------------------------
# RED until the park-canopy scoring work lands (see PLAN.md). The two repro
# ODs: at MAX, routes hug a street one block off Central Park instead of the
# park-edge street, because the park's Conservancy-managed trees are absent
# from the Forestry dataset, so the park edge scores near-zero shade. These
# assert against the CLAMPED route -- what the user actually sees -- not raw
# store.route(), which already prefers the park edge but gets reverted by the
# monotonicity clamp. citywide-marked: they need the real Manhattan tiles and
# skip cleanly without them.

WEIGHTS = [0.0, 5.0, 15.0, 40.0]


def _clamped_max_route(store, frm, to, month=7):
    """The route a MAX-shade request actually returns to the user: all four
    presets computed, then clamp_shade_monotonic applied exactly as /route
    does."""
    pair = store.snap_pair(frm[0], frm[1], to[0], to[1])
    assert pair is not None, "endpoints did not snap to a shared component"
    start, end = pair
    routes = [store.route(start, end, tree_weight=w, month=month) for w in WEIGHTS]
    assert all(r is not None for r in routes), "a preset failed to route"
    return clamp_shade_monotonic(routes, WEIGHTS)[-1]


def _length_on(route, street):
    return sum(s["length_m"] for s in route["segments"] if s["name"] == street)


@pytest.mark.citywide
def test_max_shade_prefers_central_park_south_over_the_block_one_south(citywide_store):
    """Central Park's own trees are Conservancy-managed and absent from the
    Forestry dataset, so Central Park South scores near-zero shade; the
    monotonicity clamp then reverts a MAX request to West 58th Street (one
    block south), which -- by our current, park-blind data -- genuinely has
    more street trees. Once the park's canopy is scored, a MAX walker should
    be sent along Central Park South, under the park's edge trees, not a block
    away. Today: 0m on CPS vs ~796m on West 58th."""
    route = _clamped_max_route(citywide_store, (40.76359, -73.97333), (40.76979, -73.98447))
    on_park_edge = _length_on(route, "Central Park South")
    on_block_over = _length_on(route, "West 58th Street")
    assert on_park_edge > on_block_over, (
        f"MAX-shade route favors the block one over: {on_park_edge:.0f}m on Central Park South "
        f"vs {on_block_over:.0f}m on West 58th Street (via {[s['name'] for s in route['segments']]})"
    )


@pytest.mark.citywide
def test_max_shade_prefers_central_park_west_over_the_block_one_west(citywide_store):
    """Same root cause: at MAX the route runs up Columbus Avenue (one block
    west) and only clips ~28m of Central Park West to reach the destination,
    because the park's canopy along CPW isn't in the score. Once park canopy
    is scored, MAX should run substantially along Central Park West itself.
    Today: ~28m on CPW vs the bulk on Columbus Avenue."""
    route = _clamped_max_route(citywide_store, (40.78051, -73.97666), (40.76982, -73.98103))
    on_park_edge = _length_on(route, "Central Park West")
    on_block_over = _length_on(route, "Columbus Avenue")
    assert on_park_edge > on_block_over, (
        f"MAX-shade route favors the block one over: {on_park_edge:.0f}m on Central Park West "
        f"vs {on_block_over:.0f}m on Columbus Avenue (via {[s['name'] for s in route['segments']]})"
    )
