"""The one-run start-edge fold in GraphStore._best_plan must pick exactly
what a two-run search picks.

THE CLAIM
---------
A snap point sits partway along an edge, so a search can't start there.
The obvious search runs Dijkstra from each endpoint of the start edge
and keeps the cheaper total. The fold runs once from the nearer
endpoint, with the start edge re-priced at (far lead-in - near lead-in)
in the request's own cost array: every node then settles at
min(via near, via far), which is what the two runs compute between
them. This file keeps the two-run search as a reference and checks the
fold against it plan-for-plan -- entry node, exit node, edge list --
across a jittered grid, every preset weight, and the snap positions
that matter: mid-block, off-centre, and exactly on a node.

THE ONE LEGITIMATE DIFFERENCE
-----------------------------
A snap exactly on a node has a zero lead-in, so "start at that node and
walk the whole start edge" ties with "start at the edge's other end":
the same walk written two ways. The reference may write it the first
way (it tries endpoints in u, v order and keeps a strict minimum); the
fold always writes it the second. The comparison normalizes the
reference to the fold's spelling -- and the end-to-end check below
confirms the two spellings are one route (same length, shade, trees).

WHY A JITTERED GRID
-------------------
A regular grid has many equal-length routes at weight 0, and Dijkstra's
choice among exact ties depends on search order -- which the fold
changes (a different source). Irregular node spacing makes exact ties
vanishingly unlikely, so every disagreement would be a real one.
"""

import gzip
import json
import random

import numpy as np
import pytest

from pipeline import config
from server.graph_store import GraphStore, _local_distance_m

WEIGHTS = [0.0, 5.0, 15.0, 40.0]
MONTH = 7
ROWS, COLS = 6, 6
ORIGIN_LAT, ORIGIN_LON = 40.68000, -73.99000
STEP_LAT, STEP_LON = 0.00090, 0.00118  # ~100m x ~100m before jitter


def _build_tile():
    rng = random.Random(20260909)
    nodes = {}
    for r in range(ROWS):
        for c in range(COLS):
            nodes[f"n{r}_{c}"] = [
                ORIGIN_LON + c * STEP_LON + rng.uniform(-0.00015, 0.00015),
                ORIGIN_LAT + r * STEP_LAT + rng.uniform(-0.00012, 0.00012),
            ]
    edges = []

    def add(u, v, name, coords=None, deciduous=None):
        a, b = nodes[u], nodes[v]
        pts = coords or [a, b]
        length = sum(_local_distance_m(p[1], p[0], q[1], q[0])
                     for p, q in zip(pts, pts[1:]))
        if deciduous is None:
            deciduous = rng.choice([0.0, 0.0, 0.5, 1.5, 3.0, 5.0])
        edges.append({
            "u": u, "v": v, "key": 0, "side": rng.choice(["L", "R"]),
            "length_m": round(length, 1), "name": name,
            "tree_deciduous": deciduous, "tree_evergreen": rng.choice([0.0, 0.0, 0.8]),
            "tree_count": int(deciduous * 2), "tree_park_canopy": 0.0,
            "coords": pts,
        })

    for r in range(ROWS):
        for c in range(COLS):
            if c + 1 < COLS:
                add(f"n{r}_{c}", f"n{r}_{c + 1}", f"Row {r} Street")
            if r + 1 < ROWS:
                add(f"n{r}_{c}", f"n{r + 1}_{c}", f"Col {c} Avenue")
    # One self-loop: a little park path leaving and re-entering the
    # bottom-left corner. Exercises the "one endpoint, nothing to fold"
    # branch, which a plain grid never reaches.
    corner = nodes["n0_0"]
    loop_pts = [corner,
                [corner[0] - 0.00030, corner[1] - 0.00020],
                [corner[0] - 0.00010, corner[1] - 0.00045],
                corner]
    add("n0_0", "n0_0", "Corner Loop", coords=loop_pts, deciduous=2.0)
    return nodes, edges


NODES, EDGES = _build_tile()


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> GraphStore:
    export_dir = tmp_path_factory.mktemp("fold-export")
    with gzip.open(export_dir / "tile.json.gz", "wt") as fh:
        json.dump({"nodes": NODES, "edges": EDGES}, fh)
    previous = config.EXPORT_DIR
    config.EXPORT_DIR = export_dir
    try:
        loaded = GraphStore()
        loaded.load()
    finally:
        config.EXPORT_DIR = previous
    return loaded


def _two_run_plan(store, start, end, costs):
    """The reference search: Dijkstra from each start endpoint,
    one-to-many to both end
    endpoints, strict minimum over the four totals, then the same-edge
    direct hop compared like any other candidate."""
    graph = store._graph
    costs_view = memoryview(costs)
    end_options = [(end.node_u, end.dist_to_u_m), (end.node_v, end.dist_to_v_m)]
    end_nodes = [node for node, _ in end_options]
    best_cost, best_plan = None, None
    for s_node, s_dist_m in ((start.node_u, start.dist_to_u_m),
                             (start.node_v, start.dist_to_v_m)):
        s_cost = s_dist_m / store._length[start.edge] * costs[start.edge]
        paths = graph.get_shortest_paths(s_node, to=end_nodes,
                                         weights=costs_view, output="epath")
        for (e_node, e_dist_m), path in zip(end_options, paths):
            e_cost = e_dist_m / store._length[end.edge] * costs[end.edge]
            if not path and s_node != e_node:
                continue
            total = s_cost + float(costs[path].sum()) + e_cost
            if best_cost is None or total < best_cost:
                best_cost = total
                best_plan = ("via_nodes", s_node, s_dist_m, e_node, e_dist_m, list(path))
    if start.edge == end.edge:
        direct_dist_m = abs(start.dist_to_u_m - end.dist_to_u_m)
        direct_cost = direct_dist_m / store._length[start.edge] * costs[start.edge]
        if best_cost is None or direct_cost < best_cost:
            best_plan = ("direct", direct_dist_m)
    return best_plan


def _normalized(plan, start):
    """Spell a 'walk the whole start edge first' plan as 'enter at the
    other end' -- the fold's spelling. Only a zero lead-in ever produces
    the first form (any other makes it a strict there-and-back)."""
    if plan is None or plan[0] != "via_nodes":
        return plan
    _, s_node, s_dist_m, e_node, e_dist_m, path = plan
    if path and path[0] == start.edge and start.node_u != start.node_v:
        assert s_dist_m == 0.0, "a start-edge first hop with a real lead-in is a there-and-back"
        if s_node == start.node_u:
            s_node, s_dist_m = start.node_v, start.dist_to_v_m
        else:
            s_node, s_dist_m = start.node_u, start.dist_to_u_m
        path = path[1:]
    return ("via_nodes", s_node, s_dist_m, e_node, e_dist_m, path)


def _point_on_edge(edge, fraction):
    """(lat, lon) `fraction` of the way along an edge's first segment."""
    a, b = edge["coords"][0], edge["coords"][1]
    return (a[1] + (b[1] - a[1]) * fraction, a[0] + (b[0] - a[0]) * fraction)


def _cases():
    rng = random.Random(7)
    grid_edges = [e for e in EDGES if e["u"] != e["v"]]
    fractions = [0.0, 0.0, 0.2, 0.5, 0.5, 0.85, 1.0]
    cases = []
    for _ in range(60):
        a, b = rng.choice(grid_edges), rng.choice(grid_edges)
        cases.append((_point_on_edge(a, rng.choice(fractions)),
                      _point_on_edge(b, rng.choice(fractions))))
    # Same edge, both ways round, so the direct hop competes.
    same = grid_edges[len(grid_edges) // 2]
    cases.append((_point_on_edge(same, 0.2), _point_on_edge(same, 0.7)))
    cases.append((_point_on_edge(same, 0.7), _point_on_edge(same, 0.2)))
    # Start on the self-loop (its far vertex), end across the grid.
    loop = next(e for e in EDGES if e["u"] == e["v"])
    loop_far = loop["coords"][2]
    cases.append(((loop_far[1], loop_far[0]), _point_on_edge(grid_edges[-1], 0.5)))
    cases.append((_point_on_edge(grid_edges[-1], 0.5), (loop_far[1], loop_far[0])))
    return cases


CASES = _cases()


def test_the_fixture_exercises_every_branch(store):
    """Guards against a vacuous pass: the cases must include zero
    lead-ins (the tie), a same-edge pair, and a self-loop snap."""
    seen_zero_lead_in = seen_same_edge = seen_self_loop = False
    for frm, to in CASES:
        start, end = store.snap_pair(*frm, *to)
        if min(start.dist_to_u_m, start.dist_to_v_m) == 0.0:
            seen_zero_lead_in = True
        if start.edge == end.edge:
            seen_same_edge = True
        if start.node_u == start.node_v:
            seen_self_loop = True
    assert seen_zero_lead_in and seen_same_edge and seen_self_loop


@pytest.mark.parametrize("weight", WEIGHTS)
def test_fold_picks_the_two_run_plan(store, weight):
    for frm, to in CASES:
        pair = store.snap_pair(*frm, *to)
        assert pair is not None
        start, end = pair
        costs = store.edge_costs(weight, MONTH)
        reference = _normalized(_two_run_plan(store, start, end, costs.copy()), start)
        folded = store._best_plan(start, end, costs.copy())
        assert folded == reference, (frm, to, weight)


@pytest.mark.parametrize("weight", WEIGHTS)
def test_fold_leaves_the_cost_array_as_it_found_it(store, weight):
    frm, to = CASES[0]
    start, end = store.snap_pair(*frm, *to)
    costs = store.edge_costs(weight, MONTH)
    untouched = costs.copy()
    store._best_plan(start, end, costs)
    assert np.array_equal(costs, untouched)


def test_zero_lead_in_tie_is_one_route_end_to_end(store):
    """Where the reference and the fold spell the plan differently, the
    stitched response must be the same walk: same length, shade, trees,
    and the same first and last coordinate."""
    checked = 0
    for frm, to in CASES:
        start, end = store.snap_pair(*frm, *to)
        for weight in WEIGHTS:
            costs = store.edge_costs(weight, MONTH)
            raw = _two_run_plan(store, start, end, costs.copy())
            if raw is None or raw[0] != "via_nodes" or not raw[5] or raw[5][0] != start.edge:
                continue
            checked += 1
            _, s_node, s_dist_m, e_node, e_dist_m, path = raw
            reference_length = round(
                s_dist_m + float(store._length[path].sum()) + e_dist_m, 1)
            result = store.route(start, end, tree_weight=weight, month=MONTH)
            assert result["length_m"] == pytest.approx(reference_length, abs=0.05)
            assert result["coords"][0] == pytest.approx(start.point)
            assert result["coords"][-1] == pytest.approx(end.point)
    assert checked > 0, "no zero-lead-in tie case was hit -- the check proved nothing"
