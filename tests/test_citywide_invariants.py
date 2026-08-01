"""Opt-in-by-default sweep of the shade-priority invariant across the FULL
citywide graph -- real geography the pilot tile can't cover.

Runs automatically whenever the real data/tiles/ data is on disk (the
citywide_store fixture skips otherwise, so CI and a fresh clone never fail
for lack of it). In a tight edit-test loop, skip the ~15s graph load with:

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
MAX_EXPECTED_FLAGGED = 20  # current residual is 7; see comment above


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

    assert len(flagged) <= MAX_EXPECTED_FLAGGED, (
        f"{len(flagged)} node pair(s) sit within {NEAR_NODE_MAX_M}m of each other in "
        f"reality but need a >{DETOUR_FLAG_M}m detour in the graph, and aren't a known "
        f"divided-carriageway pattern -- well beyond the small, legitimately-blocked "
        f"residual expected (<= {MAX_EXPECTED_FLAGGED}). First few:\n"
        + "\n".join(
            f"  nodes {a},{b}: real={real:.1f}m graph={graph:.1f}m at "
            f"({lonlat[a][1]:.5f},{lonlat[a][0]:.5f})"
            for a, b, real, graph in flagged[:5]
        )
    )
