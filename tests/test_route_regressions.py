"""Pinned routes on the real citywide export: four anchor bands at known
chokepoints, and a seeded golden batch spread over all five boroughs.

Anchors are length bands, not just ceilings: at several sites a bug
makes the route shorter (a phantom weld shortcutting between elevation
levels, an over-connection across water), so a suspiciously short route
is as much a regression as a detour.

Goldens are 25 seeded pairs drawn with tools/audit/routing_harness.py's
exact recipe (seed 20260825, uniform over CITY_BBOX, 500-5000m
straight-line band -- the first 25 routable draws), pinned at their
weight-0 month-7 length. They are the broad tripwire the four hand-picked
anchors can't be: any wormhole (length collapses), severance (length
balloons), or load-time merge bug moves lengths by tens of percent.
Tolerance is max(2.5%, 30m): snapping can legitimately land a request on
a slightly different node after a rebuild, which moves a route by tens of
meters, while the failure classes this guards against move it by far
more. On the same export the values are deterministic and exact.

After a deliberate graph change (new OSM pin, pipeline change), re-derive
with tools/audit/routing_harness.py and bump the numbers in one
documented commit -- never nudge them to green a red run without knowing
why it went red.
"""
import pytest


def _route_length(store, frm, to):
    pair = store.snap_pair(frm[0], frm[1], to[0], to[1])
    if pair is None:
        return None
    result = store.route(pair[0], pair[1], tree_weight=0.0, month=7)
    return None if result is None else result["length_m"]


# --- Anchor bands ------------------------------------------------------------
# Each pin: (from, to, min_m, max_m, evidence). Bands re-measured 2026-08-28
# on the sidewalk export; the failure each bound guards is in the evidence.

ANCHOR_PINS = [
    pytest.param(
        (40.704456, -73.986651), (40.717267, -73.977333), 4100.0, 5000.0,
        "4,566.6m measured 2026-08-28, verified over the Manhattan Bridge "
        "Pedestrian Path (the route's longest step, 1,971.6m, names the "
        "walkway); the per-sidewalk approach loops at both ends were "
        "checked step-by-step before pinning. Floor 4,100 catches a "
        "phantom shortcut across the river (a weld at DUMBO reads "
        "~3,372m); ceiling 5,000 catches the walkway severing, which "
        "forces the Brooklyn Bridge and balloons the walk well past 5.5km",
        id="dumbo-to-les-over-manhattan-bridge",
    ),
    pytest.param(
        (40.704169, -73.989582), (40.704637, -73.986443), None, 335.0,
        "301.4m measured 2026-08-28 (OSRM 305m), direct along John St "
        "under the bridge anchorage. The failure mode is a ~+40m plaza "
        "detour (dead splits at the anchorage), so the ceiling sits "
        "deliberately below measured+40",
        id="john-st-walk-across-the-anchorage",
    ),
    pytest.param(
        (40.697559, -73.99646), (40.699825, -73.996337), None, 1050.0,
        "924.9m measured 2026-08-28 (OSRM 918m) via the field-checked "
        "Squibb Park / Promenade access. If this "
        "entrance severs, the route balloons toward the next park access",
        id="clark-st-promenade-entrance",
    ),
    pytest.param(
        (40.744796, -73.978573), (40.75992, -73.936627), 5500.0, 6800.0,
        "5,977.1m measured 2026-08-28 (Valhalla-no-ferry agreed within "
        "~3%). With the Queensboro Outer Roadway severed this pair reads "
        "11,713m via the RFK Bridge; the floor guards a phantom shortcut "
        "across the river",
        id="queensboro-outer-roadway-midtown-to-lic",
    ),
]


@pytest.mark.citywide
@pytest.mark.parametrize("frm, to, min_m, max_m, evidence", ANCHOR_PINS)
def test_anchor_site_stays_in_its_verified_band(citywide_store, frm, to, min_m, max_m, evidence):
    length = _route_length(citywide_store, frm, to)
    assert length is not None, "this anchor route no longer resolves at all"
    assert length <= max_m, (
        f"anchor route got LONGER than its verified band ({length:.1f}m > "
        f"{max_m:.0f}m) -- something along it has severed. Evidence for the "
        f"band: {evidence}"
    )
    if min_m is not None:
        assert length >= min_m, (
            f"anchor route got suspiciously SHORT ({length:.1f}m < "
            f"{min_m:.0f}m) -- a phantom connection is shortcutting it. "
            f"Evidence for the band: {evidence}"
        )


# --- Golden batch ------------------------------------------------------------
# (from_lat, from_lon, to_lat, to_lon, verified_length_m), measured
# 2026-08-28 at tree_weight=0, month=7 with tools/audit/routing_harness.py
# (seed 20260825).

GOLDEN_ROUTES = [
    (40.773340, -73.905417, 40.756771, -73.855859, 5048.9),
    (40.605538, -74.009942, 40.618173, -73.971417, 4735.6),
    (40.739794, -73.883976, 40.711449, -73.862820, 4376.6),
    (40.616906, -74.121278, 40.645201, -74.092488, 5416.2),
    (40.868987, -73.814841, 40.860551, -73.801509, 2077.9),
    (40.580146, -74.155430, 40.593820, -74.164033, 2509.0),
    (40.860016, -73.863515, 40.823611, -73.862575, 5075.6),
    (40.841978, -73.866163, 40.867788, -73.885811, 4267.6),
    (40.622092, -73.955933, 40.664710, -73.946568, 5799.9),
    (40.722622, -73.735995, 40.731961, -73.718854, 3534.2),
    (40.787649, -73.978592, 40.796334, -73.938363, 4606.6),
    (40.618190, -74.095420, 40.634026, -74.090207, 3158.0),
    (40.740766, -73.774703, 40.765024, -73.792946, 3437.8),
    (40.667382, -73.948307, 40.629923, -73.954523, 4848.6),
    (40.757100, -73.827482, 40.745114, -73.816579, 2070.2),
    (40.718936, -73.948692, 40.692127, -73.906311, 5184.7),
    (40.724484, -73.738175, 40.763544, -73.753055, 6964.4),
    (40.682725, -73.824985, 40.691724, -73.844185, 2651.6),
    (40.700508, -73.747105, 40.707746, -73.768146, 2502.7),
    (40.719394, -73.767077, 40.738681, -73.733314, 3918.9),
    (40.722480, -73.943323, 40.741097, -73.901659, 5010.3),
    (40.636819, -74.000884, 40.606813, -73.979385, 4323.4),
    (40.606368, -74.019339, 40.606030, -74.002881, 1798.7),
    (40.744242, -73.827132, 40.766421, -73.811837, 3586.7),
    (40.710329, -73.844899, 40.717277, -73.815492, 2914.2),
]

GOLDEN_TOLERANCE_RELATIVE = 0.025
GOLDEN_TOLERANCE_FLOOR_M = 30.0


@pytest.mark.citywide
def test_golden_routes_stay_at_their_verified_lengths(citywide_store):
    """One test, not 25 parametrized ones: a systemic break (merge bug,
    mass severance) fails many goldens at once, and one combined report
    reads better than 20 identical stack traces."""
    off = []
    unroutable = 0
    for a_lat, a_lon, b_lat, b_lon, expected in GOLDEN_ROUTES:
        length = _route_length(citywide_store, (a_lat, a_lon), (b_lat, b_lon))
        if length is None:
            unroutable += 1
            off.append((a_lat, a_lon, b_lat, b_lon, expected, None))
            continue
        tolerance = max(GOLDEN_TOLERANCE_RELATIVE * expected,
                        GOLDEN_TOLERANCE_FLOOR_M)
        if abs(length - expected) > tolerance:
            off.append((a_lat, a_lon, b_lat, b_lon, expected, length))
    assert not off, (
        f"{len(off)} of {len(GOLDEN_ROUTES)} golden routes moved "
        f"({unroutable} unroutable). A deliberate graph change needs a "
        f"documented re-derivation, not a nudge. Offenders:\n" + "\n".join(
            f"  ({a:.5f},{b:.5f})->({c:.5f},{d:.5f}): expected {e:.1f}m, "
            f"got {'UNROUTABLE' if g is None else f'{g:.1f}m'}"
            for a, b, c, d, e, g in off[:8]
        )
    )
