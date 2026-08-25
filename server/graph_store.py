"""In-memory routing graph, loaded once at server startup from data/tiles/.

Design (the memory-conscious layout from the plan):
- Per-edge NUMBERS live in numpy arrays — one tightly-packed array per
  attribute, indexed by edge position. This is what keeps the citywide
  graph in the hundreds-of-MB range instead of gigabytes (a Python list
  of dicts carries ~10× overhead per value).
- Per-edge geometry ("shapes") is packed the same way: every edge's
  [lon, lat] points concatenated into one flat (total_points, 2) buffer,
  with an offsets array saying where each edge's slice starts — nested
  Python lists cost ~6x more (each coordinate becomes a boxed float object
  behind a pointer), which is the difference between ~950MB and ~150MB of
  geometry at citywide scale. Names stay a plain Python list (small,
  non-numeric, only touched for the handful of edges on a returned route).
- igraph (a C graph library with Python bindings) holds the topology and
  runs Dijkstra; a Shapely STRtree snaps clicked coordinates to the
  nearest point on the nearest reachable STREET (not the nearest
  intersection, and not simply the nearest edge regardless of whether it
  goes anywhere — see snap_pair for why both distinctions matter).

Costs are NOT precomputed: each request's month + tree_weight produce a
fresh cost array with two vectorized numpy lines — microseconds for the
whole graph — which keeps every slider value exact rather than quantized.
"""

import logging
import gzip
import hashlib
import json
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

import igraph
import numpy as np
import shapely
from shapely.geometry import Point
from shapely.geometry.polygon import orient
from shapely.ops import substring
from shapely.strtree import STRtree

from pipeline import config
from server import coverage_frame

logger = logging.getLogger(__name__)


# igraph's C layer emits this RuntimeWarning from get_shortest_paths()
# whenever the two endpoints sit in different components. snap_pair()
# (below) now keeps /route from ever calling route() with such a pair in
# the first place -- two real, genuinely disconnected places (mainland
# <-> Governors Island, eventually Staten Island) get a clean "no route"
# from snap_pair() itself, cheaper than a Dijkstra call that walks the
# whole component before giving up. route() still refuses the same case
# on its own if ever called directly some other way, which is what would
# still trip this warning -- filtered once here, at module scope, rather
# than per-call: warnings.catch_warnings() mutates global filter state
# and isn't thread-safe, and /route runs across Starlette's thread pool.
warnings.filterwarnings("ignore", message="Couldn't reach some vertices", category=RuntimeWarning)


def _dist2(p: list[float] | np.ndarray, q: np.ndarray) -> float:
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


def clamp_shade_monotonic(routes: list[dict], weights: list[float]) -> list[dict]:
    """Enforce the Shade_priority promise across a batch of routes: as
    tree_weight increases, a route's shade_fraction must never DECREASE.
    Returns a new list aligned with `routes`/`weights`; any route that would
    break the guarantee is replaced by the shadiest lower-or-equal-weight
    route already in the batch.

    Why this is needed: route() minimizes a smooth density-weighted cost, but
    shade_fraction saturates that density at SHADE_SATURATION_DENSITY -- so
    the route chosen for a higher weight can genuinely report LESS shade
    than a lower weight's route (the router keeps rewarding density past the
    point where the stat stops crediting it). Far rarer since the stat went
    continuous (2026-08-17: 0/24 sampled pairs pre-clamp, vs 7/24 under the
    old shaded-or-not threshold whose measured rate was ~17% of citywide
    routes, up to a 0.22 drop), but the mechanism is still real, so the
    guarantee stays. /route computes every preset in one
    call, so this is pure post-processing: it only ever falls back to a real
    route the batch already produced, never one worse on shade than the
    preset's own route -- the walker strictly benefits, and length/time can
    only stay level or drop when it fires (the fallback route is shorter).

    Substitutes the WHOLE route dict (geometry + every stat together), never
    just the shade number, so nothing downstream can disagree. Order-robust:
    walks weights ascending regardless of the requested order and returns the
    result in the original positions. shade_fraction is already rounded to
    3dp upstream, so the epsilon only guards against float noise, not real
    differences.
    """
    epsilon = 1e-9
    clamped = list(routes)
    best: dict | None = None  # shadiest route seen so far, ascending weight
    for position in sorted(range(len(routes)), key=lambda i: weights[i]):
        route = routes[position]
        if best is not None and route["shade_fraction"] < best["shade_fraction"] - epsilon:
            clamped[position] = best
        else:
            best = route
    return clamped


# A degree of latitude is ~111.32 km everywhere on Earth — used to convert
# STRtree query distances (in degrees-of-latitude units, since only
# longitude gets scaled) back into real meters.
METERS_PER_DEGREE_LAT = 111_320.0

# Simplification tolerance for the drawn coverage boundary, in
# degrees-of-latitude units (~22m) — trims the served ring's vertex count.
# Small on purpose: the boundary is drawn at exactly MAX_SNAP_DISTANCE_M
# from the streets, so simplification is the only thing that can make the
# drawn line disagree with the acceptance rule, and this bounds that
# disagreement to a sliver nobody can click precisely enough to notice.
COVERAGE_SIMPLIFY_DEG = 0.0002

# Caches _compute_coverage_rings()'s output across server restarts --
# measured at ~12s of a ~15s cold start at Brooklyn+Manhattan scale (a
# shapely union_all over every edge's buffered geometry, which grows with
# the graph), for output that only changes when the tiles themselves do.
# Named with a leading dot so it reads as a derived artifact, not a tile;
# living inside TILES_DIR (rather than a fixed path elsewhere) is
# deliberate -- the cache automatically follows TILES_DIR wherever it
# points, tests included, rather than every test that monkeypatches
# TILES_DIR to a tmp_path silently reading/writing the real repo's cache
# file instead of its own sandboxed one.
COVERAGE_CACHE_FILENAME = ".coverage_cache.json"

# A version tag for load()'s MERGE SEMANTICS, folded into the coverage
# fingerprint (see _tiles_fingerprint). load() can change which components
# exist without any tile's bytes changing, so a cached coverage from before
# such a change must be invalidated even when every tile file is unchanged.
# Bump when load()'s edge topology can change for identical tiles.
LOAD_PARAMS = "load-v23|no-hide-rule"


def _tiles_fingerprint(tile_paths: list) -> str:
    """A cheap fingerprint of every loaded tile's identity (name, size,
    mtime) — changes whenever a tile is added, removed, or re-exported,
    which is exactly when the coverage cache (above) needs recomputing
    rather than reused.

    Two recipe tags ride along, because the drawn coverage depends on more
    than the tiles' bytes: the offshore frame's parameters
    (server/coverage_frame.py) and LOAD_PARAMS, which covers any change to
    WHICH components end up visible. Deleting the hide rule was exactly
    that kind of change -- without a LOAD_PARAMS bump it would have served
    rings computed under the old rule from every existing cache."""
    parts = sorted(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in tile_paths)
    rule = coverage_frame.FRAME_PARAMS + "|" + LOAD_PARAMS
    return hashlib.sha256(("\n".join(parts) + "\n" + rule).encode()).hexdigest()


def _load_cached_coverage(cache_path, fingerprint: str) -> dict | None:
    """The on-disk coverage cache ({"rings": ..., "frame": ...}), if its
    fingerprint matches the tiles being loaded right now — None on any
    mismatch, missing file, corrupt cache, or pre-frame cache format, all
    treated the same way (recompute), since this is strictly a speed
    optimization with no correctness dependency on it."""
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("fingerprint") != fingerprint:
        return None
    if "rings" not in cached or "frame" not in cached:
        return None
    return cached


def _save_cached_coverage(cache_path, fingerprint: str, rings, frame: dict) -> None:
    cache_path.write_text(json.dumps({"fingerprint": fingerprint, "rings": rings, "frame": frame}))


def _local_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance between two nearby points -- fine at the
    few-meters-to-tens-of-meters scale it is used at (same approximation
    pipeline/config.py's buffered_bbox() already uses for short local
    distances, just inverted)."""
    mean_lat = (lat1 + lat2) / 2
    dlat_m = (lat2 - lat1) * METERS_PER_DEGREE_LAT
    dlon_m = (lon2 - lon1) * METERS_PER_DEGREE_LAT * math.cos(math.radians(mean_lat))
    return math.hypot(dlat_m, dlon_m)


@dataclass(frozen=True)
class SnapPoint:
    """Where a clicked/geocoded point resolves onto the street network: the
    closest position on the closest edge, plus the real-meters cost of
    reaching each of that edge's two real endpoints from there.

    Tree-weight independent by construction — snap_pair() takes no
    tree_weight, since finding the nearest *reachable* street is pure
    geometry plus connectivity, neither of which varies by tree_weight.
    Only which endpoint route() ends up connecting through can vary by
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
        self._coverage_rings: list[list[list[float]]] = []  # closed [lon, lat] rings, CCW, one per piece
        # The offshore frame + feather rings (server/coverage_frame.py),
        # computed from the rings above at load, cached alongside them.
        self._coverage_frame: dict = {}
        self._edge_lines_scaled: np.ndarray | None = None  # see _build_edge_index
        self._strtree: STRtree | None = None
        self._lat_scale = 1.0  # see _build_edge_index
        self._bounds: tuple[float, float, float, float] | None = None  # lon_min, lat_min, lon_max, lat_max

        # Edge attribute arrays, all aligned by edge position.
        self._length = np.empty(0, dtype=np.float32)
        # Which connected component each edge belongs to -- see
        # snap_pair() for why this is tracked at all: every component is
        # kept and every one is snappable, so requiring a component
        # REACHABLE FROM BOTH endpoints is the only thing standing between
        # a click and a disconnected fragment near it.
        self._edge_component = np.empty(0, dtype=np.int32)
        self._tree_deciduous = np.empty(0, dtype=np.float32)
        self._tree_evergreen = np.empty(0, dtype=np.float32)
        # float32, not int32: block-face scoring gives an edge a fractional
        # SHARE of its block's trees (a face with 3 trees over 10 edges =
        # 0.3 each), and an integer dtype would round every one of those to
        # zero on load. route() still reports a whole number -- it sums the
        # shares along the path and rounds once at the end.
        self._tree_count = np.empty(0, dtype=np.float32)
        # The slice of _tree_deciduous that is park-canopy credit rather
        # than countable trees (FIXES item 4) -- already inside
        # _tree_deciduous, so it's a share of the score, never an addition.
        self._tree_park_canopy = np.empty(0, dtype=np.float32)
        self._names: list[str] = []
        # Edge shapes, packed: all edges' [lon, lat] points concatenated
        # into one flat block. float64, not float32 — at NYC longitudes
        # float32's resolution is ~0.5m, too coarse for snapping/drawing.
        # Edge e's points are _coord_buf[offsets[e]:offsets[e+1]]; use
        # _edge_coords(e) rather than slicing by hand.
        self._coord_buf = np.empty((0, 2), dtype=np.float64)
        self._coord_offsets = np.zeros(1, dtype=np.int64)

        self._graph: igraph.Graph | None = None

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read every tile in data/tiles/ into one merged graph."""
        tile_paths = sorted(config.TILES_DIR.glob("*.json.gz"))
        if not tile_paths:
            raise FileNotFoundError(
                f"No graph data in {config.TILES_DIR}. Build it with "
                "`uv run python -m pipeline.build`, or point "
                "SHADEWALKER_TILES_DIR at a directory holding a built "
                "export to run against that instead."
            )
        coverage_fingerprint = _tiles_fingerprint(tile_paths)
        coverage_cache_path = config.TILES_DIR / COVERAGE_CACHE_FILENAME

        node_lonlat: list[list[float]] = []
        edge_pairs: list[tuple[int, int]] = []  # (u_idx, v_idx) for igraph
        length, deciduous, evergreen, counts = [], [], [], []
        # .get()-defaulted on read: tiles exported before v19 (the committed
        # pilot test fixture) predate the field entirely, and 0.0 is exactly
        # what they mean -- no canopy credit was computed for them.
        canopy_credit: list[float] = []
        names: list[str] = []
        coords_per_edge: list[list[list[float]]] = []  # packed into _coord_buf after the loop
        seen_edges: dict[tuple, int] = {}  # (u, v, key, side) -> position in the lists above
        seen_geometries: set[tuple] = set()  # (u, v, side, geometry hash) -- see below
        # Pass 1: every node from every tile, so the complete node universe
        # is known before any edge is ingested. (Tiles are re-read in pass 2
        # rather than held in memory -- one tile at a time keeps peak RAM
        # flat across a citywide load.)
        for path in tile_paths:
            tile = json.loads(gzip.open(path, "rt").read())
            for node_id, (lon, lat) in tile["nodes"].items():
                if node_id not in self._id_to_idx:
                    self._id_to_idx[node_id] = len(node_lonlat)
                    node_lonlat.append([lon, lat])

        def _emit(u_id, v_id, key, side, seg_length_m, decid, everg,
                  cnt, canopy, name, coords):
            """Add one edge (a whole edge, or one piece of a split one) to
            the graph arrays, through the existing border-dedupe. Splitting
            reduces the severed-overlap class to the already-solved
            duplicate-border-edge class: after the split, two tiles'
            overlapping copies have identical endpoints and identical coords,
            so this same dedupe collapses them."""
            # Border edges appear in two neighboring tiles; a canonical
            # (sorted) node pair makes both copies hash identically. Each
            # tile scored its copy against only its own tree fetch, so the
            # copies can disagree -- when they do, keep the better-scored
            # one, not the first-seen one. Both copies count trees in the
            # identical corridor, so a copy can only be MISSING trees its
            # tile's fetch didn't cover, never have extras: higher tree
            # value == closer to complete. (First-seen-wins silently kept
            # the worse copy 3,740 times citywide, including a 1.7km Harlem
            # River Drive Greenway edge held at 0 trees while its other copy
            # had 95.)
            dedupe_key = (*sorted((u_id, v_id)), key, side)
            existing = seen_edges.get(dedupe_key)
            if existing is not None:
                stored_value = deciduous[existing] + evergreen[existing]
                if decid + everg > stored_value:
                    length[existing] = seg_length_m
                    deciduous[existing] = decid
                    evergreen[existing] = everg
                    counts[existing] = cnt
                    canopy_credit[existing] = canopy
                    names[existing] = name
                    coords_per_edge[existing] = coords
                return

            # OSM itself sometimes contains the same way twice -- identical
            # geometry between the same two nodes, which osmnx keeps as
            # parallel edges under different multigraph keys (159 confirmed
            # citywide, all within a single tile). Keep one: same endpoints,
            # so dropping the extra copy can't disconnect anything. Hashing
            # the coords (direction-insensitive) instead of storing them
            # keeps this set small; genuinely different parallel edges
            # between the same nodes (a street and a separate path) hash
            # differently and both survive.
            forward = tuple(tuple(point) for point in coords)
            geometry_key = (*dedupe_key[:2], side,
                            min(hash(forward), hash(forward[::-1])))
            if geometry_key in seen_geometries:
                return
            seen_geometries.add(geometry_key)

            seen_edges[dedupe_key] = len(edge_pairs)
            edge_pairs.append((self._id_to_idx[u_id], self._id_to_idx[v_id]))
            length.append(seg_length_m)
            deciduous.append(decid)
            evergreen.append(everg)
            counts.append(cnt)
            canopy_credit.append(canopy)
            names.append(name)
            coords_per_edge.append(coords)

        # Pass 2: edges, each emitted through the dedupe above.
        for path in tile_paths:
            tile = json.loads(gzip.open(path, "rt").read())
            for edge in tile["edges"]:
                _emit(edge["u"], edge["v"], edge["key"], edge["side"],
                      edge["length_m"], edge["tree_deciduous"],
                      edge["tree_evergreen"], edge["tree_count"],
                      edge.get("tree_park_canopy", 0.0), edge["name"],
                      edge["coords"])

        self._names = names
        self._node_lonlat = np.array(node_lonlat)
        # The data's actual extent — whatever tiles happen to be loaded —
        # rather than a hardcoded bbox from pipeline/config.py, so this
        # stays correct without a server change once Stage 2 adds more tiles.
        lon_min, lat_min = self._node_lonlat.min(axis=0)
        lon_max, lat_max = self._node_lonlat.max(axis=0)
        self._bounds = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
        # Floored at 0.01m, in ONE place for every edge: 960 real exported
        # edges have length_m 0.0 -- a sub-5cm edge rounds to 0.0 at export
        # (pipeline/export.py rounds to 0.1m) -- and a snap landing on a
        # zero-length edge turns route()'s partial-edge division into
        # 0/0 -> NaN -> a crash at int(round(tree_count)). Found live on a
        # Central Park test route. 1cm on a <5cm edge distorts nothing.
        self._length = np.maximum(np.array(length, dtype=np.float32), 0.01)
        self._tree_deciduous = np.array(deciduous, dtype=np.float32)
        self._tree_evergreen = np.array(evergreen, dtype=np.float32)
        self._tree_count = np.array(counts, dtype=np.float32)
        self._tree_park_canopy = np.array(canopy_credit, dtype=np.float32)

        # Pack the edge shapes: one flat buffer + an offsets array (see
        # __init__). cumsum turns per-edge point counts into slice
        # boundaries — offsets[e] is where edge e's points start.
        point_counts = [len(edge_coords) for edge_coords in coords_per_edge]
        self._coord_offsets = np.concatenate(([0], np.cumsum(point_counts))).astype(np.int64)
        self._coord_buf = np.concatenate(
            [np.asarray(edge_coords, dtype=np.float64) for edge_coords in coords_per_edge]
        )

        self._graph = igraph.Graph(n=len(node_lonlat), edges=edge_pairs, directed=False)

        # No filtering here anymore -- every component is kept. This used to
        # drop everything but the largest connected component, because
        # rectangular borough bboxes deliberately overreached past the real
        # coastline (see the old BROOKLYN_BBOX), sweeping in street
        # fragments from across the water (Jersey City, a Lower Manhattan
        # sliver, the Rockaways) with no real connection to the rest of the
        # data. That's no longer possible: pipeline/graph/boundary.py's
        # clip_to_nyc() now drops non-NYC territory at fetch time, before it
        # ever reaches data/tiles/, so every component here is trusted as
        # real NYC data -- including genuinely disconnected real places
        # (Governors Island, ferry-only; eventually Staten Island, whose
        # only bridges lead to NJ, not the rest of NYC) alongside plenty of
        # genuinely disconnected junk (an orphaned pedestrian crossing, a
        # plaza's interior path network -- see snap_pair() for why keeping
        # these doesn't mean routing ever resolves onto one by mistake).
        # snap_pair() below is what turns "two points that legitimately
        # can't connect" into a clean 422, so a multi-component graph is a
        # normal, supported state now, not an error condition -- see
        # PLAN.md's borough-boundary polygon plan.
        components = self._graph.connected_components(mode="weak")
        membership = np.asarray(components.membership, dtype=np.int32)
        self._edge_component = membership[[u for u, _ in edge_pairs]]
        if len(components) > 1:
            sizes = sorted((len(component) for component in components), reverse=True)
            logger.info(f"[graph_store] {len(components)} disconnected components "
                  f"(sizes, largest 5: {sizes[:5]})")

        self._build_edge_index()

        cached = _load_cached_coverage(coverage_cache_path, coverage_fingerprint)
        if cached is not None:
            self._coverage_rings = cached["rings"]
            self._coverage_frame = cached["frame"]
        else:
            self._coverage_rings = self._compute_coverage_rings()
            self._coverage_frame = coverage_frame.build_frame(self._coverage_rings)
            _save_cached_coverage(coverage_cache_path, coverage_fingerprint,
                                  self._coverage_rings, self._coverage_frame)

        logger.info(f"[graph_store] {len(tile_paths)} tile(s): "
              f"{len(node_lonlat)} nodes, {len(edge_pairs)} edges loaded")

    def _compute_coverage_rings(self) -> list[list[list[float]]]:
        """The drawn coverage boundary: the union of every street edge
        buffered by MAX_SNAP_DISTANCE_M — i.e. exactly the region the
        server accepts clicks in ("within 200m of a loaded street"), so
        the dashed line(s) on the map are the acceptance rule made visible.

        Chosen over a concave hull of the nodes after the hull clipped
        Red Hook: any global "how far in should the outline carve" knob
        shaves peninsulas, whereas a per-street footprint cannot exclude
        a routable place by construction. Built in the same scaled space
        the STRtree uses, so "200m" here is the same 200m the snap check
        measures. Measured at ~12s at Brooklyn+Manhattan scale, growing
        with the graph — load() only pays this on the first boot after a
        real data change, caching the result otherwise (see load()'s use
        of COVERAGE_CACHE_FILENAME).

        Returns one ring per disjoint piece of the footprint — plural, not
        a single ring picking "the biggest piece": load() now keeps every
        real component (Governors Island, eventually Staten Island), and
        each one deserves its own visible boundary rather than being
        silently dropped from the map while still being fully routable.
        /coverage serves these as a GeoJSON MultiPolygon.

        Two accepted approximations, both slivers: simplify() can move a
        ring up to ~22m either way (see COVERAGE_SIMPLIFY_DEG), and
        interior holes in the footprint (a cemetery's unwalkable core) are
        dropped — each piece is drawn as a single ring, and a click in
        such a pocket still gets the honest out-of-coverage rejection."""
        radius_deg = config.MAX_SNAP_DISTANCE_M / METERS_PER_DEGREE_LAT
        # Every edge, because every edge is snappable: the drawn boundary
        # IS the acceptance region, so advertising less than we accept
        # would tell someone their own street is outside our coverage.
        footprint = shapely.union_all(
            shapely.buffer(self._edge_lines_scaled, radius_deg, quad_segs=2)
        )
        footprint = footprint.simplify(COVERAGE_SIMPLIFY_DEG)
        pieces = list(footprint.geoms) if footprint.geom_type == "MultiPolygon" else [footprint]
        # The frontend punches its map-dimming holes by reversing these
        # rings, which assumes counterclockwise winding (the old
        # rectangle's order) — orient() guarantees it regardless of what
        # union_all produced.
        return [
            [[round(lon / self._lat_scale, 6), round(lat, 6)] for lon, lat in orient(piece).exterior.coords]
            for piece in pieces
        ]

    def coverage_rings(self) -> list[list[list[float]]]:
        """One closed [lon, lat] ring per disjoint coverage piece — see
        _compute_coverage_rings for shape and winding guarantees."""
        return self._coverage_rings

    def coverage_frame(self) -> dict:
        """The offshore frame + feather rings the frontend draws instead
        of tracing the rings above — {"frame": rings, "feather_350":
        rings, "feather_800": rings}, lon/lat (server/coverage_frame.py).
        The rings above stay the ACCEPTANCE region (in_coverage); the
        frame is the generous visual boundary drawn through the water."""
        return self._coverage_frame

    def _edge_coords(self, edge: int) -> np.ndarray:
        """Edge `edge`'s [lon, lat] points — a zero-copy view into the
        packed coordinate buffer. A row indexes like a little [lon, lat]
        list, so callers can treat it exactly like the old nested lists."""
        return self._coord_buf[self._coord_offsets[edge]:self._coord_offsets[edge + 1]]

    def _build_edge_index(self) -> None:
        """Index edges for nearest-street snapping (see snap_pair).

        One Shapely LineString per edge, in a cos(mean_lat)-scaled
        coordinate space — a degree of longitude is shorter than a degree
        of latitude away from the equator, so scaling longitude by
        cos(latitude) is what makes "nearest" geodesically meaningful
        rather than warped east-west. Same approximation the old node
        KDTree used; fine at city scale.

        Built with one batched shapely.linestrings() call over the packed
        coordinate buffer — `indices` maps each coordinate row to the edge
        it belongs to, so all LineStrings materialize in a single C-level
        pass instead of a per-edge Python loop. That keeps this cheap at
        citywide scale (Stage 2, ~1M edges), not just pilot scale.
        """
        mean_lat = float(np.mean(self._node_lonlat[:, 1]))
        self._lat_scale = math.cos(math.radians(mean_lat))
        scaled = self._coord_buf * np.array([self._lat_scale, 1.0])
        point_counts = np.diff(self._coord_offsets)
        edge_of_each_point = np.repeat(np.arange(len(point_counts)), point_counts)
        self._edge_lines_scaled = shapely.linestrings(scaled, indices=edge_of_each_point)
        self._strtree = STRtree(self._edge_lines_scaled)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _nearest_edge(self, lat: float, lon: float) -> tuple[int, float]:
        """Nearest edge to a point, and the real-meters distance to it.

        The snap tree holds every edge, so a tree position IS an edge id.
        It briefly held only "visible" ones under the hide rule, which
        needed a position -> edge id mapping alongside it."""
        point = Point(lon * self._lat_scale, lat)
        idx, dist_deg = self._strtree.query_nearest(point, return_distance=True)
        return int(idx[0]), float(dist_deg[0]) * METERS_PER_DEGREE_LAT

    def _nearby_components(self, lat: float, lon: float) -> dict[int, tuple[int, float]]:
        """Every distinct connected component with an edge within
        MAX_SNAP_DISTANCE_M of (lat, lon) — the same radius /coverage and
        in_coverage() already treat as "close enough to be on the map,"
        not a new tunable — mapped to that component's own nearest edge
        and the real-meters distance to it.

        A single click can have many components in range at once: a dense
        plaza can put dozens of small disconnected path fragments within
        200m of a real corner (measured up to 142 near a dense Manhattan
        intersection). Returning all of them, rather than picking one
        "nearest" overall, is what lets snap_pair() tell a genuinely
        reachable street apart from a closer dead end.
        """
        point = Point(lon * self._lat_scale, lat)
        radius_deg = config.MAX_SNAP_DISTANCE_M / METERS_PER_DEGREE_LAT
        candidates = self._strtree.query(point, predicate="dwithin", distance=radius_deg)
        nearest_per_component: dict[int, tuple[int, float]] = {}
        for edge in candidates:
            edge = int(edge)
            component = int(self._edge_component[edge])
            dist_m = self._edge_lines_scaled[edge].distance(point) * METERS_PER_DEGREE_LAT
            if component not in nearest_per_component or dist_m < nearest_per_component[component][1]:
                nearest_per_component[component] = (edge, dist_m)
        return nearest_per_component

    def snap_pair(
        self, from_lat: float, from_lon: float, to_lat: float, to_lon: float
    ) -> tuple["SnapPoint", "SnapPoint"] | None:
        """Snap a route request's two endpoints onto edges that can
        actually reach each other.

        Replaces snapping each point independently to its single nearest
        edge, which ignored reachability entirely — a real bug: a click
        at a real, named street corner (Union Square, a Brooklyn Bridge
        landing) sometimes snapped onto a tiny disconnected fragment
        instead (an orphaned pedestrian crossing, a plaza's interior path
        network — see PLAN.md), because that fragment happened to sit a
        few meters closer than the real, reachable street.

        Considering every component within MAX_SNAP_DISTANCE_M of each
        point and requiring one shared by both — rather than trying each
        candidate with a real Dijkstra call — is what keeps this cheap
        even where a click has dozens of components in range: it's a set
        intersection over already-known component membership, not a
        search. (An earlier design that tried routing through every
        candidate pair was measured at ~48s for a single dense request —
        this scales with "how many components are nearby," which this
        design doesn't.)

        None means no component reaches both points within
        MAX_SNAP_DISTANCE_M — the same real "no route" case as mainland
        <-> Governors Island, just recognized here instead of by a wasted
        Dijkstra call that walks the whole component before giving up.
        """
        start_options = self._nearby_components(from_lat, from_lon)
        end_options = self._nearby_components(to_lat, to_lon)
        shared = start_options.keys() & end_options.keys()
        if not shared:
            return None

        # Prefer whichever shared component sits closest to both points
        # combined -- there's usually exactly one shared component (the
        # main street grid), but a point near two real bridge landings
        # could plausibly have more than one legitimate option.
        best_component = min(shared, key=lambda c: start_options[c][1] + end_options[c][1])
        start_edge, _ = start_options[best_component]
        end_edge, _ = end_options[best_component]
        return (
            self._snap_point_for_edge(from_lat, from_lon, start_edge),
            self._snap_point_for_edge(to_lat, to_lon, end_edge),
        )

    def _snap_point_for_edge(self, lat: float, lon: float, edge: int) -> SnapPoint:
        """Where (lat, lon) projects onto a specific edge: the closest
        position on it, plus the real-meters cost of reaching each of its
        two real endpoints from there.

        Split out from snap_pair (the only caller) so *which* edge to
        snap onto (a reachability decision) and *where* on that edge (pure
        geometry) are separate steps — this half replaces the old
        nearest-NODE snap, which could only ever land on an intersection —
        wrong whenever the real nearest thing is mid-block.
        """
        line = self._edge_lines_scaled[edge]
        point = Point(lon * self._lat_scale, lat)
        frac = line.project(point) / line.length if line.length > 0 else 0.0
        projected = line.interpolate(frac * line.length)
        snapped_point = [projected.x / self._lat_scale, projected.y]

        dist_from_geom_start_m = frac * self._length[edge]
        dist_from_geom_end_m = (1.0 - frac) * self._length[edge]

        # An edge's geometry doesn't always run u→v (see route()'s stitching
        # loop below for the full explanation — to_undirected() can store
        # an edge's geometry backwards relative to its (u,v) index). Decide
        # which real distance belongs to u vs v by checking which end of
        # the raw geometry u actually sits at, rather than re-projecting
        # node coordinates onto the line — that second approach breaks for
        # a self-loop edge where u == v, since it can't tell "the short way"
        # from "the long way" around the loop. Deriving both distances from
        # one projection fraction sidesteps that entirely.
        u, v = self._graph.es[edge].tuple
        geom_start = self._edge_coords(edge)[0]
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

        The bbox is padded by MAX_SNAP_DISTANCE_M to match the drawn
        boundary: each coverage ring extends that far past its outermost
        street (they ARE the acceptance region drawn — see
        _compute_coverage_rings), so a raw node-min/max box would wrongly
        reject clicks just past the outermost street that a drawn ring
        includes and the snap check would accept.
        """
        pad_lat = config.MAX_SNAP_DISTANCE_M / METERS_PER_DEGREE_LAT
        pad_lon = pad_lat / self._lat_scale
        lon_min, lat_min, lon_max, lat_max = self._bounds
        if not (lon_min - pad_lon <= lon <= lon_max + pad_lon
                and lat_min - pad_lat <= lat <= lat_max + pad_lat):
            return False
        _, dist_m = self._nearest_edge(lat, lon)
        return dist_m <= config.MAX_SNAP_DISTANCE_M

    def _edge_density(self, month: int) -> np.ndarray:
        """Month-adjusted tree density (score per meter), vectorized over
        every edge. Shared by edge_costs() (unsaturated -- degree of
        density always matters to the router) and route()'s
        shade_fraction (the same number, saturated at
        SHADE_SATURATION_DENSITY for reporting).

        NO LENGTH FLOOR. There was one (DENSITY_LENGTH_FLOOR_M = 20.0) and it
        is deleted, not unset -- see its epitaph in pipeline/config.py. It
        patched a centerline-era symptom, short edges inheriting a cross
        street's trees through a buffer corridor. Block-face scoring removes
        the cause: an edge holds a share of its block's trees proportional to
        its own length, so it cannot out-read its own block. Measured on the
        citywide export 2026-08-24, short edges are LESS dense than long ones
        (0-5m median 0.0093 against 100m+ 0.0110), so a floor would only
        deflate correct values -- 7.6x on a 2.6m edge, and half of all
        sidewalk edges are under 5m.

        The fail-closed guard that lived here (all-zero while the floor was
        None) is gone with it: both constants now have measured, sidewalk-era
        values, which is the condition its own comment set for removal."""
        canopy = config.CANOPY_BY_MONTH[month - 1]  # month is 1-12; lists index from 0
        tree_score = self._tree_evergreen + self._tree_deciduous * canopy
        return tree_score / self._length

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

        The 2x2 combinations cost only 2 real Dijkstra runs, not 4:
        get_shortest_paths(v, to=[...]) finds the cheapest path from one
        source to every listed target in a single run (that's inherent to
        how Dijkstra works, not a batching trick), so each start endpoint
        covers both end endpoints at once. Worth it at citywide scale —
        each run's fixed cost grows with the graph, measured around 13ms
        on a 203k-node component (a straight-line distance thing, not a
        constant), so halving the run count matters more here than it did
        at pilot-tile scale.

        When start and end land on the same edge, also try cutting
        straight between them along it — otherwise two nearby clicks on
        the same block would be forced through a corner and back for no
        reason. It's compared by cost like everything else, not assumed
        to win, since a leafy detour via a real corner can still cost less
        at a high tree_weight.
        """
        costs = self.edge_costs(tree_weight, month)
        # Continuous per-edge shade credit (FIXES item 2): an edge
        # contributes min(density / SHADE_SATURATION_DENSITY, 1) of its
        # length to shade_fraction, replacing the old shaded-or-not
        # threshold whose cliff-edge let near-identical routes read 0%
        # vs 100% -- see the constant's comment for the calibration.
        #
        # The fail-closed branch here (all-zero while the constant was None)
        # was removed on 2026-08-24 when the constant got a measured
        # sidewalk-era value of 0.02. Saturation caps only what is REPORTED:
        # edge_costs() above uses the unsaturated density, so the router
        # still separates two blocks that both display as fully shaded.
        shade_credit = np.minimum(
            self._edge_density(month) / config.SHADE_SATURATION_DENSITY, 1.0
        )

        start_options = [(start.node_u, start.dist_to_u_m), (start.node_v, start.dist_to_v_m)]
        end_options = [(end.node_u, end.dist_to_u_m), (end.node_v, end.dist_to_v_m)]
        end_nodes = [e_node for e_node, _ in end_options]

        best_cost: float | None = None
        best_plan: tuple | None = None
        for s_node, s_dist_m in start_options:
            s_cost = s_dist_m / self._length[start.edge] * costs[start.edge]
            # output="epath" → the path as a list of edge positions, which
            # is what we need to sum attributes and stitch geometry. One
            # call covers both end_nodes (see this function's docstring).
            # igraph's C layer warns here ("Couldn't reach some vertices")
            # whenever s_node and a given e_node sit in different
            # components -- snap_pair() keeps the real /route flow from
            # ever reaching this with such a pair, so in practice this is
            # now only a defense-in-depth path (see the module-scope
            # filter comment above for why it's silenced there rather
            # than with a per-call warnings.catch_warnings(), which isn't
            # thread-safe and /route runs across Starlette's thread pool).
            edge_paths = self._graph.get_shortest_paths(
                s_node, to=end_nodes, weights=costs, output="epath"
            )
            for (e_node, e_dist_m), edge_path in zip(end_options, edge_paths):
                e_cost = e_dist_m / self._length[end.edge] * costs[end.edge]
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
            # A legitimate outcome now, not a bug: load() keeps every
            # component (see its own comment), so two points in genuinely
            # disconnected parts of NYC -- mainland and Governors Island,
            # eventually mainland and Staten Island -- hit this and get a
            # clean "no route" here rather than an error.
            return None

        segments: list[dict] = []  # consecutive same-street runs, for text directions

        if best_plan[0] == "direct":
            _, direct_dist_m = best_plan
            coords = self._edge_substring(start.edge, start.point, end.point)
            length_m = direct_dist_m
            tree_count = direct_dist_m / self._length[start.edge] * self._tree_count[start.edge]
            # Density is uniform along an edge, so a partial edge earns
            # its whole edge's per-meter credit over just the walked part.
            shaded_length_m = direct_dist_m * float(shade_credit[start.edge])
            canopy_score = float(self._tree_park_canopy[start.edge])
            walked_tree_score = float(
                self._tree_deciduous[start.edge] + self._tree_evergreen[start.edge]
            )
            _add_segment(segments, self._names[start.edge] or "unnamed path", length_m)
        else:
            _, s_node, s_dist_m, e_node, e_dist_m, edge_path = best_plan

            coords = self._edge_substring(start.edge, start.point, self._node_lonlat[s_node].tolist())
            _add_segment(segments, self._names[start.edge] or "unnamed path", s_dist_m)

            current = s_node
            for e in edge_path:
                u, v = self._graph.es[e].tuple
                next_node = v if u == current else u
                step = self._edge_coords(e)

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
                    step = step[::-1]  # [::-1] = reversed view (JS: [...a].reverse())

                # tolist() → plain [lon, lat] lists; numpy rows aren't
                # JSON-serializable and coords feeds the response directly.
                coords.extend(step[1:].tolist())  # skip duplicated joint
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
            # Same proportional-credit treatment as tree_count above: the
            # partial lead-in/lead-out edges earn their own edge's
            # per-meter credit over just the walked distance. (The old
            # binary definition also subtracted a per-intersection
            # exposure gap here, SHADE_CROSSING_GAP_M -- dropped with the
            # continuous redesign, see SHADE_SATURATION_DENSITY's comment.)
            shaded_length_m = (
                s_dist_m * float(shade_credit[start.edge])
                + float((self._length[edge_path] * shade_credit[edge_path]).sum())
                + e_dist_m * float(shade_credit[end.edge])
            )
            # How much of the walked tree score is park-canopy credit vs
            # countable trees (FIXES item 4) -- the frontend hides the
            # raw "trees: N" stat when this share is significant, since a
            # count can't see area-based credit. Same proportional
            # partial-edge treatment as tree_count above.
            s_frac = s_dist_m / self._length[start.edge]
            e_frac = e_dist_m / self._length[end.edge]
            canopy_score = (
                s_frac * float(self._tree_park_canopy[start.edge])
                + float(self._tree_park_canopy[edge_path].sum())
                + e_frac * float(self._tree_park_canopy[end.edge])
            )
            walked_tree_score = (
                s_frac * float(self._tree_deciduous[start.edge] + self._tree_evergreen[start.edge])
                + float((self._tree_deciduous[edge_path] + self._tree_evergreen[edge_path]).sum())
                + e_frac * float(self._tree_deciduous[end.edge] + self._tree_evergreen[end.edge])
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
            # 0.0 when the route has no tree score at all -- "no trees" is
            # not "all canopy".
            # float() strips the numpy float32 the _length division leaks
            # into these sums -- pydantic can't serialize numpy scalars.
            "park_canopy_share": (
                round(float(canopy_score) / float(walked_tree_score), 3)
                if walked_tree_score else 0.0
            ),
            "segments": [
                {"name": s["name"], "length_m": round(s["length_m"], 1)} for s in segments
            ],
        }
