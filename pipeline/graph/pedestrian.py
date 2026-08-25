"""OSM's pedestrian ways → the routing graph.

Replaces the centerline model. There is no fetch step: the pinned extract
(`data/oracle/new-york-latest.osm.pbf`) is read straight off disk, in one
pass, for the whole city. No Overpass, no tiles, no cache versioning.

THE GOVERNING RULE
------------------
If OSM says a way is a pedestrian way, it is in the graph. `is_pedestrian`
below is the *whole* rule — there are no zone exclusions, no gap ledger, no
derived connectors, and no location-specific exceptions. Coverage gaps stay
gaps. Read `history/sidewalk-model-decision.md` before changing the filter:
the coverage figures quoted in CLAUDE.md were measured with exactly this
predicate, so a change here silently invalidates them.

THE EXTRACT IS THE WHOLE STATE
------------------------------
`new-york-latest.osm.pbf` is Geofabrik's NEW YORK STATE extract — measured
extent of its pedestrian ways alone is -79.738,40.496 .. -71.856,45.035,
and only 46.2% of them touch the five boroughs. So every read MUST be
clipped to NYC's real boundary, which is why `build()` requires a shape
rather than defaulting to one.

Skipping the clip is not a cosmetic error. Unclipped, the graph is
868,339 nodes instead of 386,651, and its largest connected component
reads 37.1% instead of 81.9% — a number that looks exactly like a routing
catastrophe and is purely an artifact of measuring Buffalo alongside
Brooklyn. This was measured on 2026-08-22, after the omission produced
precisely that false alarm.

The centerline model had a separate step for this (`boundary.clip_to_nyc`),
orphaned when `run_tile.py` was deleted and still uncalled. This module does
not use it: it clips way by way inside read_ways(), against a prepared
boundary, before a graph exists at all.

WHAT AN EDGE IS
---------------
An OSM way is not a graph edge. Mappers split ways for their own reasons (a
surface change, a bridge, an editing session), so one way can span many
junctions and one junction can sit mid-way. So ways are re-split at
JUNCTIONS — nodes shared by two or more ways, plus every way's own
endpoints — and the chain of shape points between two junctions becomes one
edge carrying its full geometry. This is the same topology osmnx's
`simplify_graph()` produced for the centerline model, computed directly.

COORDINATES
-----------
Everything here is EPSG:4326 (lon/lat degrees) because that is what the
export and the frontend draw with. Lengths are NOT computed in degrees:
they come from `pyproj.Geod`, which measures on the WGS84 ellipsoid and so
needs no projected CRS at all. Any later step that BUFFERS or MEASURES
AREA still has to reproject (see `config.METRIC_CRS`) — buffering in
degrees is this project's #1 bug class.
"""

import logging
from typing import NamedTuple

import numpy as np
import osmium
from osmium.filter import EntityFilter
from pyproj import Geod
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

logger = logging.getLogger(__name__)

# The `highway` values that ARE dedicated pedestrian infrastructure.
# Street centerlines are deliberately absent -- that is the whole point of
# the model. `footway` covers both sidewalks (`footway=sidewalk`) and
# crossings (`footway=crossing`); crossings are what join sidewalks to each
# other, so a "sidewalks only" graph is disconnected by construction.
PED_HIGHWAY = frozenset({"footway", "path", "steps", "pedestrian"})

# Street centerlines. NOT part of the routing graph -- they are read only
# so a nameless sidewalk can borrow the name of the street it runs along
# (pipeline/graph/naming.py). Same set the coverage audit uses to decide
# "is this somewhere a person would plausibly walk".
STREET_HIGHWAY = frozenset({
    "primary", "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "unclassified", "residential", "living_street",
    "service", "track",
})

# WGS84 ellipsoid, for geodesic segment lengths.
_GEOD = Geod(ellps="WGS84")


class Way(NamedTuple):
    """One OSM way we kept, reduced to what the graph needs."""
    osm_id: int
    name: str
    node_ids: list[int]
    lons: list[float]
    lats: list[float]
    # OSM's own `highway`, plus `/<footway>` when it has one, e.g.
    # "footway/sidewalk", "footway/crossing", "steps". Shade scoring needs
    # this: a crossing runs ACROSS a roadway, so it has no block face and
    # scores no shade, and it must not be counted into a block's pavement
    # length either or it would dilute that block's density.
    # Defaulted so the three existing keyword-only call sites (this module,
    # tools/audit/measure_sidewalk_kerb_match.py, tests) keep working.
    kind: str = ""


def is_pedestrian(tags: dict) -> bool:
    """Whether OSM marks this way as something a pedestrian may walk on.

    Byte-for-byte the predicate used by
    `tools/audit/measure_sidewalk_only_coverage.py`, which produced the
    coverage measurements the model was chosen on. That tool imports this
    function rather than keeping its own copy, so the shipped filter and
    the measured filter cannot drift apart.

    The access logic is OSM's own tag hierarchy, not a house rule:
    `foot=no|private` excludes; a blanket `access=private|no` excludes
    UNLESS `foot=yes|designated` overrides it (real on NYC bridge
    walkways); a `cycleway` counts only where foot access is explicit.
    `area=yes` is a polygon (a plaza's interior fill), not a line to
    route along -- its edges are mapped separately as real ways.
    """
    hw = tags.get("highway")
    if tags.get("area") == "yes":
        return False
    if tags.get("foot") in ("no", "private"):
        return False
    if tags.get("access") in ("private", "no") and \
            tags.get("foot") not in ("yes", "designated"):
        return False
    if hw == "cycleway":
        return tags.get("foot") in ("yes", "designated")
    return hw in PED_HIGHWAY


def is_named_street(tags: dict) -> bool:
    """A street centerline carrying a name, for the naming derivation only.

    Nameless streets are useless as a parent (they can't lend a name), so
    they're filtered here rather than downstream. Service roads that are
    private or a driveway are excluded for the same reason the coverage
    audit excludes them: a sidewalk's parent is the street it runs along,
    not the parking aisle behind it.
    """
    if not tags.get("name"):
        return False
    if tags.get("highway") not in STREET_HIGHWAY or tags.get("area") == "yes":
        return False
    if tags.get("service") in ("private", "driveway", "parking_aisle"):
        return False
    return True


def read_ways(pbf_path, nyc_shape: BaseGeometry) -> tuple[list[Way], list[Way]]:
    """One pass over the extract, returning (pedestrian ways, named streets).

    Both come out of a single read on purpose. The graph needs the first
    and the naming derivation needs the second, and a second pass over the
    472MB extract costs ~110s for data the first pass already streamed
    past. A way can appear in both lists only if it is somehow tagged as
    both, which OSM does not do.

    `.with_locations()` makes pyosmium attach each node's coordinates to
    the way as it streams, so no separate node pass or id->coord table is
    needed. Nodes whose location is missing (a way referencing a node the
    extract itself cut off) are dropped; a way left with fewer than two
    points carries no length and is skipped entirely.

    A way is KEPT WHOLE if ANY of its points is inside NYC, rather than
    having its outside-NYC points removed. Bridges are the reason: the
    Verrazzano and the GWB approaches cross the boundary, and dropping
    the far points would leave a span ending in mid-air. The out-of-city
    tail of a kept way is a dangling stub that routes nowhere, which is
    harmless; a severed bridge is not.

    The containment test is exact (shapely `contains`), not a grid
    approximation. Two things keep it fast enough to be worth the
    accuracy: a bounding-box reject handles the ~54% of ways that are
    nowhere near the city without any geometry work, and the per-point
    loop stops at the first point found inside.
    """
    pedestrian_ways: list[Way] = []
    street_ways: list[Way] = []
    prepared = prep(nyc_shape)
    bounds = nyc_shape.bounds

    processor = (osmium.FileProcessor(pbf_path,
                                      osmium.osm.NODE | osmium.osm.WAY)
                 .with_locations()
                 .with_filter(EntityFilter(osmium.osm.WAY)))
    for way in processor:
        tags = dict(way.tags)
        if not tags:
            continue
        walkable = is_pedestrian(tags)
        street = is_named_street(tags)
        if not (walkable or street):
            continue
        node_ids, lons, lats = [], [], []
        for node in way.nodes:
            if not node.location.valid():
                continue
            node_ids.append(node.ref)
            lons.append(node.lon)
            lats.append(node.lat)
        if len(node_ids) < 2:
            continue
        if not _touches_nyc(prepared, lons, lats, *bounds):
            continue
        footway = tags.get("footway")
        parsed = Way(osm_id=way.id, name=_normalize_name(tags.get("name")),
                     node_ids=node_ids, lons=lons, lats=lats,
                     kind=(tags.get("highway") or "?")
                          + (f"/{footway}" if footway else ""))
        if walkable:
            pedestrian_ways.append(parsed)
        if street:
            street_ways.append(parsed)
    return pedestrian_ways, street_ways


def _touches_nyc(prepared, lons, lats,
                 min_lon, min_lat, max_lon, max_lat) -> bool:
    """Whether any of a way's points lies inside NYC's real boundary.

    The bounding-box pass first: a point outside the city's bbox cannot
    be inside the city, and a plain float comparison is far cheaper than
    a polygon test. Only points that survive it get the real check.
    """
    for lon, lat in zip(lons, lats):
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if prepared.contains(Point(lon, lat)):
            return True
    return False


def find_junctions(ways: list[Way]) -> set[int]:
    """Node ids where an edge must start or end.

    Three cases, all genuine topology rather than tuning:

    1. A node used by two or more ways -- a real junction.
    2. A way's own first and last node, which are where it meets whatever
       continues past it (and are junctions by case 1 whenever something
       does).
    3. A node a single way visits twice -- a loop closing on itself, or a
       lollipop path. Without this the chain would run through its own
       start and the edge would be a self-intersecting line.
    """
    uses: dict[int, int] = {}
    junctions: set[int] = set()
    for way in ways:
        seen_in_this_way: set[int] = set()
        for node_id in way.node_ids:
            if node_id in seen_in_this_way:
                junctions.add(node_id)          # case 3
            seen_in_this_way.add(node_id)
        for node_id in seen_in_this_way:
            uses[node_id] = uses.get(node_id, 0) + 1
        junctions.add(way.node_ids[0])          # case 2
        junctions.add(way.node_ids[-1])
    for node_id, count in uses.items():
        if count >= 2:
            junctions.add(node_id)              # case 1
    return junctions


def _split_way(way: Way, junctions: set[int]) -> list[tuple]:
    """Cut one way into (node_ids, lons, lats) chains between junctions.

    A chain always begins and ends on a junction, because a way's own
    endpoints are junctions by construction (`find_junctions` case 2).
    """
    chains = []
    start = 0
    for position in range(1, len(way.node_ids)):
        if way.node_ids[position] not in junctions:
            continue
        stop = position + 1
        chains.append((way.node_ids[start:stop],
                       way.lons[start:stop],
                       way.lats[start:stop]))
        start = position
    return chains


def _chain_lengths_m(chains: list[tuple]) -> np.ndarray:
    """Geodesic length of every chain, in meters, in one batched call.

    Every consecutive point pair from every chain goes into one flat pair
    of arrays so `Geod.inv` runs once over the whole city rather than a
    million times. `np.add.reduceat` then sums each chain's own slice of
    the result. A per-chain call gives the same numbers; this is the same
    arithmetic, batched, and turns minutes into seconds.
    """
    lon_a, lat_a, lon_b, lat_b = [], [], [], []
    starts, offset = [], 0
    for _, lons, lats in chains:
        starts.append(offset)
        lon_a.extend(lons[:-1])
        lat_a.extend(lats[:-1])
        lon_b.extend(lons[1:])
        lat_b.extend(lats[1:])
        offset += len(lons) - 1
    if not lon_a:
        return np.zeros(len(chains))
    _, _, seg = _GEOD.inv(np.array(lon_a), np.array(lat_a),
                          np.array(lon_b), np.array(lat_b))
    return np.add.reduceat(seg, starts)


def _normalize_name(raw) -> str:
    """OSM's `name` as a plain string; unnamed ways become "".

    Unnamed is the overwhelming majority here: only 2.7% of NYC's
    `footway=sidewalk` ways carry a name of their own. The parent-street
    derivation that fills the rest in is a separate step.
    """
    if isinstance(raw, str):
        return raw
    return ""


def build_graph(ways: list[Way]) -> tuple[dict, list[dict]]:
    """Turn pedestrian ways into (nodes, edges) for the export.

    Takes ways rather than a path so the caller can read pedestrian ways
    and named streets in ONE pass (`read_ways`) and hand the naming step
    the second list. Get the ways with:

        from pipeline.fetch.boundaries import fetch_borough_boundaries
        from pipeline.graph.boundary import nyc_boundary
        ped, streets = read_ways(path, nyc_boundary(fetch_borough_boundaries()))

    That boundary is deliberately the water-INCLUDED dataset: a bridge's
    midspan sits over water, and excluding it once severed every
    inter-borough crossing (pipeline/fetch/boundaries.py's docstring
    carries that history).

    nodes: {"<osm node id>": [lon, lat]} -- only the junction nodes that
           are actually an edge endpoint. Shape points live inside an
           edge's own `coords` and are not routable, exactly as the
           centerline model treated them.
    edges: one dict per edge, in the export's own vocabulary. Tree fields
           are absent -- scoring is a later step and owns them. `name` is
           the way's OWN name where OSM gave it one and "" otherwise;
           pipeline/graph/naming.py fills the blanks in afterwards.
    """
    junctions = find_junctions(ways)
    logger.info(f"  [pedestrian] {len(junctions):,} junction nodes")

    chains, names, kinds = [], [], []
    for way in ways:
        for chain in _split_way(way, junctions):
            chains.append(chain)
            names.append(way.name)
            kinds.append(way.kind)

    lengths = _chain_lengths_m(chains)

    nodes: dict[str, list[float]] = {}
    edges: list[dict] = []
    # A (u, v) pair can legitimately carry more than one edge -- two
    # separate paths between the same junctions. `key` distinguishes them,
    # matching the export contract the server dedupes on.
    keys: dict[tuple, int] = {}
    for (node_ids, lons, lats), name, kind, length_m in zip(
            chains, names, kinds, lengths):
        u, v = str(node_ids[0]), str(node_ids[-1])
        nodes.setdefault(u, [round(lons[0], 6), round(lats[0], 6)])
        nodes.setdefault(v, [round(lons[-1], 6), round(lats[-1], 6)])
        pair = tuple(sorted((u, v)))
        key = keys.get(pair, 0)
        keys[pair] = key + 1
        edges.append({
            "u": u,
            "v": v,
            "key": key,
            # "C" is a placeholder the spine does not use. Every edge here
            # is already one real pavement, so the field's future meaning
            # is which side of the parent street it is ("north side of
            # Court Street") -- assigned by the per-side scoring step.
            "side": "C",
            "length_m": round(float(length_m), 1),
            "name": name,
            # OSM's own classification, carried through so scoring can tell
            # a sidewalk from a crossing. Not exported: the server has no
            # use for it, and the export is a contract worth keeping small.
            "kind": kind,
            "coords": [[round(lon, 6), round(lat, 6)]
                       for lon, lat in zip(lons, lats)],
        })

    logger.info(f"  [pedestrian] {len(nodes):,} nodes, {len(edges):,} edges")
    return nodes, edges
