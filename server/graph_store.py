"""In-memory routing graph, loaded once at server startup from data/tiles/.

Design (the memory-conscious layout from the plan):
- Per-edge NUMBERS live in numpy arrays — one tightly-packed array per
  attribute, indexed by edge position. This is what keeps the citywide
  graph in the hundreds-of-MB range instead of gigabytes (a Python list
  of dicts carries ~10× overhead per value).
- Per-edge geometry and names stay as plain Python lists (only touched for
  the handful of edges on a returned route, never in bulk math).
- igraph (a C graph library with Python bindings) holds the topology and
  runs Dijkstra; a Shapely STRtree snaps clicked coordinates to the
  nearest point on the nearest STREET (not the nearest intersection —
  see snap_to_edge for why that distinction matters).

Costs are NOT precomputed: each request's month + tree_weight produce a
fresh cost array with two vectorized numpy lines — microseconds for the
whole graph — which keeps every slider value exact rather than quantized.
"""

import gzip
import json
import math
from dataclasses import dataclass

import igraph
import numpy as np
from shapely.geometry import LineString, Point
from shapely.ops import substring
from shapely.strtree import STRtree

from pipeline import config


def _dist2(p: list[float], q: np.ndarray) -> float:
    """Squared distance between a [lon, lat] point and a node's [lon, lat]
    array. Squared because we only ever compare two distances — skipping
    the square root doesn't change which one is smaller."""
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2


def _add_segment(segments: list[dict], name: str, length_m: float) -> None:
    """Appends one leg of turn-by-turn directions, merging into the previous
    leg if it's the same street. Skips legs that round to 0 m — a click
    landing almost exactly at a real intersection would otherwise add a
    spurious "0 m along X" leg."""
    if round(length_m, 1) <= 0:
        return
    if segments and segments[-1]["name"] == name:
        segments[-1]["length_m"] += length_m
    else:
        segments.append({"name": name, "length_m": length_m})


# A degree of latitude is ~111.32 km everywhere on Earth — used to convert
# STRtree query distances (in degrees-of-latitude units, since only
# longitude gets scaled) back into real meters.
METERS_PER_DEGREE_LAT = 111_320.0


@dataclass(frozen=True)
class SnapPoint:
    """Where a clicked/geocoded point resolves onto the street network: the
    closest position on the closest edge, plus the real-meters cost of
    reaching each of that edge's two real endpoints from there.

    Tree-weight independent by construction — snap_to_edge() takes no
    tree_weight, since finding the nearest street is pure geometry. Only
    which endpoint route() ends up connecting through can vary by
    tree_weight; that's a routing decision, not a geometric one.
    """

    edge: int
    point: list[float]  # [lon, lat] — the projected point on the edge
    node_u: int
    node_v: int
    dist_to_u_m: float
    dist_to_v_m: float


class GraphStore:
    def __init__(self) -> None:
        # id ↔ index: igraph and numpy work in dense integer positions;
        # OSM node ids (strings) exist only at the boundary.
        self._id_to_idx: dict[str, int] = {}
        self._node_lonlat: np.ndarray | None = None  # (N, 2) float64
        self._edge_lines_scaled: list[LineString] = []  # see _build_edge_index
        self._strtree: STRtree | None = None
        self._lat_scale = 1.0  # see _build_edge_index
        self._bounds: tuple[float, float, float, float] | None = None  # lon_min, lat_min, lon_max, lat_max

        # Edge attribute arrays, all aligned by edge position.
        self._length = np.empty(0, dtype=np.float32)
        self._tree_deciduous = np.empty(0, dtype=np.float32)
        self._tree_evergreen = np.empty(0, dtype=np.float32)
        self._tree_count = np.empty(0, dtype=np.int32)
        self._names: list[str] = []
        self._coords: list[list[list[float]]] = []  # per edge: [[lon,lat], ...]

        self._graph: igraph.Graph | None = None

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read every tile in data/tiles/ into one merged graph."""
        tile_paths = sorted(config.TILES_DIR.glob("*.json.gz"))
        if not tile_paths:
            raise FileNotFoundError(
                f"No tiles in {config.TILES_DIR} — run the pipeline first "
                "(uv run python -m pipeline.run_tile pilot)"
            )

        node_lonlat: list[list[float]] = []
        edge_pairs: list[tuple[int, int]] = []  # (u_idx, v_idx) for igraph
        length, deciduous, evergreen, counts = [], [], [], []
        seen_edges: set[tuple] = set()  # cross-tile dedupe on (u, v, key, side)

        for path in tile_paths:
            tile = json.loads(gzip.open(path, "rt").read())

            for node_id, (lon, lat) in tile["nodes"].items():
                if node_id not in self._id_to_idx:
                    self._id_to_idx[node_id] = len(node_lonlat)
                    node_lonlat.append([lon, lat])

            for edge in tile["edges"]:
                # Border edges appear in two neighboring tiles; a canonical
                # (sorted) node pair makes both copies hash identically.
                dedupe_key = (*sorted((edge["u"], edge["v"])), edge["key"], edge["side"])
                if dedupe_key in seen_edges:
                    continue
                seen_edges.add(dedupe_key)

                edge_pairs.append((self._id_to_idx[edge["u"]], self._id_to_idx[edge["v"]]))
                length.append(edge["length_m"])
                deciduous.append(edge["tree_deciduous"])
                evergreen.append(edge["tree_evergreen"])
                counts.append(edge["tree_count"])
                self._names.append(edge["name"])
                self._coords.append(edge["coords"])

        self._node_lonlat = np.array(node_lonlat)
        # The data's actual extent — whatever tiles happen to be loaded —
        # rather than a hardcoded bbox from pipeline/config.py, so this
        # stays correct without a server change once Stage 2 adds more tiles.
        lon_min, lat_min = self._node_lonlat.min(axis=0)
        lon_max, lat_max = self._node_lonlat.max(axis=0)
        self._bounds = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
        self._length = np.array(length, dtype=np.float32)
        self._tree_deciduous = np.array(deciduous, dtype=np.float32)
        self._tree_evergreen = np.array(evergreen, dtype=np.float32)
        self._tree_count = np.array(counts, dtype=np.int32)

        self._graph = igraph.Graph(n=len(node_lonlat), edges=edge_pairs, directed=False)
        self._build_edge_index()

        print(f"[graph_store] {len(tile_paths)} tile(s): "
              f"{len(node_lonlat)} nodes, {len(edge_pairs)} edges loaded")

    def _build_edge_index(self) -> None:
        """Index edges for nearest-street snapping (see snap_to_edge).

        One Shapely LineString per edge, in a cos(mean_lat)-scaled
        coordinate space — a degree of longitude is shorter than a degree
        of latitude away from the equator, so scaling longitude by
        cos(latitude) is what makes "nearest" geodesically meaningful
        rather than warped east-west. Same approximation the old node
        KDTree used; fine at city scale.

        This is a per-edge Python object, which this file's own header
        comment generally warns against for edge data — acceptable at
        pilot/city scale (a few thousand edges); citywide (Stage 2, ~1M
        edges) would be worth rebuilding with a batched construction
        instead (shapely.linestrings(coords, indices=...)).
        """
        mean_lat = float(np.mean(self._node_lonlat[:, 1]))
        self._lat_scale = math.cos(math.radians(mean_lat))
        self._edge_lines_scaled = [
            LineString([(lon * self._lat_scale, lat) for lon, lat in coords])
            for coords in self._coords
        ]
        self._strtree = STRtree(self._edge_lines_scaled)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _nearest_edge(self, lat: float, lon: float) -> tuple[int, float]:
        """Nearest edge to a point, and the real-meters distance to it."""
        point = Point(lon * self._lat_scale, lat)
        idx, dist_deg = self._strtree.query_nearest(point, return_distance=True)
        return int(idx[0]), float(dist_deg[0]) * METERS_PER_DEGREE_LAT

    def snap_to_edge(self, lat: float, lon: float) -> SnapPoint:
        """Where a clicked/geocoded point resolves onto the street network:
        the closest position on the closest edge.

        Replaces the old nearest-NODE snap, which could only ever land on
        an intersection — wrong whenever the real nearest thing is
        mid-block. A real bug traced to exactly this: a click 5-34m from a
        real named street was snapping 100+m away into a small plaza's
        dense internal path network instead, because that plaza has far
        more intersections-per-area than a normal block (nodes only every
        ~200-300m), so it won the nearest-NODE comparison on density
        alone, not on being the right answer.
        """
        edge, _ = self._nearest_edge(lat, lon)
        line = self._edge_lines_scaled[edge]
        point = Point(lon * self._lat_scale, lat)
        frac = line.project(point) / line.length if line.length > 0 else 0.0
        projected = line.interpolate(frac * line.length)
        snapped_point = [projected.x / self._lat_scale, projected.y]

        dist_from_geom_start_m = frac * self._length[edge]
        dist_from_geom_end_m = (1.0 - frac) * self._length[edge]

        # self._coords[e] doesn't always run u→v (see route()'s stitching
        # loop below for the full explanation — to_undirected() can store
        # an edge's geometry backwards relative to its (u,v) index). Decide
        # which real distance belongs to u vs v by checking which end of
        # the raw geometry u actually sits at, rather than re-projecting
        # node coordinates onto the line — that second approach breaks for
        # a self-loop edge where u == v, since it can't tell "the short way"
        # from "the long way" around the loop. Deriving both distances from
        # one projection fraction sidesteps that entirely.
        u, v = self._graph.es[edge].tuple
        geom_start = self._coords[edge][0]
        if _dist2(geom_start, self._node_lonlat[u]) <= _dist2(geom_start, self._node_lonlat[v]):
            dist_to_u_m, dist_to_v_m = dist_from_geom_start_m, dist_from_geom_end_m
        else:
            dist_to_u_m, dist_to_v_m = dist_from_geom_end_m, dist_from_geom_start_m

        return SnapPoint(
            edge=edge,
            point=snapped_point,
            node_u=u,
            node_v=v,
            dist_to_u_m=float(dist_to_u_m),
            dist_to_v_m=float(dist_to_v_m),
        )

    def _edge_substring(self, edge: int, point_a: list[float], point_b: list[float]) -> list[list[float]]:
        """The slice of an edge's geometry between two points that sit on
        it, ordered point_a -> point_b (substring() reverses on its own
        when that means walking the edge backwards). Used for the lead-in/
        lead-out slices in route() below, and for a same-edge direct hop."""
        line = self._edge_lines_scaled[edge]
        a = Point(point_a[0] * self._lat_scale, point_a[1])
        b = Point(point_b[0] * self._lat_scale, point_b[1])
        sub = substring(line, line.project(a), line.project(b))
        scaled_coords = [sub.coords[0]] if sub.geom_type == "Point" else list(sub.coords)
        return [[x / self._lat_scale, y] for x, y in scaled_coords]

    def coverage_bounds(self) -> tuple[float, float, float, float]:
        """(lon_min, lat_min, lon_max, lat_max) of the loaded graph data."""
        return self._bounds

    def in_coverage(self, lat: float, lon: float) -> bool:
        """Whether a point is somewhere we actually have routable data.

        Two checks, cheapest first: outside the loaded data's bounding box
        is an easy no. Inside the box isn't automatically a yes, though —
        a point in the middle of the Gowanus Canal is "inside" the pilot
        tile's bbox but nowhere near a real sidewalk, so the second check
        also requires a real edge within MAX_SNAP_DISTANCE_M. Nearest-EDGE
        distance is a strictly more permissive (and more accurate) signal
        than the old nearest-NODE distance — it can only be smaller, never
        larger, so this never newly rejects a point that used to pass.
        """
        lon_min, lat_min, lon_max, lat_max = self._bounds
        if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            return False
        _, dist_m = self._nearest_edge(lat, lon)
        return dist_m <= config.MAX_SNAP_DISTANCE_M

    def _edge_density(self, month: int) -> np.ndarray:
        """Month-adjusted tree density (score per meter), vectorized over
        every edge. Shared by edge_costs() (degree of density matters) and
        route()'s shade_fraction (a yes/no threshold on the same number)."""
        canopy = config.CANOPY_BY_MONTH[month - 1]  # month is 1-12; lists index from 0
        tree_score = self._tree_evergreen + self._tree_deciduous * canopy
        return tree_score / np.maximum(self._length, config.DENSITY_LENGTH_FLOOR_M)

    def edge_costs(self, tree_weight: float, month: int) -> np.ndarray:
        """The plan's trees-only cost formula, vectorized over every edge."""
        density = self._edge_density(month)
        return self._length / (1.0 + tree_weight * density)

    def route(self, start: SnapPoint, end: SnapPoint, tree_weight: float, month: int) -> dict | None:
        """Cheapest path between two snapped points. None if unreachable.

        A SnapPoint sits partway along an edge, not on a real graph node,
        so Dijkstra can't start there directly. Instead: try routing from
        each of the edge's two real endpoints (up to 2 start options x 2
        end options), add the cost of walking the partial edge to/from
        that endpoint, and keep whichever total is cheapest. This is a
        read-only evaluation — no graph mutation — because /route is a
        sync FastAPI handler that Starlette runs across a thread pool, and
        mutating the one shared igraph.Graph per request would need
        locking that serializes every routing request.

        When start and end land on the same edge, also try cutting
        straight between them along it — otherwise two nearby clicks on
        the same block would be forced through a corner and back for no
        reason. It's compared by cost like everything else, not assumed
        to win, since a leafy detour via a real corner can still cost less
        at a high tree_weight.
        """
        costs = self.edge_costs(tree_weight, month)
        shaded = self._edge_density(month) >= config.SHADE_DENSITY_THRESHOLD

        start_options = [(start.node_u, start.dist_to_u_m), (start.node_v, start.dist_to_v_m)]
        end_options = [(end.node_u, end.dist_to_u_m), (end.node_v, end.dist_to_v_m)]

        best_cost: float | None = None
        best_plan: tuple | None = None
        for s_node, s_dist_m in start_options:
            s_cost = s_dist_m / self._length[start.edge] * costs[start.edge]
            for e_node, e_dist_m in end_options:
                e_cost = e_dist_m / self._length[end.edge] * costs[end.edge]
                # output="epath" → the path as a list of edge positions,
                # which is what we need to sum attributes and stitch
                # geometry.
                edge_path = self._graph.get_shortest_paths(
                    s_node, to=e_node, weights=costs, output="epath"
                )[0]
                if not edge_path and s_node != e_node:
                    continue  # disconnected via this pair of endpoints
                total_cost = s_cost + float(costs[edge_path].sum()) + e_cost
                if best_cost is None or total_cost < best_cost:
                    best_cost = total_cost
                    best_plan = ("via_nodes", s_node, s_dist_m, e_node, e_dist_m, edge_path)

        if start.edge == end.edge:
            direct_dist_m = abs(start.dist_to_u_m - end.dist_to_u_m)
            direct_cost = direct_dist_m / self._length[start.edge] * costs[start.edge]
            if best_cost is None or direct_cost < best_cost:
                best_cost = direct_cost
                best_plan = ("direct", direct_dist_m)

        if best_plan is None:
            return None  # disconnected (shouldn't happen after retain_all=False)

        segments: list[dict] = []  # consecutive same-street runs, for text directions

        if best_plan[0] == "direct":
            _, direct_dist_m = best_plan
            coords = self._edge_substring(start.edge, start.point, end.point)
            length_m = direct_dist_m
            tree_count = direct_dist_m / self._length[start.edge] * self._tree_count[start.edge]
            # Unlike tree_count's proportional split, shade is all-or-
            # nothing per edge -- the edge either clears the threshold or
            # it doesn't, so a partial edge inherits its whole edge's status.
            shaded_length_m = direct_dist_m if shaded[start.edge] else 0.0
            _add_segment(segments, self._names[start.edge] or "unnamed path", length_m)
        else:
            _, s_node, s_dist_m, e_node, e_dist_m, edge_path = best_plan

            coords = self._edge_substring(start.edge, start.point, self._node_lonlat[s_node].tolist())
            _add_segment(segments, self._names[start.edge] or "unnamed path", s_dist_m)

            current = s_node
            for e in edge_path:
                u, v = self._graph.es[e].tuple
                next_node = v if u == current else u
                step = self._coords[e]

                # An edge's stored geometry doesn't always run u→v: osmnx's
                # to_undirected() collapses each one-way pair into a single
                # edge but keeps whichever original direction's geometry it
                # happened to retain, regardless of which node ended up
                # labeled u vs v. Trusting "u == current" to predict
                # direction was wrong for edges stored backwards — it
                # flipped a correctly-oriented line, drawing a
                # there-and-back spike. Checking which *end* of the raw
                # geometry is actually closer to where we're standing is
                # correct regardless of storage direction.
                here = self._node_lonlat[current]
                if _dist2(step[0], here) > _dist2(step[-1], here):
                    step = step[::-1]  # [::-1] = reversed copy (JS: [...a].reverse())

                coords.extend(step[1:])  # skip duplicated joint
                current = next_node
                _add_segment(segments, self._names[e] or "unnamed path", float(self._length[e]))

            lead_out = self._edge_substring(end.edge, self._node_lonlat[e_node].tolist(), end.point)
            coords.extend(lead_out[1:])
            _add_segment(segments, self._names[end.edge] or "unnamed path", e_dist_m)

            network_length_m = float(self._length[edge_path].sum())
            length_m = s_dist_m + network_length_m + e_dist_m
            # Partial edges get a proportional share of their tree_count —
            # there's no finer-than-per-edge tree data to split more
            # precisely than that.
            tree_count = (
                s_dist_m / self._length[start.edge] * self._tree_count[start.edge]
                + float(self._tree_count[edge_path].sum())
                + e_dist_m / self._length[end.edge] * self._tree_count[end.edge]
            )
            shaded_length_m = (
                (s_dist_m if shaded[start.edge] else 0.0)
                + float(self._length[edge_path][shaded[edge_path]].sum())
                + (e_dist_m if shaded[end.edge] else 0.0)
            )

        if len(coords) < 2:
            coords = coords * 2  # start and end snapped to the same point

        return {
            "coords": coords,
            "length_m": round(length_m, 1),
            "minutes": round(length_m / 1.4 / 60, 1),  # 1.4 m/s walking pace
            # Round the total once, not each partial piece, so rounding
            # error from the fractional lead-in/lead-out doesn't compound.
            "tree_count": int(round(tree_count)),
            "shade_fraction": round(shaded_length_m / length_m, 3) if length_m else 0.0,
            "segments": [
                {"name": s["name"], "length_m": round(s["length_m"], 1)} for s in segments
            ],
        }
