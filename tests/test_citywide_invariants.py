"""Opt-in-by-default sweep of the shade-priority invariant across the FULL
citywide graph -- real geography the pilot tile can't cover.

Runs automatically whenever the real data/tiles/ data is on disk (the
citywide_store fixture skips otherwise, so CI and a fresh clone never fail
for lack of it). In a tight edit-test loop, skip the ~15s graph load with:

    uv run pytest -m "not citywide"
"""
import random

import pytest

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
