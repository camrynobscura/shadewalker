"""Tests for pipeline/graph/naming.py -- deriving a sidewalk's parent street.

This module never touches the routing graph, so nothing here can produce a
wrong ROUTE. What it can produce is a confidently wrong INSTRUCTION, which
is the failure the rule was written to avoid: "turn left on Court Street"
when Court Street is the next block over sends someone to the wrong corner
and they believe it.

Every failure in here is disguised as something else:

  - measuring MINIMUM distance instead of the MEDIAN along the line makes
    every cross street a candidate, because a block-length sidewalk touches
    a different one at each end. That exact error made the audit instrument
    report 16.1% where the truth was 94.0% -- and it reads as "OSM's data is
    too sparse to name anything", not as a bug in our metric.
  - failing to group candidates by NAME turns one street mapped as two OSM
    ways into two rival candidates, so the clearest cases -- a sidewalk
    running dead alongside one street -- come out "ambiguous" and unnamed.
  - conflating "too short to name" with "no parent found" corrupts the rate
    we compare against the measured 94.0%, while every individual edge still
    looks correctly handled.

All of it runs on hand-built geometry -- no .pbf, no fixture. The fixtures
are laid out so distances are EXACT: `_to_m` is a flat scale, so an offset
built as `metres / _LAT_M` and read back through it round-trips to the same
double. Verified, not assumed -- which is what lets the ratio boundary below
be tested as an exact tie rather than a bracket.
"""

import math

import pytest
from shapely.geometry import LineString

from pipeline.graph.naming import (
    _K,
    _LAT_M,
    AMBIGUOUS_RATIO,
    MAX_FOLD_NAMES,
    MIN_NAMEABLE_LEN_M,
    PARALLEL_MAX_DIFF_DEG,
    PARENT_MAX_M,
    _probe_points,
    _to_m,
    assign_parent_names,
)
from pipeline.graph.pedestrian import Way

BASE_LON, BASE_LAT = -74.00, 40.70


def _origin(index=0):
    """Scenarios sit ~1.1km apart so one's streets never reach another's."""
    return (BASE_LON, BASE_LAT + 0.01 * index)


def _edge(coords, name=""):
    """One export-shaped edge. Only `name` and `coords` matter to naming."""
    return {"u": "1", "v": "2", "key": 0, "side": "C",
            "length_m": 0.0, "name": name, "coords": coords}


def _sidewalk(length_m=60.0, name="", origin=None):
    """A due-east edge of exactly `length_m`.

    60m is the default because it samples 6 probes at 0/12/24/36/48/60m,
    which is what separates a parent street from a cross street below.
    """
    lon, lat = origin or _origin()
    return _edge([[lon, lat], [lon + length_m / _K, lat]], name=name)


def _parallel_street(name, offset_m, osm_id=1, span_m=200.0, origin=None):
    """A named street running due east, `offset_m` north of the sidewalk.

    Spans wider than the sidewalk in both directions, so every probe
    measures the perpendicular: the median distance is exactly `offset_m`.
    """
    lon, lat = origin or _origin()
    dlat, dlon = offset_m / _LAT_M, span_m / _K
    return Way(osm_id=osm_id, name=name, node_ids=[osm_id, osm_id + 1],
               lons=[lon - dlon, lon + dlon],
               lats=[lat + dlat, lat + dlat])


def _cross_street(name, at_m=0.0, osm_id=9, reach_m=100.0, origin=None):
    """A named street running due north, crossing the sidewalk at `at_m`."""
    lon, lat = origin or _origin()
    dlat = reach_m / _LAT_M
    crossing_lon = lon + at_m / _K
    return Way(osm_id=osm_id, name=name, node_ids=[osm_id, osm_id + 1],
               lons=[crossing_lon, crossing_lon],
               lats=[lat - dlat, lat + dlat])


def _street_ending_partway(name, offset_m, ends_at_m, osm_id=11, origin=None):
    """A street parallel at `offset_m` that stops `ends_at_m` along the
    sidewalk -- what a street split at a junction actually looks like.

    Past that end the probe distances fan out instead of staying flat, which
    is what separates the median from the maximum.
    """
    lon, lat = origin or _origin()
    dlat = offset_m / _LAT_M
    return Way(osm_id=osm_id, name=name, node_ids=[osm_id, osm_id + 1],
               lons=[lon - 200.0 / _K, lon + ends_at_m / _K],
               lats=[lat + dlat, lat + dlat])


def _line(edge):
    return LineString([_to_m(lon, lat) for lon, lat in edge["coords"]])


def _street_line(way):
    return LineString([_to_m(lon, lat) for lon, lat in zip(way.lons, way.lats)])


# ── Rule 1: a way's own name wins ────────────────────────────────────────

def test_an_edge_that_already_has_a_name_keeps_it():
    edge = _sidewalk(name="Joralemon Street")
    tally = assign_parent_names([edge], [])
    assert edge["name"] == "Joralemon Street"
    assert tally["own"] == 1
    assert tally["derived"] == 0


def test_own_name_wins_over_a_nearer_differently_named_street():
    """OSM said so. We don't second-guess it, however close the rival."""
    edge = _sidewalk(name="Joralemon Street")
    touching = _parallel_street("Court Street", offset_m=0.0)
    assign_parent_names([edge], [touching])
    assert edge["name"] == "Joralemon Street"


def test_an_osm_typo_survives_verbatim():
    """`Brookyln Bridge Promenade` is misspelled IN OSM. Deriving a name is
    not a licence to correct it -- that would be an override of OSM, which
    the governing rule forbids, and it is the user's own confirmed reading.
    """
    edge = _sidewalk(name="Brookyln Bridge Promenade")
    correct = _parallel_street("Brooklyn Bridge Promenade", offset_m=1.0)
    assign_parent_names([edge], [correct])
    assert edge["name"] == "Brookyln Bridge Promenade"


# ── The length gate ──────────────────────────────────────────────────────

def test_an_edge_shorter_than_the_minimum_gets_no_name():
    """Corner nubs and kerb ramps: too short for their nearest-street answer
    to mean anything, so they inherit at render time instead.
    """
    edge = _sidewalk(length_m=MIN_NAMEABLE_LEN_M - 0.1)
    tally = assign_parent_names([edge], [_parallel_street("Court Street", 8.0)])
    assert edge["name"] == ""
    assert tally["too_short"] == 1
    assert tally["derived"] == 0


def test_an_edge_just_over_the_minimum_is_named():
    edge = _sidewalk(length_m=MIN_NAMEABLE_LEN_M + 0.1)
    tally = assign_parent_names([edge], [_parallel_street("Court Street", 8.0)])
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


def test_too_short_is_counted_separately_from_having_no_parent():
    """Conflating the two would corrupt the rate compared against the
    measured 94.0% while every individual edge still looked right.
    """
    edge = _sidewalk(length_m=1.0)
    tally = assign_parent_names([edge], [_parallel_street("Court Street", 8.0)])
    assert tally["too_short"] == 1
    assert tally["no_parent"] == 0


# ── Finding a parent at all ──────────────────────────────────────────────

def test_a_street_beyond_the_search_radius_is_not_a_parent():
    edge = _sidewalk()
    far = _parallel_street("Court Street", offset_m=PARENT_MAX_M + 10.0)
    tally = assign_parent_names([edge], [far])
    assert edge["name"] == ""
    assert tally["no_parent"] == 1


def test_a_single_named_street_in_range_wins_without_a_ratio_test():
    """One candidate is never ambiguous, however far out it sits -- there is
    nothing for it to be confused with.
    """
    edge = _sidewalk()
    lone = _parallel_street("Court Street", offset_m=25.0)
    tally = assign_parent_names([edge], [lone])
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


def test_a_street_the_sidewalk_lies_on_top_of_wins():
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", 0.0, osm_id=1),
               _parallel_street("Union Street", 20.0, osm_id=3)]
    assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"


def test_a_street_way_with_one_point_is_skipped_not_crashed():
    """A single-node way cannot become a LineString. Guarding it is what
    keeps one malformed OSM way from taking down a citywide build.
    """
    degenerate = Way(osm_id=7, name="Nub Street", node_ids=[7],
                     lons=[BASE_LON], lats=[BASE_LAT])
    edge = _sidewalk()
    tally = assign_parent_names(
        [edge], [degenerate, _parallel_street("Court Street", 20.0)])
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


# ── The metric: median ALONG the line, not minimum ───────────────────────

def test_a_cross_street_touching_one_end_is_not_the_parent():
    """THE regression. A 60m sidewalk touches its cross street at one end,
    so the MINIMUM distance to it is 0 -- nearer than the true parent
    running alongside at 8m. The median is 36m, past the 30m radius, so it
    is not a candidate at all. Swap the median for a minimum and this edge
    gets named after the street it merely crosses.
    """
    edge = _sidewalk(length_m=60.0)
    streets = [_parallel_street("Court Street", offset_m=8.0, osm_id=1),
               _cross_street("Union Street", at_m=0.0, osm_id=9)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1
    assert tally["ambiguous"] == 0


def test_a_street_only_crossed_at_one_end_is_no_parent_at_all():
    """The 30m radius applies to the MEDIAN, not just to the search buffer.
    The cross street's closest point is 0m, so the buffer hands it over; only
    the median check (36m) rejects it. Drop that check and a sidewalk gets
    named after a street it merely crosses.
    """
    edge = _sidewalk(length_m=60.0)
    crossing = _cross_street("Union Street", at_m=0.0)
    tally = assign_parent_names([edge], [crossing])
    assert edge["name"] == ""
    assert tally["no_parent"] == 1


def test_the_touching_cross_street_really_is_nearer_by_minimum_distance():
    """Pins the trap the test above is guarding, so it cannot quietly stop
    being a trap: by minimum distance the cross street WINS (0m vs 8m), and
    only the median demotes it (36m, outside the 30m radius).
    """
    line = _line(_sidewalk(length_m=60.0))
    parent = _street_line(_parallel_street("Court Street", offset_m=8.0))
    crossing = _street_line(_cross_street("Union Street", at_m=0.0))

    probes = _probe_points(line)
    to_crossing = sorted(probe.distance(crossing) for probe in probes)
    to_parent = sorted(probe.distance(parent) for probe in probes)

    assert to_crossing[0] == pytest.approx(0.0)
    assert to_crossing[0] < to_parent[0]
    assert to_crossing[len(to_crossing) // 2] == pytest.approx(36.0)
    assert to_crossing[len(to_crossing) // 2] > PARENT_MAX_M


def test_a_parent_street_that_ends_partway_along_still_counts():
    """Streets are split at junctions, so a sidewalk routinely outruns the
    way beside it. Past that end the probe distances fan out, and only a
    MEDIAN stays inside the radius -- take the maximum instead and a real
    parent running dead alongside gets thrown away as too far.
    """
    edge = _sidewalk(length_m=60.0)
    parent = _street_ending_partway("Court Street", offset_m=8.0,
                                    ends_at_m=20.0)

    # Pins the discriminator, so this cannot quietly stop being one.
    distances = sorted(probe.distance(_street_line(parent))
                       for probe in _probe_points(_line(edge)))
    assert distances[len(distances) // 2] < PARENT_MAX_M < distances[-1]

    tally = assign_parent_names([edge], [parent])
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


# ── Candidates are grouped by NAME, not by way ───────────────────────────

def test_one_street_mapped_as_two_ways_is_one_candidate():
    """Court Street split at a junction is still Court Street. Counting the
    halves as rivals makes the clearest case in the city -- a sidewalk
    running dead alongside one street -- come out ambiguous and unnamed.
    """
    edge = _sidewalk()
    halves = [_parallel_street("Court Street", 10.0, osm_id=1),
              _parallel_street("Court Street", 12.0, osm_id=3)]
    tally = assign_parent_names([edge], halves)
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1
    assert tally["ambiguous"] == 0


def test_grouping_keeps_the_nearest_way_of_a_shared_name():
    """Court Street's near half (10m) is what it competes on, not its far
    half (24m). Keeping the wrong one flips the ranking and hands the edge
    to Union Street at 20m.
    """
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", 10.0, osm_id=1),
               _parallel_street("Court Street", 24.0, osm_id=3),
               _parallel_street("Union Street", 20.0, osm_id=5)]
    assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"


# ── The ambiguity ratio ──────────────────────────────────────────────────

def test_a_decisively_nearer_street_is_the_parent():
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", 6.0, osm_id=1),
               _parallel_street("Union Street", 20.0, osm_id=3)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


def test_two_comparable_streets_leave_the_edge_unnamed():
    """A wrong street name is worse than none -- it is confidently wrong."""
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", 15.0, osm_id=1),
               _parallel_street("Union Street", 20.0, osm_id=3)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == ""
    assert tally["ambiguous"] == 1
    assert tally["derived"] == 0


def test_the_ratio_boundary_is_inclusive():
    """12/20 is exactly AMBIGUOUS_RATIO, and the rule admits it. The flat
    `_to_m` scale makes these medians exactly 12.0 and 20.0, so this really
    is the tie and not a near-miss -- which is what makes it able to catch
    the boundary being tightened from `<=` to `<`.
    """
    edge = _sidewalk()
    near = _parallel_street("Court Street", 12.0, osm_id=1)
    far = _parallel_street("Union Street", 20.0, osm_id=3)

    line = _line(edge)
    probes = _probe_points(line)
    medians = []
    for street in (near, far):
        distances = sorted(p.distance(_street_line(street)) for p in probes)
        medians.append(distances[len(distances) // 2])
    assert medians[0] / medians[1] == AMBIGUOUS_RATIO  # the exact tie

    tally = assign_parent_names([edge], [near, far])
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


@pytest.mark.parametrize("nearest_m,expected_name,bucket", [
    (11.9, "Court Street", "derived"),    # ratio 0.595 -- inside
    (12.1, "", "ambiguous"),              # ratio 0.605 -- outside
])
def test_the_ratio_decides_either_side_of_the_boundary(
        nearest_m, expected_name, bucket):
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", nearest_m, osm_id=1),
               _parallel_street("Union Street", 20.0, osm_id=3)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == expected_name
    assert tally[bucket] == 1


def test_two_streets_both_touching_the_edge_are_ambiguous():
    """The runner-up is 0m away, so the ratio would divide by zero. Two
    streets both lying on the sidewalk is ambiguous by any reading anyway.
    """
    edge = _sidewalk()
    streets = [_parallel_street("Court Street", 0.0, osm_id=1),
               _parallel_street("Union Street", 0.0, osm_id=3)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == ""
    assert tally["ambiguous"] == 1


# ── Probe sampling ───────────────────────────────────────────────────────

def test_a_short_line_still_gets_three_probes():
    """Two probes would make the median an arbitrary pick between the two
    ends -- exactly the end-touching case the metric exists to survive.
    """
    assert len(_probe_points(_line(_sidewalk(length_m=6.0)))) == 3


def test_probes_are_spaced_about_ten_metres():
    assert len(_probe_points(_line(_sidewalk(length_m=100.0)))) == 10


def test_probes_span_the_whole_line():
    """Sampling that stops short of an end would miss the very geometry
    that distinguishes a parent street from a cross street.
    """
    line = _line(_sidewalk(length_m=60.0))
    probes = _probe_points(line)
    start, end = line.coords[0], line.coords[-1]
    assert probes[0].distance(LineString([start, start]).centroid) < 1e-6
    assert probes[-1].distance(LineString([end, end]).centroid) < 1e-6


# ── The tally is an exact partition ──────────────────────────────────────

def test_every_edge_lands_in_exactly_one_bucket():
    """The tally is what gets compared against the measured 94.0%. If the
    buckets don't partition the edges, that comparison is meaningless.
    """
    edges = [
        _sidewalk(name="Joralemon Street", origin=_origin(0)),   # own
        _sidewalk(origin=_origin(1)),                            # derived
        _sidewalk(length_m=1.0, origin=_origin(2)),              # too_short
        _sidewalk(origin=_origin(3)),                            # no_parent
        _sidewalk(origin=_origin(4)),                            # ambiguous
    ]
    streets = [
        _parallel_street("Court Street", 8.0, osm_id=1, origin=_origin(1)),
        _parallel_street("Court Street", 15.0, osm_id=3, origin=_origin(4)),
        _parallel_street("Union Street", 20.0, osm_id=5, origin=_origin(4)),
    ]
    tally = assign_parent_names(edges, streets)

    assert tally == {"own": 1, "derived": 1, "ambiguous": 1,
                     "no_parent": 1, "too_short": 1}
    assert sum(tally.values()) == len(edges)


def test_the_named_count_matches_the_edges_that_actually_have_names():
    edges = [
        _sidewalk(name="Joralemon Street", origin=_origin(0)),
        _sidewalk(origin=_origin(1)),
        _sidewalk(origin=_origin(2)),
    ]
    streets = [_parallel_street("Court Street", 8.0, osm_id=1,
                                origin=_origin(1))]
    tally = assign_parent_names(edges, streets)

    assert tally["own"] + tally["derived"] == sum(1 for e in edges if e["name"])


def test_edges_are_mutated_in_place():
    """The runner relies on this -- `assign_parent_names` returns only the
    tally, and the export writes the same list it passed in.
    """
    edges = [_sidewalk()]
    before = id(edges[0])
    assign_parent_names(edges, [_parallel_street("Court Street", 8.0)])
    assert id(edges[0]) == before
    assert edges[0]["name"] == "Court Street"


# ── Constants are pinned to the measurement that produced them ───────────

def test_constants_match_the_measurement_they_were_derived_from():
    """These three are not free parameters: 94.0% unambiguous was measured
    with exactly these values. Moving one silently invalidates that number,
    so it has to be a deliberate act with a re-measurement behind it.
    """
    assert PARENT_MAX_M == 30.0
    assert AMBIGUOUS_RATIO == 0.6
    assert MIN_NAMEABLE_LEN_M == 5.0


def test_the_degrees_to_metres_scale_matches_the_audit_instrument():
    """Deliberately the same flat approximation the audit used. A "better"
    projection here would be an improvement that invalidates the 94.0%
    without anything going red -- so this goes red instead.
    """
    assert _K == 111320.0 * math.cos(math.radians(40.7))
    assert _LAT_M == 110540.0
    assert _to_m(1.0, 1.0) == (_K, _LAT_M)


# ── The parallelism filter (2026-08-28) and fold_names ───────────────────

def test_a_perpendicular_street_cannot_lend_its_name():
    """THE class the filter was added for: a short corner scrap running
    along Court but sitting nearer to the cross street. By distance alone
    Union wins decisively (median 5m vs 9m, ratio 0.556 <= 0.6) -- the
    measured dominant error, a corner scrap taking the PERPENDICULAR
    street's name. Union runs north, the scrap runs east: 90 degrees
    apart, so the filter removes Union and Court wins alone.
    """
    edge = _sidewalk(length_m=8.0)
    streets = [_parallel_street("Court Street", offset_m=9.0, osm_id=1),
               _cross_street("Union Street", at_m=-1.0, osm_id=9)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1


def test_a_perpendicular_street_no_longer_causes_ambiguity():
    """The coverage half of the measured win (+27.5% named edges): a cross
    street inside the radius used to make the real parent look contested
    (median 10m vs 9m, ratio 0.9 -> ambiguous, no name). Perpendicular
    candidates no longer compete, so the parent is decisive.
    """
    edge = _sidewalk(length_m=12.0)
    streets = [_parallel_street("Court Street", offset_m=9.0, osm_id=1),
               _cross_street("Union Street", at_m=-4.0, osm_id=9)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == "Court Street"
    assert tally["derived"] == 1
    assert tally["ambiguous"] == 0


def test_an_ambiguous_edge_carries_fold_names_nearest_first():
    """No name, but not no information: direction rendering folds a
    nameless piece into an adjacent street run only when that run's name
    is among the piece's plausible parents."""
    edge = _sidewalk(length_m=60.0)
    streets = [_parallel_street("Court Street", offset_m=8.0, osm_id=1),
               _parallel_street("Smith Street", offset_m=-10.0, osm_id=5)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == ""
    assert tally["ambiguous"] == 1
    assert edge["fold_names"] == ["Court Street", "Smith Street"]


def test_a_too_short_nub_gets_proximity_only_fold_names():
    """A 3m corner nub has no measurable direction, so it gets no name --
    but it still knows which streets could own it, cross street included,
    which is what lets a kerb ramp at Court & Union fold into either
    neighbouring run."""
    edge = _sidewalk(length_m=3.0)
    streets = [_parallel_street("Court Street", offset_m=8.0, osm_id=1),
               _cross_street("Union Street", at_m=-2.0, osm_id=9)]
    tally = assign_parent_names([edge], streets)
    assert edge["name"] == ""
    assert tally["too_short"] == 1
    assert edge["fold_names"] == ["Union Street", "Court Street"]


def test_an_edge_with_nothing_in_range_has_no_fold_names():
    """A real park path is not foldable into anything: no candidates, no
    key at all -- the export stays small and the server's .get() default
    covers it."""
    edge = _sidewalk(length_m=60.0)
    tally = assign_parent_names([edge], [])
    assert edge["name"] == ""
    assert tally["no_parent"] == 1
    assert "fold_names" not in edge


def test_fold_names_are_capped():
    edge = _sidewalk(length_m=60.0)
    streets = [_parallel_street("A Street", offset_m=8.0, osm_id=1),
               _parallel_street("B Street", offset_m=-10.0, osm_id=3),
               _parallel_street("C Street", offset_m=12.0, osm_id=5),
               _parallel_street("D Street", offset_m=-14.0, osm_id=7)]
    assign_parent_names([edge], streets)
    assert edge["fold_names"] == ["A Street", "B Street", "C Street"]


def test_the_parallelism_constant_matches_its_derivation():
    """PARALLEL_MAX_DIFF_DEG = 30 is what measure_naming_precision.py
    measured the P5 win with (RIGHT 75.9 / WRONG 3.5 by length on the
    street-achievable subset, 2026-08-28). Changing it invalidates those
    numbers -- deliberate act, re-measurement required."""
    assert PARALLEL_MAX_DIFF_DEG == 30.0
    assert MAX_FOLD_NAMES == 3
