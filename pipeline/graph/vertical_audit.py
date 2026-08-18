"""Vertical-suspects detector (FIXES.md item 0): find pipeline-made weld
edges that may jump between ground and an elevated structure.

REPORT-ONLY, by design. The 2026-08-14 offline validation (five rule
iterations against the previous day's fully-labeled phantom audit,
data/audits/2026-08-14/veto_classifier.py) proved automatic removal can't
be made safe: a third of confirmed phantoms have no elevation-tagged OSM
way near them at all; phantom meshes alibi each other under reachability
arbitration; and the rule's most confident tier still condemned a real
park entrance (Brooklyn Heights Promenade / Clark St) that only human
imagery review cleared. So this module DETECTS and reports; removal stays
evidence-gated in the server's coordinate blocklist
(server/phantom_connectors.json), with human field checks where eyes
decide (see history/phantom-vertical-connectors.md).

The rule, validated offline (same thresholds):
  a weld edge is SUSPECT when exactly one endpoint lies on the interior
  of an elevated walkable way (bridge != no/boardwalk, or layer > 0)
  while the other endpoint is nowhere near that way -- either attached to
  the way itself (shape i: the snap split the way's own edge) or with its
  synthetic path running along the way (shape ii: the imported path IS
  the deck, e.g. NYC's data mapping the High Line's own paths). A way
  terminus is exempt (structures land at grade) unless another elevated
  way continues past it (OSM splits one structure into several ways).
  A suspect is REPORTED only if the graph minus all suspects, plus the
  full sidewalk layer (OSM's true pedestrian reality -- the ramps our
  centerline filters exclude live there), cannot walk between the weld's
  endpoints within ARBITER_MAX_M: the same harm bar the shipped per-edge
  phantom pass used (real walk > 300m).

Runs pre-simplify, where osmids are still scalar and the weld=True
attribute marks exactly the edges the pipeline manufactured.
"""

import logging
import json

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

from pipeline import config
from pipeline.graph.centerline import METRIC_CRS

logger = logging.getLogger(__name__)


_TO_METRIC = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform

ON_M = 10.0            # "on the way" horizontal tolerance (audit-proven)
ATTACH_M = 1.5         # a split node sits ON the target way's own line
TERMINUS_M = 30.0      # this close to a structure's end = at-grade landing...
CONTINUATION_M = 5.0   # ...unless another elevated way continues within this
OVERLAP_NEAR_M = 12.0  # "synthetic path runs along the way" tolerance
OVERLAP_MIN_FRAC = 0.5
OVERLAP_WALK_M = 250.0
ARBITER_MAX_M = 300.0  # the shipped harm bar: a real walk longer than this
                       # (or none) makes a suspect worth reporting

REPORTS_DIR = config.RAW_DIR / "vertical_suspects"

WALKABLE_HIGHWAYS = {"footway", "cycleway", "pedestrian", "path", "steps",
                     "living_street", "track", "bridleway"}


def _scalar(value):
    """Unsimplified osmnx attrs are scalars, but be safe on lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _is_walkable(tags: dict) -> bool:
    if _scalar(tags.get("foot")) in ("yes", "designated", "permissive"):
        return True
    return (_scalar(tags.get("highway")) in WALKABLE_HIGHWAYS
            and _scalar(tags.get("foot")) != "no")


def _is_elevated(tags: dict) -> bool:
    bridge = _scalar(tags.get("bridge"))
    if bridge not in (None, "no", "boardwalk"):
        return True
    try:
        return int(_scalar(tags.get("layer")) or 0) > 0
    except (TypeError, ValueError):
        return False


def _node_point_m(graph: nx.MultiDiGraph, node) -> Point:
    return Point(_TO_METRIC(graph.nodes[node]["x"], graph.nodes[node]["y"]))


def _elevated_way_lines(graph: nx.MultiDiGraph):
    """One merged metric line per elevated OSM way in the graph, plus its
    tags -- edges grouped by osmid (scalar pre-simplify), each edge
    contributing its own straight node-to-node segment (unsimplified
    edges carry no geometry attr; node coords are the geometry)."""
    segments_by_way: dict = {}
    tags_by_way: dict = {}
    for u, v, data in graph.edges(data=True):
        if not _is_elevated(data):
            continue
        osmid = _scalar(data.get("osmid"))
        if not isinstance(osmid, int) or osmid < 0:
            continue  # synthetic edges carry no OSM elevation truth
        a, b = _node_point_m(graph, u), _node_point_m(graph, v)
        if a.distance(b) < 0.01:
            continue
        segments_by_way.setdefault(osmid, []).append(LineString([a, b]))
        tags_by_way[osmid] = data
    lines, meta = [], []
    for osmid, segments in segments_by_way.items():
        merged = unary_union(segments)
        # a one-edge way unions straight to a bare LineString, which
        # linemerge() refuses (it wants a collection) -- real crash on the
        # pilot tile's single-segment footway bridge, 2026-08-14
        if not isinstance(merged, LineString):
            merged = linemerge(merged)
        parts = merged.geoms if hasattr(merged, "geoms") else [merged]
        for part in parts:
            if isinstance(part, LineString) and part.length >= 1.0:
                lines.append(part)
                meta.append({"osmid": osmid, "tags": tags_by_way[osmid]})
    return lines, meta


def _synthetic_path_overlap(graph, start_node, skip_node, line_m) -> float:
    """Fraction of the synthetic path reachable from start_node (never
    through skip_node, never across a weld) that lies within
    OVERLAP_NEAR_M of line_m -- "does the imported path ride this
    structure" (shape ii)."""
    undirected = graph.to_undirected(as_view=True)
    seen = {start_node, skip_node}
    frontier = [(start_node, 0.0)]
    points = []
    while frontier:
        node, dist = frontier.pop()
        for neighbor in undirected.neighbors(node):
            if neighbor in seen:
                continue
            edge_datas = undirected.get_edge_data(node, neighbor)
            data = next(iter(edge_datas.values()))
            osmid = _scalar(data.get("osmid"))
            if not isinstance(osmid, int) or osmid >= 0 or data.get("weld"):
                continue  # only walk the imported path itself
            length = float(data.get("length", 0.0))
            if dist + length > OVERLAP_WALK_M:
                continue
            seen.add(neighbor)
            points.append(_node_point_m(graph, neighbor))
            frontier.append((neighbor, dist + length))
    if not points:
        return 0.0
    near = sum(1 for p in points if p.distance(line_m) <= OVERLAP_NEAR_M)
    return near / len(points)


def _suspect_reason(graph, u, v, lines, meta, tree):
    """The first elevated way that makes weld (u, v) geometrically
    suspicious, or None. See the module docstring for the rule."""
    p_u, p_v = _node_point_m(graph, u), _node_point_m(graph, v)
    candidates = set(tree.query(p_u, predicate="dwithin", distance=ON_M)) | set(
        tree.query(p_v, predicate="dwithin", distance=ON_M))
    for li in candidates:
        line = lines[li]
        if not _is_walkable(meta[li]["tags"]):
            continue  # nothing non-walkable is a snap target or a deck
        d_u, d_v = p_u.distance(line), p_v.distance(line)
        if (d_u <= ON_M) == (d_v <= ON_M):
            continue  # both on it (path along the deck) or both off: fine
        on_point, on_node, off_node = (p_u, u, v) if d_u <= ON_M else (p_v, v, u)
        # terminus exemption: structures land at grade at their ends --
        # unless the structure continues as another elevated way there
        exempt = False
        for terminus in (Point(line.coords[0]), Point(line.coords[-1])):
            if on_point.distance(terminus) > TERMINUS_M:
                continue
            continues = any(
                ti != li and lines[ti].distance(terminus) <= CONTINUATION_M
                for ti in tree.query(terminus, predicate="dwithin",
                                     distance=CONTINUATION_M)
            )
            if not continues:
                exempt = True
                break
        if exempt:
            continue
        shape_i = min(d_u, d_v) <= ATTACH_M
        shape_ii = False
        if not shape_i and isinstance(on_node, int) and on_node < 0:
            shape_ii = (_synthetic_path_overlap(graph, on_node, off_node, line)
                        >= OVERLAP_MIN_FRAC)
        if shape_i or shape_ii:
            return {"way": meta[li]["osmid"],
                    "way_name": _scalar(meta[li]["tags"].get("name")) or "",
                    "shape": "i" if shape_i else "ii"}
    return None


def report_vertical_suspects(
    graph: nx.MultiDiGraph,
    sidewalk_graph: nx.MultiDiGraph | None,
    tile_id: str,
) -> list[dict]:
    """Audit every weld=True edge; write and return the suspects report.

    sidewalk_graph is the tile's FULL (un-narrowed) ANY_SIDEWALK_FILTER
    fetch when available -- arbitration must see OSM's true pedestrian
    reality, or welds that substitute for filtered-out ramps read as
    unreachable and get wrongly flagged (measured: Kosciuszko Bridge
    landing). None (pilot/CI without park data) just means arbitration
    runs on the walk graph alone -- suspects lists there skew
    conservative, and the report says so.
    """
    welds = [(u, v, k) for u, v, k, data in graph.edges(keys=True, data=True)
             if data.get("weld")]
    if not welds:
        return []

    lines, meta = _elevated_way_lines(graph)
    if not lines:
        return []
    tree = STRtree(lines)

    suspects = []
    for u, v, k in welds:
        reason = _suspect_reason(graph, u, v, lines, meta, tree)
        if reason is not None:
            suspects.append((u, v, k, reason))
    if not suspects:
        return []

    # arbitration graph: everything except the suspect welds themselves
    # (excluded JOINTLY, so a phantom mesh can't alibi its own members),
    # plus the sidewalk layer, which shares real OSM node ids and joins
    # naturally
    arb = graph.copy()
    arb.remove_edges_from([(u, v, k) for u, v, k, _ in suspects])
    if sidewalk_graph is not None:
        arb = nx.compose(arb, sidewalk_graph)
    arb = arb.to_undirected(as_view=False)

    report = []
    for u, v, k, reason in suspects:
        try:
            walk_m = nx.dijkstra_path_length(arb, u, v, weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            walk_m = None
        if walk_m is not None and walk_m <= ARBITER_MAX_M:
            continue  # a real walk exists; the weld merely shortcuts it
        report.append({
            "u": str(u), "v": str(v),
            "lat": round(graph.nodes[u]["y"], 6), "lon": round(graph.nodes[u]["x"], 6),
            "weld_m": round(float(graph.edges[u, v, k].get("length", 0.0)), 1),
            "arbiter_walk_m": None if walk_m is None else round(walk_m, 1),
            "sidewalks_in_arbitration": sidewalk_graph is not None,
            **reason,
        })

    if report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORTS_DIR / f"{tile_id}.json"
        with open(out_path, "w") as f:
            json.dump(report, f, indent=1)
        logger.info(f"  [vertical] {tile_id}: {len(report)} suspect weld(s) of "
              f"{len(welds)} -- review against the blocklist ({out_path})")
    return report
