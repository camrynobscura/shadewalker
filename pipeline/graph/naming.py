"""Give nameless sidewalks the name of the street they run along.

WHY THIS EXISTS
---------------
OSM sidewalks are unnamed. Only 2.7% of NYC's 249,665 `footway=sidewalk`
ways carry a name, and OSM's own `associatedStreet` relation covers 0.7%
of them (83 relations citywide). Without a derived name, turn-by-turn
directions read "Head 2,482 m along unnamed path" -- one instruction for
a mile and a half -- into an aria-live region that WCAG 2.2 AA requires
to be useful.

IS THIS AN OVERRIDE?
--------------------
No, and the distinction is the governing rule's own. Deriving a label
adds no way, removes none, and connects nothing; its worst failure is a
confusing direction, never a wrong route. Same category as tree data.
The routing graph is untouched by this module.

THE RULE
--------
1. A way's own name wins. OSM said so; we don't second-guess it, and that
   includes its typos -- `Brookyln Bridge Promenade` stays misspelled.
2. Otherwise, the nearest named street the edge runs alongside (within
   PARENT_MAX_M and roughly parallel -- PARALLEL_MAX_DIFF_DEG), but only
   if it is decisively nearer than the nearest differently-named such
   street. Parallelism matters because the dominant error otherwise is
   corner scraps taking the perpendicular cross street's name
   (measurements at PARALLEL_MAX_DIFF_DEG below).
3. Ambiguous or nothing in range emits no name -- but carries
   `fold_names` (the plausible parents) so direction rendering can fold
   nameless scraps into the street run they belong to on evidence
   instead of a length threshold. A wrong street name is worse than
   none: it sends someone to the wrong corner confidently.

MEASURED, NOT ASSUMED
---------------------
Over 8,000 sidewalks: 94.0% unambiguous, 2.2% ambiguous, 3.8% with no
named street in range. Distance is the median sampled along the line, not
the minimum: a block-length sidewalk touches a different cross street at
each end, so minimum distance calls almost everything ambiguous.

That measurement sampled ways. This module names edges, which are the
chains between junctions and so run shorter (median 16m); the rates are
re-measured after a real run by tools/audit/measure_naming_precision.py
rather than assumed to carry over.
"""

import logging
import math

from shapely.geometry import LineString
from shapely.strtree import STRtree

from pipeline import config

logger = logging.getLogger(__name__)

# How far from a sidewalk to look for its parent street. A NYC sidewalk
# sits roughly 5-20m from its street's middle; past 30m the nearest street
# is more likely a different one than a far-set parent.
PARENT_MAX_M = 30.0

# The nearest name must be this much closer than the nearest differently
# named street to count as unambiguous. Shared deliberately with
# tools/audit/test_tree_side_assignment.py: both ask "is the nearest
# candidate decisively nearer than the runner-up?"
AMBIGUOUS_RATIO = 0.6

# Edges shorter than this are corner nubs, kerb ramps and crossing stubs.
# They carry little route length and their nearest-street answer is noisy,
# so they inherit from their neighbours at direction-rendering time rather
# than guessing here. (They still get fold_names below -- proximity only,
# since a 2m nub has no measurable direction of its own.)
MIN_NAMEABLE_LEN_M = 5.0

# A street may only lend its name to a sidewalk that runs alongside it:
# median local bearing difference over the probes at most this. Measured
# 2026-08-28 by tools/audit/measure_naming_precision.py (holdout over the
# 12,159 own-named edges): the filter moved the street-achievable subset,
# by length walked, from right 69.8 / none 24.8 / wrong 5.4 to right 75.9
# / none 20.6 / wrong 3.5 -- better on every axis at once -- and named
# 27.5% more edges citywide, because a perpendicular cross street no
# longer competes at corners. 30 degrees sits far from a cross street's
# 90 while tolerating curved streets.
PARALLEL_MAX_DIFF_DEG = 30.0

# How many plausible parent names an unnamed edge carries out of this
# module (edge["fold_names"]), nearest first. Direction rendering uses
# them as folding evidence -- "does this nameless scrap belong to the
# street run it interrupts?" -- instead of a bare length threshold. Named
# edges carry none; their name is their evidence.
MAX_FOLD_NAMES = 3

# Degrees -> metres, flat, at NYC's latitude. The error across the city's
# latitude span (40.47-40.92) is ~0.7%, i.e. ~0.2m on the 30m threshold,
# which cannot move a decision this rule makes. Every pipeline measurement
# uses this same approximation so their distances stay comparable.
_K = 111320.0 * math.cos(math.radians(40.7))
_LAT_M = 110540.0


def _to_m(lon: float, lat: float) -> tuple[float, float]:
    return (lon * _K, lat * _LAT_M)


def _probe_points(line: LineString) -> list:
    """Points sampled along a line, for median-distance measurement.

    One probe per ~10m, at least 3. The median over these is what
    separates a parent street (alongside for the whole length) from a
    cross street (close at one end, far everywhere else).
    """
    count = max(3, int(line.length // 10))
    return [line.interpolate(line.length * i / (count - 1))
            for i in range(count)]


def _local_bearing(line: LineString, s: float) -> float:
    """The line's direction around distance s along it, degrees mod 180
    (undirected -- a street and a sidewalk running opposite ways are
    still parallel), from a 4m chord centred there."""
    a = line.interpolate(max(s - 2.0, 0.0))
    b = line.interpolate(min(s + 2.0, line.length))
    if a.x == b.x and a.y == b.y:
        return 0.0
    return math.degrees(math.atan2(b.x - a.x, b.y - a.y)) % 180.0


def _street_index(streets) -> tuple[STRtree, list, list]:
    """An STRtree over street geometries in metres, plus their names."""
    geometries, names = [], []
    for way in streets:
        points = [_to_m(lon, lat) for lon, lat in zip(way.lons, way.lats)]
        if len(points) < 2:
            continue
        geometries.append(LineString(points))
        names.append(way.name)
    return STRtree(geometries), geometries, names


def _candidate_names(line, probes, probe_bearings, index, geometries,
                     names) -> list:
    """Nearby streets that could plausibly own this edge, as
    [(name, median_m)] nearest first. Distance is per street name: two
    ways both called "Court Street" are one candidate, not two.

    probe_bearings=None skips the parallelism filter -- used for
    sub-MIN_NAMEABLE nubs, whose own direction is noise.
    """
    by_name: dict[str, float] = {}
    for position in index.query(line.buffer(PARENT_MAX_M)):
        street = geometries[position]
        if probe_bearings is not None:
            differences = []
            for probe, edge_bearing in zip(probes, probe_bearings):
                street_bearing = _local_bearing(street, street.project(probe))
                d = abs(edge_bearing - street_bearing)
                differences.append(min(d, 180.0 - d))
            differences.sort()
            if differences[len(differences) // 2] > PARALLEL_MAX_DIFF_DEG:
                continue
        distances = sorted(probe.distance(street) for probe in probes)
        median = distances[len(distances) // 2]
        if median > PARENT_MAX_M:
            continue
        name = names[position]
        if name not in by_name or median < by_name[name]:
            by_name[name] = median
    return sorted(by_name.items(), key=lambda item: item[1])


def _is_connector_kind(kind: str) -> bool:
    """Same rule as server/graph_store.py's _is_connector_kind, over the
    shared config vocabulary — the two must agree: the server folds these
    legs into the street runs around them and never renders their name,
    which is what makes skipping them here safe."""
    return any(marker in kind for marker in config.CONNECTOR_KIND_MARKERS)


def assign_parent_names(edges: list[dict], streets) -> dict:
    """Fill in `name` on every edge that hasn't got one. Mutates `edges`.

    Connector kinds (crossings, traffic islands) are skipped outright:
    the server never renders a name or reads fold_names for them, so
    deriving either is build time spent on output nobody can see. An OSM
    name of the connector's own is kept — the skip avoids work, it does
    not delete data.

    Edges that end up without a name get `fold_names` instead (up to
    MAX_FOLD_NAMES plausible parents, nearest first) so direction
    rendering can decide "does this nameless piece belong to the street
    run around it?" from evidence rather than a length threshold.

    Returns a tally of what happened, for the caller to log and for the
    audit to compare against the measured baselines
    (tools/audit/measure_naming_precision.py).
    """
    index, geometries, names = _street_index(streets)
    logger.info(f"  [naming] {len(geometries):,} named street ways indexed")

    tally = {"own": 0, "derived": 0, "ambiguous": 0, "no_parent": 0,
             "too_short": 0, "connector": 0}

    for edge in edges:
        if edge["name"]:
            tally["own"] += 1
            continue

        if _is_connector_kind(edge["kind"]):
            # ~105k crossing/traffic-island edges would otherwise go
            # through probes and the street index for names the server
            # folds away unseen (graph_store._foldable_into short-circuits
            # on kind; _display_name blanks it; fold_names is never
            # consulted for connector legs).
            tally["connector"] += 1
            continue

        line = LineString([_to_m(lon, lat) for lon, lat in edge["coords"]])
        probes = _probe_points(line)

        if line.length < MIN_NAMEABLE_LEN_M:
            tally["too_short"] += 1
            ranked = _candidate_names(line, probes, None, index, geometries,
                                      names)
            if ranked:
                edge["fold_names"] = [n for n, _ in ranked[:MAX_FOLD_NAMES]]
            continue

        probe_bearings = [_local_bearing(line, line.project(probe))
                          for probe in probes]
        ranked = _candidate_names(line, probes, probe_bearings, index,
                                  geometries, names)

        if not ranked:
            tally["no_parent"] += 1
            continue

        if len(ranked) == 1:
            edge["name"] = ranked[0][0]
            tally["derived"] += 1
            continue

        nearest, runner_up = ranked[0][1], ranked[1][1]
        # runner_up of 0 would divide by zero; two streets both touching
        # the edge is ambiguous by any reading.
        decisive = runner_up > 0 and nearest / runner_up <= AMBIGUOUS_RATIO
        if decisive:
            edge["name"] = ranked[0][0]
            tally["derived"] += 1
        else:
            tally["ambiguous"] += 1
            edge["fold_names"] = [n for n, _ in ranked[:MAX_FOLD_NAMES]]

    total = len(edges)
    named = tally["own"] + tally["derived"]
    logger.info(
        f"  [naming] {named:,}/{total:,} edges named ({named / total * 100:.1f}%) "
        f"-- {tally['own']:,} own, {tally['derived']:,} derived; "
        f"unnamed: {tally['ambiguous']:,} ambiguous, "
        f"{tally['no_parent']:,} no parent, {tally['too_short']:,} too short, "
        f"{tally['connector']:,} connectors skipped (names never render)"
    )
    return tally
