"""Building shade per edge, by month and hour -- the second shade LAYER.

WHAT IT ANSWERS
---------------
For every edge and every (month, hour) slot of pipeline/sun.py's table:
the share of the edge's sample points that lie in a building's shadow,
stored 0-255. Trees are the first layer (blocks.py / canopy.py); the
server combines the two by union, 1 - (1 - t)(1 - b). Routing stays
OSM-only -- this is a label on edges, like trees. Every kind of edge is
scored, crossings included (trees skip crossings; buildings do shade
roadways).

THE PHYSICS, FLAT GROUND
------------------------
A sample point p is shaded for sun (azimuth az, elevation el) iff, walking
from p toward the sun, a building at distance d has height H >= d.tan(el).
Any first blocker settles it, so what stands behind it never matters, and
a building across the street counts exactly like one beside you. Ground
is flat.

SAMPLE POINTS
-------------
Each edge is sliced every config.SHADOW_SAMPLE_STEP_M and probed at slice
centres (blockface.py's rule -- never the edge's midpoint). At each slice
three points across the line, at config.SHADOW_STRIP_OFFSETS_M along the
local perpendicular, so the fraction is measured over the same 2 m walker
strip the tree layer uses. Points that fall inside a footprint
(misalignment, arcades; <= 0.2% of points, measured 2026-09-08) are
excluded from the fraction and tallied, never counted as shaded or as
sunny.

TWO ENGINES, ONE ANSWER
-----------------------
  - Raster march (all buildings): footprints are rasterized onto a grid
    of obstacle-top heights at config.SHADOW_CELL_M, tallest wins. For
    each slot the ray from every point is walked toward the sun in
    config.SHADOW_MARCH_STEP_M hops, vectorised across all points, until
    the eye-line has climbed above config.SHADOW_RASTER_HEIGHT_CAP_M (no
    building the raster is trusted for can shade beyond that) or the
    point is known shaded. Cost is O(points x hops), independent of how
    many buildings there are. Its error is positional: a shadow's edge
    lands within about one cell plus one hop of the truth.
  - Exact sweep (buildings ABOVE the cap): the shadow of a flat-roofed
    building is its footprint swept away from the sun by H / tan(el),
    built as footprint + translated footprint + one quad per ring
    segment; a point inside that polygon is shaded. Bulk point-in-polygon
    via an STRtree over the tile's points. Exact, but per building per
    slot, so reserved for the few whose shadows outrun the march.
  Both stop at config.SHADOW_MAX_REACH_M. `raster_shaded` and
  `sweep_shaded` are public so the feasibility instrument can run either
  on the same points and measure their disagreement.

Everything is done in the pipeline's flat-metre space (naming._to_m --
the convention every pipeline measurement uses; config.METRIC_CRS is
prose). Work is tiled: config.SHADOW_TILE_M squares of sample points, each
rasterizing only the buildings within its reach margin, so memory stays
in the tens of MB and tiles could be spread over processes later.

OUTPUT
------
`edge["building_shade"]` = 288 bytes, month-major (index = (month-1)*24 +
hour), value = round(255 x shaded / sampled); night slots and edges with
no valid point are 0. Bytes, not a list of ints: 488k lists of 288 would
be ~1 GB of pointers; the export decides the on-disk packing.

A whole-city pass is ~11.7M points and several hours. The per-tile point
objects, the uint16 tally and the 2 km tiles keep it inside a 16 GB
laptop's memory; a first cut with citywide point objects and 4 km tiles
swapped at 8.6 GB.
"""

import logging
import math
import time

import numpy as np
import rasterio.features
import rasterio.transform
import shapely
from shapely.affinity import translate
from shapely.geometry import Polygon, shape
from shapely.strtree import STRtree

from pipeline import config
from pipeline import sun as sun_module
from pipeline.graph.naming import _to_m

logger = logging.getLogger(__name__)

_M_PER_FT = 0.3048
SLOT_COUNT = sun_module.SLOT_COUNT


# ── sample points ────────────────────────────────────────────────────────────

def _sample_edge(coords_m: np.ndarray, step_m: float, offsets_m) -> np.ndarray:
    """Sample points for one edge as an (n, 2) array in metres.

    `coords_m` is the (k, 2) polyline. Slices tile the whole line, one
    probe per slice at its centre, and at each probe every offset along
    the perpendicular of the segment the probe sits on.
    """
    if len(coords_m) < 2:
        return np.empty((0, 2))
    seg = np.diff(coords_m, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    total = float(seg_len.sum())
    if total < 1e-9:
        return np.empty((0, 2))
    slices = max(1, math.ceil(total / step_m))
    slice_m = total / slices
    targets = (np.arange(slices) + 0.5) * slice_m
    cum = np.concatenate(([0.0], np.cumsum(seg_len)))
    index = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, len(seg) - 1)
    # A zero-length segment would divide by zero; fall back to a unit
    # along-x direction there (it contributes no length anyway).
    length = np.where(seg_len[index] > 0, seg_len[index], 1.0)
    along = (targets - cum[index]) / length
    centres = coords_m[index] + seg[index] * along[:, None]
    direction = seg[index] / length[:, None]
    normal = np.stack([-direction[:, 1], direction[:, 0]], axis=1)
    points = [centres + normal * offset for offset in offsets_m]
    return np.concatenate(points, axis=0)


def sample_points(edges: list[dict], step_m: float | None = None,
                  offsets_m=None) -> tuple[np.ndarray, np.ndarray]:
    """All sample points for `edges`: (points (n, 2) in metres, edge index (n,))."""
    step_m = config.SHADOW_SAMPLE_STEP_M if step_m is None else step_m
    offsets_m = config.SHADOW_STRIP_OFFSETS_M if offsets_m is None else offsets_m
    chunks, owners = [], []
    for edge_index, edge in enumerate(edges):
        coords = edge.get("coords") or []
        coords_m = np.array([_to_m(lon, lat) for lon, lat in coords], dtype=float)
        pts = _sample_edge(coords_m, step_m, offsets_m)
        if len(pts):
            chunks.append(pts)
            owners.append(np.full(len(pts), edge_index, dtype=np.int64))
    if not chunks:
        return np.empty((0, 2)), np.empty(0, dtype=np.int64)
    return np.concatenate(chunks), np.concatenate(owners)


# ── buildings ────────────────────────────────────────────────────────────────

def prepare_buildings(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(geometries in metres, heights in metres) from raw footprint rows
    (pipeline/fetch/buildings.py's `usable()` output)."""
    geoms, heights = [], []
    for row in rows:
        height_ft = float(row["height_roof"])
        geom = shape(row["the_geom"])
        geoms.append(shapely.transform(geom, _lonlat_to_m))
        heights.append(height_ft * _M_PER_FT)
    return np.array(geoms, dtype=object), np.array(heights, dtype=float)


def _lonlat_to_m(xy: np.ndarray) -> np.ndarray:
    lon, lat = xy[:, 0], xy[:, 1]
    xs, ys = _to_m(lon, lat)
    return np.stack([xs, ys], axis=1)


# ── raster march ─────────────────────────────────────────────────────────────

def build_height_grid(geoms, heights, bounds, cell_m):
    """Obstacle-top heights on a grid covering `bounds` = (x0, y0, x1, y1).

    Returns (grid (rows, cols) float32, x0, y1): row 0 is the NORTH edge,
    the rasterio convention. Tallest wins where footprints overlap.
    """
    x0, y0, x1, y1 = bounds
    cols = max(1, math.ceil((x1 - x0) / cell_m))
    rows = max(1, math.ceil((y1 - y0) / cell_m))
    y_top = y0 + rows * cell_m
    transform = rasterio.transform.from_origin(x0, y_top, cell_m, cell_m)
    if len(geoms) == 0:
        return np.zeros((rows, cols), dtype=np.float32), x0, y_top
    order = np.argsort(heights)   # paint short first so the tallest wins
    shapes = [(geoms[i], float(heights[i])) for i in order]
    grid = rasterio.features.rasterize(
        shapes, out_shape=(rows, cols), transform=transform, fill=0.0,
        dtype="float32", merge_alg=rasterio.enums.MergeAlg.replace)
    return grid, x0, y_top


def raster_shaded(points: np.ndarray, grid: np.ndarray, x0: float, y_top: float,
                  cell_m: float, azimuth: float, elevation: float,
                  march_step_m: float, height_cap_m: float,
                  max_reach_m: float) -> np.ndarray:
    """Boolean (n,): which points the raster march finds shaded.

    Hops of `march_step_m` toward the sun; at hop k the eye-line is at
    k.step.tan(el), and any cell at least that tall blocks it. The march
    stops where no building up to `height_cap_m` could still block
    (height_cap / tan(el)), or at `max_reach_m`.
    """
    n = len(points)
    shaded = np.zeros(n, dtype=bool)
    if n == 0 or elevation <= 0:
        return shaded
    tan_el = math.tan(math.radians(elevation))
    reach = min(height_cap_m / tan_el, max_reach_m)
    hops = int(math.ceil(reach / march_step_m))
    ux = math.sin(math.radians(azimuth)) * march_step_m
    uy = math.cos(math.radians(azimuth)) * march_step_m
    rows, cols = grid.shape
    active = np.arange(n)
    px, py = points[:, 0], points[:, 1]
    for k in range(1, hops + 1):
        if len(active) == 0:
            break
        x = px[active] + k * ux
        y = py[active] + k * uy
        col = np.floor((x - x0) / cell_m).astype(np.int64)
        row = np.floor((y_top - y) / cell_m).astype(np.int64)
        inside = (col >= 0) & (col < cols) & (row >= 0) & (row < rows)
        h = np.zeros(len(active), dtype=np.float32)
        h[inside] = grid[row[inside], col[inside]]
        hit = h >= k * march_step_m * tan_el
        shaded[active[hit]] = True
        active = active[~hit]
    return shaded


# ── exact sweep ──────────────────────────────────────────────────────────────

def shadow_polygon(geom, height_m: float, azimuth: float, elevation: float,
                   max_reach_m: float):
    """The ground shadow of a flat-roofed footprint: the footprint swept
    away from the sun by height / tan(el), capped at `max_reach_m`."""
    length = min(height_m / math.tan(math.radians(elevation)), max_reach_m)
    dx = -math.sin(math.radians(azimuth)) * length
    dy = -math.cos(math.radians(azimuth)) * length
    parts = [geom, translate(geom, dx, dy)]
    polygons = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for polygon in polygons:
        for ring in (polygon.exterior, *polygon.interiors):
            xy = np.asarray(ring.coords)
            for (ax, ay), (bx, by) in zip(xy[:-1], xy[1:]):
                parts.append(Polygon([(ax, ay), (bx, by), (bx + dx, by + dy), (ax + dx, ay + dy)]))
    return shapely.union_all(parts)


def sweep_shaded(point_tree: STRtree, n_points: int, geoms, heights,
                 azimuth: float, elevation: float, max_reach_m: float) -> np.ndarray:
    """Boolean (n_points,): points inside any of the buildings' shadows.
    `point_tree` is an STRtree over the points (built once per tile)."""
    shaded = np.zeros(n_points, dtype=bool)
    if elevation <= 0 or len(geoms) == 0:
        return shaded
    shadows = [shadow_polygon(g, h, azimuth, elevation, max_reach_m)
               for g, h in zip(geoms, heights)]
    _, point_idx = point_tree.query(shadows, predicate="contains")
    shaded[point_idx] = True
    return shaded


# ── the pass ─────────────────────────────────────────────────────────────────

def _tile_keys(points: np.ndarray, tile_m: float) -> np.ndarray:
    return np.floor(points / tile_m).astype(np.int64)


def score_building_shade(edges: list[dict], footprints, sun_table, *, step_m=None,
                         cell_m=None, march_step_m=None, max_reach_m=None,
                         tile_m=None) -> dict:
    """Set `building_shade` (288 bytes) on every edge. Returns the tally.

    `footprints` is `prepare_buildings()`'s (geometries, heights): the
    caller converts first and drops the raw rows, so the 1.08M row dicts
    (~1.5 GB) are not alive during the pass. The keyword overrides exist
    for the feasibility instrument; the pipeline passes none of them.

    Memory: shapely point objects exist for one tile at a time, never
    citywide (11.7M of them is the largest single cost); the shaded-count
    table is uint16; edge owners are int32. Progress is logged per tile
    with a running estimate, because a whole-city pass is hours and the
    step is otherwise silent.
    """
    step_m = config.SHADOW_SAMPLE_STEP_M if step_m is None else step_m
    cell_m = config.SHADOW_CELL_M if cell_m is None else cell_m
    march_step_m = config.SHADOW_MARCH_STEP_M if march_step_m is None else march_step_m
    max_reach_m = config.SHADOW_MAX_REACH_M if max_reach_m is None else max_reach_m
    tile_m = config.SHADOW_TILE_M if tile_m is None else tile_m
    cap_m = config.SHADOW_RASTER_HEIGHT_CAP_M
    geoms, heights = footprints

    started = time.perf_counter()
    points, owner = sample_points(edges, step_m)
    owner = owner.astype(np.int32)
    logger.info(f"  [shadows] {len(points):,} sample points on {len(edges):,} edges "
                f"({time.perf_counter() - started:.0f}s)")
    t0 = time.perf_counter()
    building_tree = STRtree(geoms)
    logger.info(f"  [shadows] index over {len(geoms):,} footprints ({time.perf_counter() - t0:.0f}s)")
    slots = [(m, h, sun_table[m - 1][h]) for m in range(1, 13) for h in range(24)
             if sun_table[m - 1][h] is not None]

    n_edges = len(edges)
    shaded_counts = np.zeros((n_edges, SLOT_COUNT), dtype=np.uint16)
    sampled = np.zeros(n_edges, dtype=np.int64)
    inside_count = 0
    keys = _tile_keys(points, tile_m)
    tiles = np.unique(keys, axis=0)
    slot_seconds = 0.0
    tiles_started = time.perf_counter()
    for tile_no, (tx, ty) in enumerate(tiles, 1):
        t_tile = time.perf_counter()
        idx = np.nonzero((keys[:, 0] == tx) & (keys[:, 1] == ty))[0]
        tile_points = points[idx]
        # Points inside a footprint: excluded everywhere, counted once. The
        # point objects live only for this tile.
        tile_geoms = shapely.points(tile_points)
        inside = np.zeros(len(idx), dtype=bool)
        inside[building_tree.query(tile_geoms, predicate="within")[0]] = True
        inside_count += int(inside.sum())
        idx, tile_points, tile_geoms = idx[~inside], tile_points[~inside], tile_geoms[~inside]
        sampled += np.bincount(owner[idx], minlength=n_edges)
        if len(idx) == 0:
            continue

        x0, y0 = tx * tile_m - max_reach_m, ty * tile_m - max_reach_m
        x1, y1 = (tx + 1) * tile_m + max_reach_m, (ty + 1) * tile_m + max_reach_m
        near = building_tree.query(shapely.box(x0, y0, x1, y1))
        grid, gx0, gy_top = build_height_grid(geoms[near], heights[near],
                                              (x0, y0, x1, y1), cell_m)
        tall = near[heights[near] > cap_m]
        point_tree = STRtree(tile_geoms) if len(tall) else None
        tile_owner = owner[idx]

        for month, hour, (azimuth, elevation) in slots:
            t0 = time.perf_counter()
            shaded = raster_shaded(tile_points, grid, gx0, gy_top, cell_m, azimuth,
                                   elevation, march_step_m, cap_m, max_reach_m)
            if len(tall):
                shaded |= sweep_shaded(point_tree, len(idx), geoms[tall], heights[tall],
                                       azimuth, elevation, max_reach_m)
            slot = (month - 1) * 24 + hour
            shaded_counts[:, slot] += np.bincount(tile_owner[shaded], minlength=n_edges).astype(np.uint16)
            slot_seconds += time.perf_counter() - t0
        del grid, point_tree, tile_geoms

        elapsed = time.perf_counter() - tiles_started
        remaining = elapsed / tile_no * (len(tiles) - tile_no)
        logger.info(f"  [shadows] tile {tile_no}/{len(tiles)}: {len(idx):,} points, "
                    f"{len(near):,} buildings ({len(tall):,} tall), "
                    f"{time.perf_counter() - t_tile:.0f}s -- elapsed {elapsed / 60:.0f}m, "
                    f"~{remaining / 60:.0f}m left")

    assert sampled.max() < 65535, "an edge has more sample points than the uint16 tally holds"
    with np.errstate(divide="ignore", invalid="ignore"):
        fraction = np.where(sampled[:, None] > 0,
                            shaded_counts / np.maximum(sampled, 1)[:, None], 0.0)
    table = np.rint(fraction * 255).astype(np.uint8)
    for edge_index, edge in enumerate(edges):
        edge["building_shade"] = table[edge_index].tobytes()

    tally = {
        "edges": n_edges,
        "edges_with_shade": int((table.any(axis=1)).sum()),
        "edges_without_points": int((sampled == 0).sum()),
        "slots_daylight": len(slots),
        "points": int(len(points)),
        "points_inside_footprints": inside_count,
        "buildings": int(len(geoms)),
        "buildings_tall": int((heights > cap_m).sum()),
        "tiles": int(len(tiles)),
        "seconds": time.perf_counter() - started,
        "seconds_per_slot": (slot_seconds / len(slots)) if slots else 0.0,
    }
    logger.info(f"  [shadows] {tally['edges']:,} edges scored ({tally['edges_with_shade']:,} "
                f"with any shade, {tally['edges_without_points']:,} without points), "
                f"{tally['slots_daylight']} slots daylight, {tally['points']:,} points, "
                f"{tally['points_inside_footprints']:,} inside footprints (excluded), "
                f"{tally['buildings']:,} buildings ({tally['buildings_tall']:,} above the "
                f"raster cap), {tally['tiles']} tiles, {tally['seconds_per_slot']:.2f}s/slot, "
                f"{tally['seconds']:.0f}s")
    return tally
