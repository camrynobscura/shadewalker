"""Tests for pipeline/graph/pedestrian.py -- OSM's ways -> our graph.

These do NOT test whether OSM's data is right; that is OSM's business and
the governing rule says we follow it. They test the TRANSFORMATION, which
is entirely our own code: which ways count as walkable, where a way gets
cut into edges, how long each edge is, and what survives the NYC clip.

Every failure here has a symptom that looks like something else:

  - a split that doesn't share its junction node disconnects the city,
    while still reporting a plausible node and edge count
  - an inverted access rule silently drops every bridge walkway
  - a bad clip looks like an OSM coverage gap, which our own policy says
    to shrug at

All of it runs on hand-built inputs -- no .pbf, no fixture.
"""

import numpy as np
import pytest
from shapely.geometry import box
from shapely.prepared import prep

from pipeline.graph.pedestrian import (
    _GEOD,
    Way,
    _chain_lengths_m,
    _normalize_name,
    _split_way,
    _touches_nyc,
    find_junctions,
    is_pedestrian,
)


def _way(osm_id, node_ids, name=""):
    """A straight north-south way, one point per node id."""
    lons = [-74.0] * len(node_ids)
    lats = [40.70 + 0.001 * i for i in range(len(node_ids))]
    return Way(osm_id=osm_id, name=name, node_ids=list(node_ids),
               lons=lons, lats=lats)


# ── is_pedestrian: the whole inclusion rule ──────────────────────────────

@pytest.mark.parametrize("highway", ["footway", "path", "steps", "pedestrian"])
def test_dedicated_pedestrian_highways_are_included(highway):
    assert is_pedestrian({"highway": highway})


@pytest.mark.parametrize("highway", ["residential", "primary", "service",
                                     "motorway", "unclassified"])
def test_street_centerlines_are_not_pedestrian_infrastructure(highway):
    """The model routes on pavements, not roads -- that is the point of it."""
    assert not is_pedestrian({"highway": highway})


def test_an_area_is_not_a_line_to_route_along():
    # A plaza's interior fill. Its edges are mapped separately as real ways.
    assert not is_pedestrian({"highway": "pedestrian", "area": "yes"})


@pytest.mark.parametrize("foot", ["no", "private"])
def test_foot_no_excludes(foot):
    assert not is_pedestrian({"highway": "footway", "foot": foot})


@pytest.mark.parametrize("access", ["private", "no"])
def test_blanket_access_restriction_excludes(access):
    assert not is_pedestrian({"highway": "footway", "access": access})


@pytest.mark.parametrize("access", ["private", "no"])
@pytest.mark.parametrize("foot", ["yes", "designated"])
def test_explicit_foot_access_overrides_a_blanket_restriction(access, foot):
    """OSM's own tag hierarchy, and real on NYC bridge walkways.

    This is the single easiest condition in the module to invert, and
    inverting it would drop every bridge walkway in the city -- which
    would read as a modest coverage dip, not as a break.
    """
    assert is_pedestrian({"highway": "footway", "access": access, "foot": foot})


def test_a_cycleway_counts_only_with_explicit_foot_access():
    assert not is_pedestrian({"highway": "cycleway"})
    assert is_pedestrian({"highway": "cycleway", "foot": "designated"})
    assert is_pedestrian({"highway": "cycleway", "foot": "yes"})


# ── find_junctions: where an edge is allowed to start and end ────────────

def test_a_node_shared_by_two_ways_is_a_junction():
    ways = [_way(1, [10, 20, 30]), _way(2, [40, 20, 50])]
    assert 20 in find_junctions(ways)


def test_a_ways_own_endpoints_are_junctions():
    junctions = find_junctions([_way(1, [10, 20, 30])])
    assert {10, 30} <= junctions


def test_an_interior_node_used_by_one_way_is_not_a_junction():
    """Shape points are not routable. If they became junctions, every
    bend in a sidewalk would be an intersection."""
    assert 20 not in find_junctions([_way(1, [10, 20, 30])])


def test_a_node_a_single_way_visits_twice_is_a_junction():
    """A way that doubles back through a point it already used. Without
    this rule the chain would run through that point twice and the edge
    would be a self-intersecting line.

    The revisited node is deliberately in the MIDDLE of the way, not its
    first-and-last node. A closed loop like [10, 20, 30, 40, 10] does NOT
    test this rule -- node 10 is also the way's endpoint, so the endpoint
    rule makes it a junction anyway and the test passes with the revisit
    rule deleted. That was this test's original form, and a mutation run
    (2026-08-23) caught it: deleting the rule left the whole suite green.
    """
    assert 20 in find_junctions([_way(1, [10, 20, 30, 20, 40])])


def test_one_way_visiting_a_node_twice_is_not_two_ways_using_it():
    """The use count is taken over each way's DISTINCT nodes.

    Counting raw occurrences instead would make any node a way passes
    twice look like a node shared between two ways. It happens to reach
    the same verdict for that node (both make it a junction) but it would
    also corrupt the count for every other way sharing it.
    """
    ways = [_way(1, [10, 20, 30, 20, 40])]      # 20 visited twice, one way
    junctions = find_junctions(ways)
    assert 20 in junctions                       # via the revisit rule
    assert 30 not in junctions                   # a plain shape point


# ── _split_way: cutting a way into edges ─────────────────────────────────

def test_a_way_with_no_interior_junction_stays_one_edge():
    way = _way(1, [10, 20, 30])
    chains = _split_way(way, junctions={10, 30})
    assert len(chains) == 1
    assert chains[0][0] == [10, 20, 30]


def test_consecutive_chains_share_their_junction_node():
    """THE load-bearing test.

    Each piece must END on the point the next piece STARTS on. If the
    split handed out disjoint pieces instead, the graph would come apart
    at every junction in the city -- while still reporting an entirely
    plausible node and edge count.
    """
    way = _way(1, [10, 20, 30])
    chains = _split_way(way, junctions={10, 20, 30})
    assert [c[0] for c in chains] == [[10, 20], [20, 30]]
    assert chains[0][0][-1] == chains[1][0][0]


def test_a_split_loses_no_geometry():
    """Reassembling the chains must reproduce the original way exactly,
    with each shared junction counted once."""
    way = _way(1, [10, 20, 30, 40, 50])
    chains = _split_way(way, junctions={10, 30, 50})

    rebuilt_ids = list(chains[0][0])
    rebuilt_lons = list(chains[0][1])
    for node_ids, lons, _ in chains[1:]:
        assert node_ids[0] == rebuilt_ids[-1]   # joins where the last ended
        rebuilt_ids.extend(node_ids[1:])
        rebuilt_lons.extend(lons[1:])
    assert rebuilt_ids == way.node_ids
    assert rebuilt_lons == way.lons


def test_n_interior_junctions_make_n_plus_one_edges():
    way = _way(1, [10, 20, 30, 40, 50])
    chains = _split_way(way, junctions={10, 20, 40, 50})
    assert len(chains) == 3


def test_a_closed_way_with_no_other_junction_is_one_self_loop():
    """123 of these exist in the real citywide graph."""
    way = _way(1, [10, 20, 30, 10])
    chains = _split_way(way, junctions={10})
    assert len(chains) == 1
    assert chains[0][0][0] == chains[0][0][-1] == 10


# ── _chain_lengths_m: the batched geodesic measurement ───────────────────

def test_batched_lengths_match_measuring_each_chain_alone():
    """The fast path computes every chain in one Geod call and sums each
    chain's slice with np.add.reduceat. reduceat has a documented trap
    when two consecutive start indices are equal, so the batching is
    checked against the obvious per-chain equivalent rather than trusted.
    """
    chains = [
        ([1, 2], [-74.0, -74.0], [40.70, 40.71]),
        ([2, 3, 4], [-74.0, -73.99, -73.98], [40.71, 40.715, 40.72]),
        ([4, 5], [-73.98, -73.97], [40.72, 40.72]),
        ([5, 6, 7, 8], [-73.97, -73.96, -73.95, -73.94],
         [40.72, 40.73, 40.72, 40.73]),
    ]
    batched = _chain_lengths_m(chains)
    for (_, lons, lats), got in zip(chains, batched):
        assert got == pytest.approx(_GEOD.line_length(lons, lats))


def test_a_known_distance_comes_out_right():
    """One degree of latitude is ~111 km everywhere. A length computed in
    DEGREES instead of metres -- this project's #1 bug class -- would come
    out around 0.01 rather than 111,000."""
    chains = [([1, 2], [-74.0, -74.0], [40.0, 41.0])]
    assert _chain_lengths_m(chains)[0] == pytest.approx(111_000, rel=0.01)


def test_every_chain_gets_its_own_length():
    """A short chain next to a long one must not inherit the long one's
    length -- the specific way a mis-sliced reduceat fails."""
    chains = [
        ([1, 2], [-74.0, -74.0], [40.70, 40.7001]),   # ~11 m
        ([2, 3], [-74.0, -74.0], [40.70, 40.80]),     # ~11 km
    ]
    short, long = _chain_lengths_m(chains)
    assert short < 20
    assert long > 10_000


def test_lengths_array_is_one_entry_per_chain():
    chains = [([1, 2], [-74.0, -74.0], [40.70, 40.71])] * 5
    assert len(_chain_lengths_m(chains)) == 5


def test_no_chains_is_not_a_crash():
    assert len(_chain_lengths_m([])) == 0


# ── _touches_nyc: the clip, and keeping border crossings whole ───────────

NYC = box(-74.0, 40.7, -73.9, 40.8)
PREPARED = prep(NYC)
BOUNDS = NYC.bounds


def _touches(lons, lats):
    return _touches_nyc(PREPARED, lons, lats, *[BOUNDS[0], BOUNDS[1],
                                                BOUNDS[2], BOUNDS[3]])


def test_a_way_inside_the_boundary_is_kept():
    assert _touches([-73.95, -73.94], [40.75, 40.76])


def test_a_way_entirely_outside_is_dropped():
    """Buffalo. The extract is the whole state, so most ways are this."""
    assert not _touches([-78.87, -78.86], [42.88, 42.89])


def test_a_way_straddling_the_boundary_is_kept():
    """Bridges. Keeping the way WHOLE is the reason the test is
    'does any point fall inside' rather than 'do all points'."""
    assert _touches([-73.95, -74.20], [40.75, 40.75])


def test_a_way_just_outside_the_bounding_box_is_dropped():
    """The bbox pre-check is an optimisation; it must not change the
    verdict for anything near the edge."""
    assert not _touches([-74.001, -74.002], [40.75, 40.75])


# ── _normalize_name ──────────────────────────────────────────────────────

def test_a_real_name_survives():
    assert _normalize_name("Court Street") == "Court Street"


def test_a_missing_name_becomes_empty_not_none():
    """97.3% of NYC sidewalks have no name of their own. The export's
    `name` field is a string, so this must never be None."""
    assert _normalize_name(None) == ""
    assert _normalize_name(123) == ""
