"""Regression tests pinned to real bugs found and fixed during development.

Each case asserts the specific property that was wrong, not exact output --
exact numbers would break the moment tree-weight tuning legitimately
changes, but "the route uses the correct street" or "gets rejected instead
of silently mis-snapping" should hold forever.
"""

import random

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


# --- The actual reported bug: fastest route wouldn't enter the park ---------
# The user's own report: 25 Central Park W -> 18 E 60th St, compared against
# Google Maps (which offers both an along-the-street route AND a through-the-
# park route at the same distance). Our app sent this route along Central
# Park South even at NONE shade priority -- i.e. the FASTEST route wouldn't
# use the park at all, which is what led to finding the three sidewalk/
# bridleway filter bugs above (7c46bca, a14ffa8, 94ffb29). Unlike the two
# tests above, this isn't about shade preference -- tree_weight=0 is the
# plain shortest path, so this is a pure connectivity/graph-content check.


@pytest.mark.citywide
def test_fastest_route_from_the_reported_bug_now_enters_the_park(citywide_store):
    """Pinned to the exact coordinates from the original bug report, at
    tree_weight=0 (NONE priority, the "even at NONE" complaint) -- not the
    clamped multi-preset route, since NONE is never touched by
    clamp_shade_monotonic (nothing is lower than it to fall back to).

    Confirmed against the live restarted server 2026-07-31: this route now
    spends 808 of its 1292m on "Central Park Outer Loop", vs 0m on "Central
    Park South". Asserting the park path beats the street it used to run
    along instead, not an exact length -- exact numbers will legitimately
    shift as the graph keeps changing."""
    pair = citywide_store.snap_pair(40.77008, -73.98069, 40.76426, -73.97086)
    assert pair is not None, "endpoints did not snap to a shared component"
    start, end = pair
    route = citywide_store.route(start, end, tree_weight=0.0, month=7)
    assert route is not None, "the reported route no longer resolves at all"

    on_park_path = _length_on(route, "Central Park Outer Loop")
    on_the_street_it_used_to_take = _length_on(route, "Central Park South")
    assert on_park_path > on_the_street_it_used_to_take, (
        f"the fastest route from the original bug report isn't using the park: "
        f"{on_park_path:.0f}m on Central Park Outer Loop vs "
        f"{on_the_street_it_used_to_take:.0f}m on Central Park South "
        f"(via {[s['name'] for s in route['segments']]})"
    )


# --- Ozone Park: a real gap between two near-coincident nodes ---------------
# RED until the KNOWN_NODE_GAPS batch from the citywide audit lands (see
# FIXES.md). Found 2026-07-31 via the OSRM/Valhalla/BRouter sanity check:
# this NONE-priority route came back 1239.4m, while OSRM/Valhalla/BRouter
# all agreed with each other within ~5m around 1156-1161m. Root-caused to
# two real, distinct OSM nodes only ~15m apart in reality that needed a
# ~350m round trip to connect, because nothing in the graph linked them
# directly -- not a missing street, not a park/cemetery exclusion, just two
# nearby points that were never wired together.


@pytest.mark.citywide
def test_ozone_park_route_no_longer_detours_around_a_disconnected_stub(citywide_store):
    """Pinned to the exact coordinates from the 2026-07-31 sanity-check
    finding, at tree_weight=0 (NONE priority, the plain shortest path --
    this is a pure connectivity check, not a shade-preference one).

    1180m leaves real slack above the ~1156-1161m three-engine consensus
    for legitimate path-choice variance, while staying well below the
    1239.4m the disconnected stub was forcing before the fix."""
    pair = citywide_store.snap_pair(40.67346, -73.85500, 40.67845, -73.84710)
    assert pair is not None, "endpoints did not snap to a shared component"
    start, end = pair
    route = citywide_store.route(start, end, tree_weight=0.0, month=7)
    assert route is not None, "the Ozone Park route no longer resolves at all"

    assert route["length_m"] < 1180.0, (
        f"Ozone Park route still detouring around the disconnected stub: "
        f"{route['length_m']:.1f}m (external engines agree around 1156-1161m) "
        f"via {[s['name'] for s in route['segments']]}"
    )


# --- Bronx (Melrose/Mott Haven): a stale, incomplete tile fetch -------------
# Found via the same OSRM/Valhalla/BRouter sanity check as Ozone Park above:
# this NONE-priority route came back 925.9m, while all three engines agreed
# with each other within 2m around 618-620m. Root cause was different from
# Ozone Park's, though -- not a missing KNOWN_NODE_GAPS entry, but osmnx's
# own HTTP-response cache (separate from and invisible to
# GRAPH_CACHE_VERSION) silently freezing a one-off incomplete Overpass
# response for tile r18c14 forever: bumping our own cache version and
# re-fetching kept replaying that same stale answer no matter how many
# times it ran, since osmnx's cache doesn't know our version changed.
# Fixed two ways: pipeline/fetch/streets.py now disables osmnx's cache
# entirely (ox.settings.use_cache = False), and tile r18c14 was re-fetched
# for real under the fix.


@pytest.mark.citywide
def test_bronx_route_no_longer_detours_around_a_stale_incomplete_fetch(citywide_store):
    """Pinned to the exact coordinates from the reported bug, at
    tree_weight=0 (NONE priority -- a pure connectivity check, not a
    shade-preference one).

    650m leaves real slack above the ~618-620m three-engine consensus for
    legitimate path-choice variance, while staying well below the 925.9m
    the stale/incomplete fetch was forcing before the fix."""
    pair = citywide_store.snap_pair(40.8105, -73.9051, 40.8127, -73.9104)
    assert pair is not None, "endpoints did not snap to a shared component"
    start, end = pair
    route = citywide_store.route(start, end, tree_weight=0.0, month=7)
    assert route is not None, "the Bronx route no longer resolves at all"

    assert route["length_m"] < 650.0, (
        f"Bronx route still detouring around the stale/incomplete fetch: "
        f"{route['length_m']:.1f}m (external engines agree around 618-620m) "
        f"via {[s['name'] for s in route['segments']]}"
    )


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


# --- Route-description regression (citywide) ---------------------------------
# `fix-interior-park-paths` admits unnamed park sidewalks (ANY_SIDEWALK_FILTER,
# a14ffa8) so the router can enter parks at all -- but every one of those
# edges reports as "unnamed path" in turn-by-turn directions, which feed the
# frontend's aria-live region (a WCAG 2.2 AA requirement). Measured effect on
# this exact 4-tile area, same OD-sampling approach, before vs after the
# branch: mean share of route length on unnamed edges went 13.3% -> 27.4%.
# That's a real, roughly 2x regression -- but it's geographically
# concentrated. A citywide-random-OD sample over the WHOLE graph only
# averages ~3.7%, because most trips barely touch a park; scoping this test
# to the park itself is what makes the regression visible at all.
#
# The ceiling below is deliberately loose (measured mean was 27.7% against
# real production data on 2026-07-30) -- this guards against the next filter
# widening making it much worse, not against small legitimate drift.


def _unnamed_share_pct(route: dict) -> float | None:
    total = sum(s["length_m"] for s in route["segments"])
    if total == 0:
        return None
    unnamed = sum(s["length_m"] for s in route["segments"] if s["name"] == "unnamed path")
    return 100 * unnamed / total


@pytest.mark.citywide
def test_central_park_area_route_descriptions_stay_mostly_named(citywide_store):
    """Fastest-route (tree_weight=0, matching the originally reported "even at
    NONE priority" bug) descriptions around Central Park shouldn't drift much
    further toward "unnamed path" than they already have. Random but SEEDED
    for determinism; restricted to the same 4-tile area
    (`confidence_checks.py`) where the regression was actually measured, since
    a citywide-wide sample dilutes it past visibility (see module comment)."""
    tiles = ["r16c11", "r16c12", "r17c11", "r17c12"]
    lat_min = min(config.get_tile_bbox(t).lat_min for t in tiles)
    lat_max = max(config.get_tile_bbox(t).lat_max for t in tiles)
    lon_min = min(config.get_tile_bbox(t).lon_min for t in tiles)
    lon_max = max(config.get_tile_bbox(t).lon_max for t in tiles)

    largest = set(max(citywide_store._graph.connected_components(mode="weak"), key=len))
    in_area = [
        i for i in largest
        if lon_min <= citywide_store._node_lonlat[i][0] <= lon_max
        and lat_min <= citywide_store._node_lonlat[i][1] <= lat_max
    ]
    assert len(in_area) > 100, "Central Park area barely loaded -- citywide tiles missing?"

    rng = random.Random(20260730)
    shares = []
    attempts = 0
    while len(shares) < 100 and attempts < 100 * 10:
        attempts += 1
        a = citywide_store._node_lonlat[rng.choice(in_area)]
        b = citywide_store._node_lonlat[rng.choice(in_area)]
        pair = citywide_store.snap_pair(a[1], a[0], b[1], b[0])
        if pair is None:
            continue
        start, end = pair
        route = citywide_store.route(start, end, tree_weight=0.0, month=7)
        if route is None:
            continue
        share = _unnamed_share_pct(route)
        if share is not None:
            shares.append(share)

    assert len(shares) >= 50, f"too few routed pairs to trust the mean ({len(shares)})"
    mean_share = sum(shares) / len(shares)
    assert mean_share <= 45.0, (
        f"mean unnamed-path share around Central Park rose to {mean_share:.1f}% "
        f"over {len(shares)} routes (baseline 27.7% on 2026-07-30) -- likely a "
        "fetch-filter change admitting more unnamed edges; see PLAN.md's "
        "'route descriptions lean unnamed' note before assuming this is fine"
    )
