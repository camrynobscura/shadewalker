"""In-memory routing graph, loaded once at server startup from data/tiles/.

Design (the memory-conscious layout from the plan):
- Per-edge NUMBERS live in numpy arrays — one tightly-packed array per
  attribute, indexed by edge position. This is what keeps the citywide
  graph in the hundreds-of-MB range instead of gigabytes (a Python list
  of dicts carries ~10× overhead per value).
- Per-edge geometry and names stay as plain Python lists (only touched for
  the handful of edges on a returned route, never in bulk math).
- igraph (a C graph library with Python bindings) holds the topology and
  runs Dijkstra; scipy's KDTree snaps clicked coordinates to graph nodes.

Costs are NOT precomputed: each request's month + tree_weight produce a
fresh cost array with two vectorized numpy lines — microseconds for the
whole graph — which keeps every slider value exact rather than quantized.
"""

import gzip
import json
import math

import igraph
import numpy as np
from scipy.spatial import cKDTree

from pipeline import config


def _dist2(p: list[float], q: np.ndarray) -> float:
    """Squared distance between a [lon, lat] point and a node's [lon, lat]
    array. Squared because we only ever compare two distances — skipping
    the square root doesn't change which one is smaller."""
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2


# A degree of latitude is ~111.32 km everywhere on Earth — used to convert
# the KDTree's query distance (in degrees-of-latitude units, since only
# longitude gets scaled) back into real meters.
METERS_PER_DEGREE_LAT = 111_320.0


class GraphStore:
    def __init__(self) -> None:
        # id ↔ index: igraph and numpy work in dense integer positions;
        # OSM node ids (strings) exist only at the boundary.
        self._id_to_idx: dict[str, int] = {}
        self._node_lonlat: np.ndarray | None = None  # (N, 2) float64
        self._kdtree: cKDTree | None = None
        self._lat_scale = 1.0  # see _build_kdtree
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
        self._build_kdtree()

        print(f"[graph_store] {len(tile_paths)} tile(s): "
              f"{len(node_lonlat)} nodes, {len(edge_pairs)} edges loaded")

    def _build_kdtree(self) -> None:
        """Index nodes for nearest-neighbor snapping.

        KDTree measures straight-line distance in the coordinates you give
        it — but a degree of longitude is shorter than a degree of latitude
        (by cos(latitude)), so raw lat/lon would warp "nearest" east-west.
        Scaling longitudes by cos(mean lat) makes degrees comparable; fine
        at city scale.
        """
        mean_lat = float(np.mean(self._node_lonlat[:, 1]))
        self._lat_scale = math.cos(math.radians(mean_lat))
        scaled = np.column_stack([
            self._node_lonlat[:, 0] * self._lat_scale,
            self._node_lonlat[:, 1],
        ])
        self._kdtree = cKDTree(scaled)

    # ── Routing ───────────────────────────────────────────────────────────────

    def snap(self, lat: float, lon: float) -> int:
        """Nearest graph node (as internal index) to a clicked point."""
        _, idx = self._kdtree.query([lon * self._lat_scale, lat])
        return int(idx)

    def coverage_bounds(self) -> tuple[float, float, float, float]:
        """(lon_min, lat_min, lon_max, lat_max) of the loaded graph data."""
        return self._bounds

    def in_coverage(self, lat: float, lon: float) -> bool:
        """Whether a point is somewhere we actually have routable data.

        Two checks, cheapest first: outside the loaded data's bounding box
        is an easy no. Inside the box isn't automatically a yes, though —
        a point in the middle of the Gowanus Canal is "inside" the pilot
        tile's bbox but nowhere near a real sidewalk, so the second check
        also requires a graph node within MAX_SNAP_DISTANCE_M.
        """
        lon_min, lat_min, lon_max, lat_max = self._bounds
        if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            return False
        dist_deg, _ = self._kdtree.query([lon * self._lat_scale, lat])
        return dist_deg * METERS_PER_DEGREE_LAT <= config.MAX_SNAP_DISTANCE_M

    def edge_costs(self, tree_weight: float, month: int) -> np.ndarray:
        """The plan's trees-only cost formula, vectorized over every edge."""
        canopy = config.CANOPY_BY_MONTH[month - 1]  # month is 1-12; lists index from 0
        tree_score = self._tree_evergreen + self._tree_deciduous * canopy
        density = tree_score / np.maximum(self._length, config.DENSITY_LENGTH_FLOOR_M)
        return self._length / (1.0 + tree_weight * density)

    def route(self, from_node: int, to_node: int, tree_weight: float, month: int) -> dict | None:
        """Cheapest path between two node indices. None if unreachable."""
        costs = self.edge_costs(tree_weight, month)

        # output="epath" → the path as a list of edge positions, which is
        # what we need to sum attributes and stitch geometry.
        edge_path = self._graph.get_shortest_paths(
            from_node, to=to_node, weights=costs, output="epath"
        )[0]

        if not edge_path and from_node != to_node:
            return None  # disconnected (shouldn't happen after retain_all=False)

        coords: list[list[float]] = []
        segments: list[dict] = []  # consecutive same-street runs, for text directions
        current = from_node
        for e in edge_path:
            u, v = self._graph.es[e].tuple
            next_node = v if u == current else u
            step = self._coords[e]

            # An edge's stored geometry doesn't always run u→v: osmnx's
            # to_undirected() collapses each one-way pair into a single
            # edge but keeps whichever original direction's geometry it
            # happened to retain, regardless of which node ended up
            # labeled u vs v. Trusting "u == current" to predict direction
            # was wrong for edges stored backwards — it flipped a
            # correctly-oriented line, drawing a there-and-back spike.
            # Checking which *end* of the raw geometry is actually closer
            # to where we're standing is correct regardless of storage
            # direction.
            here = self._node_lonlat[current]
            if _dist2(step[0], here) > _dist2(step[-1], here):
                step = step[::-1]  # [::-1] = reversed copy (JS: [...a].reverse())

            coords.extend(step if not coords else step[1:])  # skip duplicated joint
            current = next_node

            name = self._names[e] or "unnamed path"
            if segments and segments[-1]["name"] == name:
                segments[-1]["length_m"] += float(self._length[e])
            else:
                segments.append({"name": name, "length_m": float(self._length[e])})

        total_length = float(self._length[edge_path].sum())
        return {
            "coords": coords,
            "length_m": round(total_length, 1),
            "minutes": round(total_length / 1.4 / 60, 1),  # 1.4 m/s walking pace
            "tree_count": int(self._tree_count[edge_path].sum()),
            "segments": [
                {"name": s["name"], "length_m": round(s["length_m"], 1)} for s in segments
            ],
        }
