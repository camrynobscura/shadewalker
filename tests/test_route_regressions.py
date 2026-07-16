"""Regression tests pinned to real bugs found and fixed during development.

Each case asserts the specific property that was wrong, not exact output --
exact numbers would break the moment tree-weight tuning legitimately
changes, but "the route uses the correct street" or "gets rejected instead
of silently mis-snapping" should hold forever.
"""

from pipeline import config


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
