"""In-memory routing graph, loaded once at server startup from data/export/.

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

import array
import logging
import gzip
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import igraph
import base64
import calendar
import ijson
import numpy as np
import shapely
from shapely.geometry import Point
from shapely.ops import substring
from shapely.strtree import STRtree

from pipeline import config

logger = logging.getLogger(__name__)

# Building-shade table layout (pipeline/sun.py, meta.shade_slots): 12 months
# x 24 hours, month-major; rows are the sun on the ANCHOR day of the month.
SHADE_SLOTS = 288
SHADE_ANCHOR_DAY = 15
SHADE_LAYERS = ("trees", "buildings", "both")
DENSITY_CACHE_SIZE = 8


def _slot(month: int, hour: int) -> int:
    """Index of (month, hour) in a shade row: month-major, month 1-12."""
    return (month - 1) * 24 + hour


def _blend_weights(month: int, day: int, hour: int, minute: int) -> list[tuple[int, float]]:
    """The four (slot, weight) pairs a moment blends -- the two hours
    around the minute, in the two months around the day. The weights sum
    to 1; _building_fraction's docstring has the rule and its examples. On
    an anchor (minute 0, day 15) some weights are 0.0: those slots are not
    part of the moment, and is_night ignores them."""
    f = minute / 60.0
    h0, h1 = hour, (hour + 1) % 24
    if day >= SHADE_ANCHOR_DAY:
        m0, m1 = month, month % 12 + 1
        g = (day - SHADE_ANCHOR_DAY) / calendar.monthrange(config.SUN_ANCHOR_YEAR, m0)[1]
    else:
        m0, m1 = (month - 2) % 12 + 1, month
        span = calendar.monthrange(config.SUN_ANCHOR_YEAR, m0)[1]
        g = (span - SHADE_ANCHOR_DAY + day) / span
    return [
        (_slot(m0, h0), (1.0 - g) * (1.0 - f)),
        (_slot(m0, h1), (1.0 - g) * f),
        (_slot(m1, h0), g * (1.0 - f)),
        (_slot(m1, h1), g * f),
    ]


def _dark_slots(sun_table) -> np.ndarray:
    """(SHADE_SLOTS,) bool, laid out like a shade row: True where the
    export's sun table holds None -- the sun at or below the horizon at
    that slot's anchor (pipeline/sun.py). No table (an export from before
    the shade step; the dedupe tests' tiles) means no known night: all
    False, so those exports keep their trees-only nights exactly."""
    dark = np.zeros(SHADE_SLOTS, dtype=bool)
    if sun_table is None:
        return dark
    if len(sun_table) != 12 or any(len(row) != 24 for row in sun_table):
        raise ValueError("meta.sun_table must be 12 months x 24 hours")
    for month_index, row in enumerate(sun_table):
        for hour, sun in enumerate(row):
            if sun is None:
                dark[_slot(month_index + 1, hour)] = True
    return dark


# load()'s dedupe keys pack (node pair, multigraph key, side) into ONE int
# per edge (see load()'s MEMORY SHAPE): the key gets 17 bits, the side code
# 3. load() raises rather than let an export that exceeds them collide.
_DEDUPE_KEY_BITS = 17
_DEDUPE_SIDE_BITS = 3
_HASH_MASK = (1 << 64) - 1


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


# ── Turn-by-turn step building ──────────────────────────────────────────
#
# A route arrives as tiny legs -- one per graph edge, chopped wherever OSM
# chopped the pavement -- and used to be rendered one line per name change,
# which made a 3km walk read as 72 lines: every corner is
# [run along A] [8m crossing] [run along A], so the crossings and kerb
# scraps shredded each street into fragments. Geometry alone cannot fix it
# (a filter loose enough to keep real corners keeps 2m kerb jogs; one tight
# enough to drop the jogs collapsed the Brooklyn Bridge to a single step --
# measured 2026-08-23). So folding is EVIDENCE-based, no length thresholds:
#
#   - a crossing/traffic-island leg (OSM's own label) is never a street
#     someone walks along: it folds into the runs around it regardless of
#     its own derived name (which under parallel naming is usually the
#     along-street anyway, but for a crossing over your own street is the
#     side street -- either way a labeling artifact, not a walk).
#   - an unnamed leg folds into an adjacent run only when that run's name
#     is among the leg's own fold_names (pipeline/graph/naming.py's
#     plausible-parents evidence). A nameless path NO street claims stays
#     an honest "unnamed path" step whatever its length -- that is the 69m
#     park cut-through, and hiding it would misreport where the walker
#     walks.
#   - a step boundary is where the street NAME changes; bearings supply
#     only the word (left/right/straight).
#   - a side switch inside one street ("cross to the north side") is
#     believed only when a crossing leg sits between the two sides --
#     you cannot switch sides of a roadway without crossing it, so a
#     single mis-computed side value cannot inject a phantom step.

_STRAIGHT_MAX_DEG = 30.0   # |turn| at most this reads as "continue"
_SHARP_MIN_DEG = 150.0     # beyond this, "sharp left/right"
_BEARING_SAMPLE_M = 20.0   # how much of a run's geometry sets its bearing

_SIDE_WORDS = {"N": "north", "S": "south", "E": "east", "W": "west"}
_COMPASS8 = ["north", "northeast", "east", "southeast",
             "south", "southwest", "west", "northwest"]


def _is_connector_kind(kind: str) -> bool:
    # Vocabulary shared with naming's connector skip: what folds here as a
    # connector is exactly what naming declines to derive a name for.
    return any(marker in kind for marker in config.CONNECTOR_KIND_MARKERS)


def _bearing(a, b) -> float:
    """Forward bearing a->b in degrees clockwise from north. Flat-earth
    with the cos(lat) correction -- fine at the ~20m scale bearings are
    sampled over."""
    dx = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = b[1] - a[1]
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _run_bearing(coords, from_start: bool) -> float:
    """A run's direction over ~_BEARING_SAMPLE_M of geometry: leaving its
    start (from_start) or arriving at its end (facing travel direction
    both ways)."""
    points = coords if from_start else coords[::-1]
    accumulated = 0.0
    chosen = points[min(1, len(points) - 1)]
    for a, b in zip(points, points[1:]):
        accumulated += math.hypot(
            (b[0] - a[0]) * math.cos(math.radians(a[1])) * 111_320.0,
            (b[1] - a[1]) * 110_540.0)
        chosen = b
        if accumulated >= _BEARING_SAMPLE_M:
            break
    bearing = _bearing(points[0], chosen)
    return bearing if from_start else (bearing + 180.0) % 360.0


def _run_coords(run) -> list:
    points: list = []
    for leg in run["legs"]:
        points.extend(leg["coords"] if not points else leg["coords"][1:])
    return points


def _foldable_into(leg, name: str) -> bool:
    """May this leg vanish into a run called `name`?"""
    if _is_connector_kind(leg["kind"]):
        return True
    return not leg["name"] and name in leg["fold_names"]


def _display_name(leg) -> str:
    """Crossings never form runs under their own name -- their derived
    name is which street they cross, not a street being walked along."""
    return "" if _is_connector_kind(leg["kind"]) else leg["name"]


def _build_runs(legs: list[dict]) -> list[dict]:
    """Consecutive same-name legs -> runs, then fold interruptions into
    the street runs around them on the evidence rules above."""
    runs: list[dict] = []
    for leg in legs:
        if round(leg["length_m"], 1) <= 0:
            continue
        name = _display_name(leg)
        if runs and runs[-1]["name"] == name:
            runs[-1]["legs"].append(leg)
        else:
            runs.append({"name": name, "legs": [leg]})

    def is_interruption(run) -> bool:
        return run["name"] == ""

    def foldable(run, name) -> bool:
        return all(_foldable_into(leg, name) for leg in run["legs"])

    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            if is_interruption(runs[i]) \
                    and runs[i - 1]["name"] == runs[i + 1]["name"] \
                    and runs[i - 1]["name"] != "" \
                    and foldable(runs[i], runs[i - 1]["name"]):
                runs[i - 1]["legs"].extend(runs[i]["legs"])
                runs[i - 1]["legs"].extend(runs[i + 1]["legs"])
                del runs[i:i + 2]
                changed = True
                break
        if changed:
            continue
        # a connector at a NAME boundary (turning off A onto B) is
        # transition ground; it joins the arrival street's step. The
        # crossing at a corner is the common case.
        for i in range(1, len(runs) - 1):
            if not is_interruption(runs[i]):
                continue
            if foldable(runs[i], runs[i + 1]["name"]):
                runs[i + 1]["legs"][0:0] = runs[i]["legs"]
                del runs[i]
                changed = True
                break
            if foldable(runs[i], runs[i - 1]["name"]):
                runs[i - 1]["legs"].extend(runs[i]["legs"])
                del runs[i]
                changed = True
                break
        if changed:
            continue
        if len(runs) >= 2 and is_interruption(runs[0]) \
                and foldable(runs[0], runs[1]["name"]):
            runs[1]["legs"][0:0] = runs[0]["legs"]
            del runs[0]
            changed = True
            continue
        if len(runs) >= 2 and is_interruption(runs[-1]) \
                and foldable(runs[-1], runs[-2]["name"]):
            runs[-2]["legs"].extend(runs[-1]["legs"])
            del runs[-1]
            changed = True

    for run in runs:
        run["length_m"] = sum(leg["length_m"] for leg in run["legs"])
    return runs


def _leg_bearing_180(coords) -> float:
    """A leg's direction end-to-end, mod 180 (undirected)."""
    return _bearing(coords[0], coords[-1]) % 180.0


def _split_run_at_side_switches(run) -> list[dict]:
    """One street run -> pieces at real side switches. A switch needs BOTH
    a new non-empty side and, since the last sided leg, a crossing
    PERPENDICULAR to the direction of travel -- crossing your own street
    is always across your path, while the corner crossing over a side
    street runs along it. Without the perpendicularity test, the 6.78% of
    block boundaries where the compass word shifts (bends, near-diagonal
    tilts -- measured on the 2026-08-28 export) would each fire a phantom
    "cross to the X side" at their corner crossing. A side flip with no
    perpendicular crossing behind it is treated as the data artifact it
    is and ignored.

    Each piece's displayed side is the word carrying a strict MAJORITY of
    the piece's sided length -- a run that genuinely bends between words
    shows none rather than the first half's."""
    pieces = [{"name": run["name"], "legs": [], "switched": False}]
    last_side = ""         # last side actually committed
    crossed_since = False  # perpendicular crossing since the last sided leg
    travel_bearing = None  # last walked (non-connector) leg's direction
    for leg in run["legs"]:
        if _is_connector_kind(leg["kind"]):
            if travel_bearing is not None and len(leg["coords"]) >= 2:
                d = abs(_leg_bearing_180(leg["coords"]) - travel_bearing)
                if min(d, 180.0 - d) >= 60.0:
                    crossed_since = True
        else:
            if len(leg["coords"]) >= 2 and round(leg["length_m"], 1) > 0:
                travel_bearing = _leg_bearing_180(leg["coords"])
        side = leg["side"]
        if side:
            if not last_side or side == last_side:
                last_side = side
                crossed_since = False
            elif crossed_since:
                pieces.append({"name": run["name"], "legs": [],
                               "switched": True})
                last_side = side
                crossed_since = False
            # else: a flip with no perpendicular crossing -- ignored
        pieces[-1]["legs"].append(leg)
    for piece in pieces:
        piece["length_m"] = sum(leg["length_m"] for leg in piece["legs"])
        by_side: dict[str, float] = {}
        sided_total = 0.0
        for leg in piece["legs"]:
            if leg["side"]:
                by_side[leg["side"]] = (by_side.get(leg["side"], 0.0)
                                        + leg["length_m"])
                sided_total += leg["length_m"]
        piece["side"] = ""
        for side, side_length in by_side.items():
            if side_length * 2 > sided_total:
                piece["side"] = side
    return pieces


def build_steps(legs: list[dict]) -> list[dict]:
    """Raw route legs -> turn-by-turn steps, the response's `segments`.

    Each step: {action, name, side, heading, length_m}.
      action   "depart" | "continue" | "left" | "right" | "sharp_left" |
               "sharp_right" | "cross_side"
      name     street name, or "unnamed path"
      side     "north"/"south"/"east"/"west" or "" -- which side of the
               street this stretch walks
      heading  8-way compass word on "depart" steps, "" otherwise
      length_m rounded to 0.1
    """
    pieces: list[dict] = []
    for run in _build_runs(legs):
        pieces.extend(_split_run_at_side_switches(run))

    steps: list[dict] = []
    previous = None
    for piece in pieces:
        name = piece["name"] or "unnamed path"
        side = _SIDE_WORDS.get(piece["side"], "")
        coords = _run_coords(piece)
        if previous is None:
            outbound = _run_bearing(coords, from_start=True)
            action = "depart"
            heading = _COMPASS8[int(((outbound + 22.5) % 360) // 45)]
        elif piece.get("switched"):
            action, heading = "cross_side", ""
        else:
            inbound = _run_bearing(_run_coords(previous), from_start=False)
            outbound = _run_bearing(coords, from_start=True)
            delta = ((outbound - inbound + 180.0) % 360.0) - 180.0
            heading = ""
            if abs(delta) <= _STRAIGHT_MAX_DEG:
                action = "continue"
            elif abs(delta) >= _SHARP_MIN_DEG:
                action = "sharp_right" if delta > 0 else "sharp_left"
            else:
                action = "right" if delta > 0 else "left"
        steps.append({"action": action, "name": name, "side": side,
                      "heading": heading,
                      "length_m": round(piece["length_m"], 1)})
        previous = piece
    return steps


def clamp_shade_monotonic(routes: list[dict], weights: list[float]) -> list[dict]:
    """Enforce the Shade_priority promise across a batch of routes: as
    tree_weight increases, a route's shade_fraction must never DECREASE.
    Returns a new list aligned with `routes`/`weights`; any route that would
    break the guarantee is replaced by the shadiest lower-or-equal-weight
    route already in the batch.

    Why this is needed: since 2026-08-26 cost and display share one
    saturation point (config.DENSITY_AT_FULL_COVERAGE), so the old driver
    -- the router rewarding density the stat could not credit -- is gone.
    What remains is photo-finishes: near-tied routes swapping at a higher
    weight, where the winner's displayed FRACTION lands a hair lower
    (cost is hyperbolic per edge, the fraction is linear, so they rank
    near-equal mixtures differently). Measured on 188 random pairs
    (2026-08-26): 8 dips, every one 0.1-1.2 points, routes 1-21m apart.
    Also measured: widening the preset ladder multiplies the dips (8 ->
    23 at [0,8,30,150]) while buying ~3 points of median shade, so the
    guard scales with any future preset change. Earlier history: 0/24
    sampled pairs pre-clamp on 2026-08-17 -- a read that hid a 3.8% real
    rate until n=400, which is why this guard does not get retired on a
    small clean sample. /route computes every preset in one
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

def _local_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance between two nearby points -- fine at the
    few-meters-to-tens-of-meters scale it is used at (the standard
    ~111km-per-degree-of-latitude approximation, with the cos(lat)
    longitude correction)."""
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
        # Building shade: uint8 (SHADE_SLOTS, n_edges), slot rows contiguous
        # so one (month, hour) row is a single strided read -- see
        # _building_fraction. All-zero when the export predates the field
        # (the committed pilot fixture, tests' synthetic tiles).
        self._building_shade = np.zeros((SHADE_SLOTS, 0), dtype=np.uint8)
        self._sun_table = None   # meta.sun_table as loaded; routing reads it only via _dark_slots
        # Which slots are night in that table -- a dark slot counts as FULL
        # shade (_building_fraction). All False until a table loads.
        self._dark_slots = np.zeros(SHADE_SLOTS, dtype=bool)
        # Density arrays per (month, day, hour, minute, layers): route()
        # calls _edge_density 8x per request (2 per weight x 4), and the
        # blend reads four rows of the table each time. Tiny and
        # idempotent, so a thread race just recomputes the same array.
        # Derived from the arrays above: anything that mutates them in
        # place (tests do) must clear it.
        self._density_cache: dict[tuple, np.ndarray] = {}
        self._names: list[str] = []
        # Direction-rendering fields (2026-08-28): OSM's own kind
        # ("footway/crossing"...) so crossings fold into the street run
        # they interrupt; the COMPASS side of the parent street; and the
        # fold_names evidence for absorbing nameless scraps. All three
        # default empty for exports that predate them.
        self._kinds: list[str] = []
        self._sides: list[str] = []
        self._fold_names: list[tuple] = []
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
        """Read every export file in EXPORT_DIR into one merged graph.

        Normally that is exactly one file (the citywide export), but the
        glob-and-merge shape is load-bearing: the e2e tier points
        EXPORT_DIR at a directory holding only the pilot fixture.

        MEMORY SHAPE (`server-memory`, 2026-09-26): every per-edge
        temporary lives in a flat buffer (array.array / bytearray), never
        as one Python object per edge. CPython's small-object allocator
        returns memory to the OS only in whole 1 MiB arenas, and only once
        an arena is completely empty. The previous loader built ~520 MB of
        small temporaries (floats, id strings, dedupe tuples, an ndarray
        and a bytes row per edge) interleaved with the ~120 MB of objects
        that stay (node ids, names), so no arena ever emptied and the
        process kept its load-time peak for life. Measured on Linux (an
        Ubuntu 24.04 container calibrated to the box within 1-2%): 1,149
        MB after load then, 574 MB now, output identical attribute for
        attribute and route for route (HISTORY 2026-09-26). The only
        per-edge Python objects left are the ones ijson creates for the
        edge being read -- freed before the next edge, so the next one
        reuses their memory -- and the permanent ones, which are SHARED:
        one object per distinct street name, side or fold tuple. Keep it
        that way: a per-edge list of fresh Python objects brings the
        retention back."""
        export_paths = sorted(config.EXPORT_DIR.glob("*.json.gz"))
        if not export_paths:
            raise FileNotFoundError(
                f"No graph data in {config.EXPORT_DIR}. Build it with "
                "`uv run python -m pipeline.build`, or point "
                "SHADEWALKER_EXPORT_DIR at a directory holding a built "
                "export to run against that instead."
            )
        # Pass 1: every node from every export file, so the complete node universe
        # is known before any edge is ingested. STREAMED (ijson straight off
        # the gzip stream), never json.loads of the whole file: the parsed
        # citywide payload alone costs ~1.0GB as Python objects (measured
        # 2026-09-01 -- 489k edge dicts whose coords are lists of lists of
        # Python floats, ~50x the bytes of the same points as arrays), and
        # holding it is what OOM'd the 2GB droplet. Each file is re-streamed
        # in pass 2 rather than held; the second decompress+parse costs
        # seconds and keeps peak RAM near steady-state. Coordinates go
        # straight into one flat double array (lon, lat, lon, lat, ...).
        node_xy = array.array("d")
        for path in export_paths:
            with gzip.open(path, "rb") as f:
                for node_id, (lon, lat) in ijson.kvitems(f, "nodes", use_float=True):
                    if node_id not in self._id_to_idx:
                        self._id_to_idx[node_id] = len(node_xy) // 2
                        node_xy.append(lon)
                        node_xy.append(lat)
            # The sun table sits in meta at the top of the file; the
            # generator is abandoned as soon as it yields, so this costs a
            # few KB of parsing, not a pass.
            with gzip.open(path, "rb") as f:
                table = next(ijson.items(f, "meta.sun_table", use_float=True), None)
                if table is not None:
                    self._sun_table = table
        self._dark_slots = _dark_slots(self._sun_table)
        n_nodes = len(node_xy) // 2

        # Pass 2 state: flat columns, one entry per KEPT edge, all in step.
        u_col = array.array("q")   # endpoints as node indices, in the export's own (u, v) order
        v_col = array.array("q")
        length = array.array("d")
        deciduous = array.array("d")
        evergreen = array.array("d")
        counts = array.array("d")
        # .get()-defaulted on read: tiles exported before v19 (the committed
        # pilot test fixture) predate the field entirely, and 0.0 is exactly
        # what they mean -- no canopy credit was computed for them.
        canopy_credit = array.array("d")
        # Lists of SHARED objects (see the docstring): the list itself is one
        # buffer; its entries point at one object per distinct value.
        names: list[str] = []
        kinds: list[str] = []
        sides: list[str] = []
        fold_names: list[tuple] = []
        # Every edge's points, packed as they arrive; coord_start/coord_count
        # say where each kept edge's run is (a replaced duplicate's new run
        # sits at the end, so runs are gathered back into edge order below).
        coord_flat = array.array("d")
        coord_start = array.array("q")
        coord_count = array.array("q")
        # Building shade rows, SHADE_SLOTS bytes each, packed as they arrive;
        # shade_at[e] = which row is edge e's, or -1 (all zero / absent).
        shade_buf = bytearray()
        shade_at = array.array("q")
        seen_edges: dict[int, int] = {}   # dedupe key -> position in the columns above
        seen_geometries: set[int] = set()  # (pair, side, geometry hash) keys -- see below
        side_codes: dict[str, int] = {}
        shared_strings: dict = {}
        shared_folds: dict[tuple, tuple] = {}

        def _shared(value):
            """The one object stored for this value (first seen wins)."""
            return shared_strings.setdefault(value, value)

        def _stash_coords(coords: np.ndarray) -> tuple[int, int]:
            start = len(coord_flat) // 2
            coord_flat.frombytes(coords.tobytes())
            return start, len(coords)

        def _stash_shade(shade: bytes | None) -> int:
            if shade is None:
                return -1
            row = len(shade_buf) // SHADE_SLOTS
            shade_buf.extend(shade)
            return row

        def _emit(u_idx, v_idx, key, side, seg_length_m, decid, everg,
                  cnt, canopy, name, kind, folds, coords, shade):
            """Add one edge (a whole edge, or one piece of a split one) to
            the graph arrays, through the existing border-dedupe. Splitting
            reduces the severed-overlap class to the already-solved
            duplicate-border-edge class: after the split, two overlapping
            copies have identical endpoints and identical coords, so this
            same dedupe collapses them."""
            # In the tiled era border edges appeared in two neighboring
            # tiles; a canonical (sorted) node pair makes both copies hash
            # identically. Each tile scored its copy against only its own
            # tree fetch, so the
            # copies can disagree -- when they do, keep the better-scored
            # one, not the first-seen one. Both copies count trees in the
            # identical corridor, so a copy can only be MISSING trees its
            # tile's fetch didn't cover, never have extras: higher tree
            # value == closer to complete. (First-seen-wins silently kept
            # the worse copy 3,740 times citywide, including a 1.7km Harlem
            # River Drive Greenway edge held at 0 trees while its other copy
            # had 95.)
            # The key is ONE int over node INDICES -- (sorted pair, key,
            # side) packed into bits -- not a tuple of two id strings: the
            # id <-> index map is a bijection, so equality is unchanged, and
            # it is one small object per edge instead of three.
            lo, hi = (u_idx, v_idx) if u_idx <= v_idx else (v_idx, u_idx)
            side_code = side_codes.setdefault(side, len(side_codes))
            if not (0 <= key < (1 << _DEDUPE_KEY_BITS) and side_code < (1 << _DEDUPE_SIDE_BITS)):
                raise ValueError(f"edge key {key!r} / side {side!r} outside the dedupe key's bit fields")
            pair = lo * n_nodes + hi
            dedupe_key = (((pair << _DEDUPE_KEY_BITS) | key) << _DEDUPE_SIDE_BITS) | side_code
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
                    kinds[existing] = kind
                    fold_names[existing] = folds
                    coord_start[existing], coord_count[existing] = _stash_coords(coords)
                    shade_at[existing] = _stash_shade(shade)
                return

            # OSM itself sometimes contains the same way twice -- identical
            # geometry between the same two nodes, which osmnx keeps as
            # parallel edges under different multigraph keys (159 confirmed
            # citywide). Keep one: same endpoints,
            # so dropping the extra copy can't disconnect anything. Hashing
            # the coords (direction-insensitive, via the array's bytes)
            # instead of storing them keeps this set small; genuinely
            # different parallel edges between the same nodes (a street and
            # a separate path) hash differently and both survive.
            geometry_hash = min(hash(coords.tobytes()), hash(coords[::-1].tobytes()))
            geometry_key = (((pair << _DEDUPE_SIDE_BITS) | side_code) << 64) | (geometry_hash & _HASH_MASK)
            if geometry_key in seen_geometries:
                return
            seen_geometries.add(geometry_key)

            seen_edges[dedupe_key] = len(u_col)
            u_col.append(u_idx)
            v_col.append(v_idx)
            length.append(seg_length_m)
            deciduous.append(decid)
            evergreen.append(everg)
            counts.append(cnt)
            canopy_credit.append(canopy)
            names.append(name)
            kinds.append(kind)
            sides.append(_shared(side))
            fold_names.append(folds)
            start, count = _stash_coords(coords)
            coord_start.append(start)
            coord_count.append(count)
            shade_at.append(_stash_shade(shade))

        # Pass 2: edges, streamed one dict at a time (freed as soon as it's
        # emitted), each through the dedupe above.
        for path in export_paths:
            with gzip.open(path, "rb") as f:
                for edge in ijson.items(f, "edges.item", use_float=True):
                    folds = tuple(_shared(n) for n in edge.get("fold_names", ()))
                    shade_b64 = edge.get("building_shade")
                    _emit(self._id_to_idx[edge["u"]], self._id_to_idx[edge["v"]],
                          int(edge["key"]), edge["side"],
                          edge["length_m"], edge["tree_deciduous"],
                          edge["tree_evergreen"], edge["tree_count"],
                          edge.get("tree_park_canopy", 0.0), _shared(edge["name"]),
                          # sys.intern: 488k kind strings are ~7 distinct
                          # values; interning stores each once.
                          sys.intern(edge.get("kind", "")),
                          shared_folds.setdefault(folds, folds),
                          np.asarray(edge["coords"], dtype=np.float64),
                          # base64 of SHADE_SLOTS uint8 (pipeline/export.py);
                          # absent = all zero (an all-zero row is omitted at
                          # export, and older files predate the field).
                          base64.b64decode(shade_b64) if shade_b64 else None)
        del seen_edges, seen_geometries, shared_strings, shared_folds

        self._names = names
        self._kinds = kinds
        self._sides = sides
        self._fold_names = fold_names
        self._node_lonlat = np.frombuffer(node_xy, dtype=np.float64).reshape(-1, 2).copy()
        del node_xy
        # The data's actual extent — whatever export files are loaded —
        # rather than a hardcoded bbox from pipeline/config.py, so the
        # same server code is correct for the citywide export and the
        # pilot fixture alike.
        lon_min, lat_min = self._node_lonlat.min(axis=0)
        lon_max, lat_max = self._node_lonlat.max(axis=0)
        self._bounds = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
        # Floored at 0.01m, in ONE place for every edge: 960 real exported
        # edges have length_m 0.0 -- a sub-5cm edge rounds to 0.0 at export
        # (pipeline/export.py rounds to 0.1m) -- and a snap landing on a
        # zero-length edge turns route()'s partial-edge division into
        # 0/0 -> NaN -> a crash at int(round(tree_count)). Found live on a
        # Central Park test route. 1cm on a <5cm edge distorts nothing.
        self._length = np.maximum(np.frombuffer(length, dtype=np.float64).astype(np.float32), 0.01)
        self._tree_deciduous = np.frombuffer(deciduous, dtype=np.float64).astype(np.float32)
        self._tree_evergreen = np.frombuffer(evergreen, dtype=np.float64).astype(np.float32)
        self._tree_count = np.frombuffer(counts, dtype=np.float64).astype(np.float32)
        self._tree_park_canopy = np.frombuffer(canopy_credit, dtype=np.float64).astype(np.float32)
        del length, deciduous, evergreen, counts, canopy_credit

        # Building shade, slot-major: gathered edge-major (one row per edge,
        # by shade_at) then transposed once, so each (month, hour) row is
        # contiguous for the blend's four strided reads.
        shade_rows_of = np.frombuffer(shade_at, dtype=np.int64)
        has_shade = shade_rows_of >= 0
        rows = np.frombuffer(shade_buf, dtype=np.uint8).reshape(-1, SHADE_SLOTS)
        by_edge = np.zeros((len(shade_rows_of), SHADE_SLOTS), dtype=np.uint8)
        by_edge[has_shade] = rows[shade_rows_of[has_shade]]
        self._building_shade = np.ascontiguousarray(by_edge.T)
        with_shade = int(has_shade.sum())
        del by_edge, rows, shade_rows_of, has_shade, shade_buf, shade_at
        self._density_cache.clear()
        if with_shade:
            logger.info(f"[graph_store] building shade on {with_shade:,} edges "
                        f"({self._building_shade.nbytes / 1e6:.0f} MB)")

        # Pack the edge shapes: one flat buffer + an offsets array (see
        # __init__). cumsum turns per-edge point counts into slice
        # boundaries — offsets[e] is where edge e's points start. The
        # gather re-orders coord_flat's runs into edge order (identity
        # unless a duplicate replaced an earlier copy's geometry).
        point_counts = np.frombuffer(coord_count, dtype=np.int64)
        run_starts = np.frombuffer(coord_start, dtype=np.int64)
        self._coord_offsets = np.concatenate(([0], np.cumsum(point_counts))).astype(np.int64)
        points = np.frombuffer(coord_flat, dtype=np.float64).reshape(-1, 2)
        point_index = (np.repeat(run_starts - self._coord_offsets[:-1], point_counts)
                       + np.arange(self._coord_offsets[-1]))
        self._coord_buf = points[point_index]
        del point_counts, run_starts, points, point_index, coord_flat, coord_start, coord_count

        edge_pairs = np.column_stack((np.frombuffer(u_col, dtype=np.int64),
                                      np.frombuffer(v_col, dtype=np.int64)))
        del u_col, v_col
        self._graph = igraph.Graph(n=n_nodes, edges=edge_pairs, directed=False)

        # No filtering here anymore -- every component is kept. This used to
        # drop everything but the largest connected component, because
        # rectangular borough bboxes deliberately overreached past the real
        # coastline (see the old BROOKLYN_BBOX), sweeping in street
        # fragments from across the water (Jersey City, a Lower Manhattan
        # sliver, the Rockaways) with no real connection to the rest of the
        # data. That's no longer possible: pipeline/graph/pedestrian.py
        # clips every way against the real borough polygons at read time,
        # before anything reaches data/export/, so every component here is trusted as
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
        self._edge_component = membership[edge_pairs[:, 0]]
        if len(components) > 1:
            sizes = sorted((len(component) for component in components), reverse=True)
            logger.info(f"[graph_store] {len(components)} disconnected components "
                  f"(sizes, largest 5: {sizes[:5]})")

        self._build_edge_index()

        logger.info(f"[graph_store] {len(export_paths)} tile(s): "
              f"{n_nodes} nodes, {len(edge_pairs)} edges loaded")

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
        fixture's bbox but nowhere near a real sidewalk, so the second check
        also requires a real edge within MAX_SNAP_DISTANCE_M. Nearest-EDGE
        distance is a strictly more permissive (and more accurate) signal
        than the old nearest-NODE distance — it can only be smaller, never
        larger, so this never newly rejects a point that used to pass.

        The bbox is padded by MAX_SNAP_DISTANCE_M so the two checks agree
        with each other: the snap check accepts a point that far past the
        outermost street, and a raw node-min/max box would wrongly reject
        clicks in that margin before the snap check ever ran.
        """
        pad_lat = config.MAX_SNAP_DISTANCE_M / METERS_PER_DEGREE_LAT
        pad_lon = pad_lat / self._lat_scale
        lon_min, lat_min, lon_max, lat_max = self._bounds
        if not (lon_min - pad_lon <= lon <= lon_max + pad_lon
                and lat_min - pad_lat <= lat <= lat_max + pad_lat):
            return False
        _, dist_m = self._nearest_edge(lat, lon)
        return dist_m <= config.MAX_SNAP_DISTANCE_M

    def _tree_fraction(self, month: int) -> np.ndarray:
        """Tree-covered fraction of every edge, 0-1: month-adjusted density
        over DENSITY_AT_FULL_COVERAGE, capped at 1. The pre-2026-09 shade
        model, unchanged -- building shade joins it in _edge_density."""
        canopy = config.CANOPY_BY_MONTH[month - 1]  # month is 1-12; lists index from 0
        tree_score = self._tree_evergreen + self._tree_deciduous * canopy
        return np.minimum(tree_score / self._length / config.DENSITY_AT_FULL_COVERAGE, 1.0)

    def _building_fraction(self, month: int, day: int, hour: int, minute: int) -> np.ndarray:
        """Building-shaded fraction of every edge, 0-1, at a moment.

        The table holds one row per (month, hour): the sun on the 15th of
        the month, on the hour (pipeline/sun.py; meta.shade_slots). Between
        rows the value is a weighted average, so nothing jumps at the hour
        or on the 1st: by the minute between hour h and h+1 (2:45 = 25% of
        2:00 + 75% of 3:00; 23 wraps to 0, both night), and by the day
        between the two nearest 15ths (July 25 = 1/3 July + 2/3 August;
        Dec 16-Jan 14 blend Dec with Jan). Day 15 at minute 0 is exactly
        the row. Month lengths are the anchor year's (config.SUN_ANCHOR_YEAR)
        so the weights are the same every year; a Feb 29 request lands
        half-way to March, which is where it belongs.

        A DARK slot counts as full shade (PLAN `night-shade`, 2026-09-26).
        The build computes daylight slots only, so a dark slot's stored
        row is 0 -- "not computed", not "no shade". Read literally, it made
        the last hour before dark blend TOWARD zero: on the pilot fixture,
        July 15's length-weighted mean went 0.784 at 20:00 -> 0.523 at
        20:20 -> 0.013 at 20:59 while the sun set. With no sun there is
        nothing to stand in, so dusk now climbs to 1.0 and dawn falls from
        it, and once every slot the moment blends is dark (is_night) every
        edge is 1.0. The rows stay what the build wrote: they are building
        shadows, and night is not one.
        """
        rows = self._building_shade
        if self.is_night(month, day, hour, minute):
            # Exactly 1.0: the weighted sum below can land a float's width
            # short of it, and night's promise is "every edge, fully".
            return np.ones(rows.shape[1], dtype=np.float32)
        blended = np.zeros(rows.shape[1], dtype=np.float32)
        for slot, weight in _blend_weights(month, day, hour, minute):
            if weight == 0.0:
                continue
            if self._dark_slots[slot]:
                blended += weight * 255.0
            else:
                blended += weight * rows[slot].astype(np.float32)
        return blended / 255.0

    def is_night(self, month: int, day: int, hour: int, minute: int = 0) -> bool:
        """True when every slot this moment blends is dark (zero-weight
        slots aside). Then _building_fraction is 1.0 on every edge, every
        edge is fully shaded, and every tree_weight prices an edge by its
        length alone -- /route's night branch rests on exactly that. In
        July (dark slots 21-23 and 0-5 in the real table): 20:59 is not
        night, since 1/60 of 20:00 still counts; 21:00 is, and so is
        05:00; 05:01 is not. Always False on an export without a sun
        table."""
        for slot, weight in _blend_weights(month, day, hour, minute):
            if weight > 0.0 and not self._dark_slots[slot]:
                return False
        return True

    def _edge_density(self, month: int, day: int = SHADE_ANCHOR_DAY,
                      hour: int | None = None, minute: int = 0,
                      layers: str = "both") -> np.ndarray:
        """SHADE density per edge (score per meter, saturating at
        config.DENSITY_AT_FULL_COVERAGE), vectorized over every edge --
        trees and building shadows combined by union: covered = 1 - (1 -
        trees)(1 - buildings), returned as RATE x covered so everything
        downstream (edge_costs, shade_fraction) keeps its tree-era units.
        `hour=None` means no time was given and building shade is left
        out -- exactly the trees-only behaviour every pre-shadow test and
        harness run pins with `month=7` alone. `layers` is the switch a
        layer selector would use: "trees", "buildings" or "both" (default);
        it is validated by /route, not here. Cached per (month, day, hour,
        minute, layers), a handful of entries.

        The tree half's history, kept in full because the cap decision was
        measured and the numbers are worth re-reading:

        Month-adjusted CANOPY COVERAGE density (score per meter,
        saturating at config.DENSITY_AT_FULL_COVERAGE), vectorized over
        every edge. Shared by edge_costs() and route()'s shade_fraction,
        so the router optimizes exactly the physical quantity the user
        is shown: density/DENSITY_AT_FULL_COVERAGE is an edge's real
        covered fraction, and above full coverage there is nothing more
        to buy.

        WHY THE CAP, AND WHY AT THE EXCHANGE RATE AND NOT 0.02 (both
        measured, 2026-08-25/26; the rate itself was 0.031 then and was
        re-fit to 0.033 on 2026-08-27 when tree attachment widened to 8m
        -- see DENSITY_AT_FULL_COVERAGE's comment): uncapped, the router
        paid for score past full
        coverage -- trunk inventory, not shade (at constant >=90%
        ground-truth cover, scores span 0.00-0.058, and a blind Street
        View test could not tell 5-6.7x score gaps apart at matched
        coverage). Fed the park-canopy data, that phantom credit cost
        real shade: 22/188 random pairs reported less shade than the
        prior export, 20 of them walking more metres in the sun (worst
        +520m). But capping at the DISPLAY constant (then 0.02) was
        falsified even harder: 120/188 routes lost real shade, because
        0.02 is only ~65% coverage and the 0.02-0.031 band is the
        genuine 65%->100% difference. The cap sits where coverage
        physically saturates, per the exchange rate in the constant's
        own comment. Two fully-covered paths now cost the same and the
        tie resolves by length -- measured residual: 8/188 pairs show
        sub-1.5-point display dips between near-tied routes, which
        clamp_shade_monotonic absorbs.

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
        if layers not in SHADE_LAYERS:
            raise ValueError(f"layers must be one of {SHADE_LAYERS}, got {layers!r}")
        cache_key = (month, day, hour, minute, layers)
        cached = self._density_cache.get(cache_key)
        if cached is not None:
            return cached
        covered = np.zeros(len(self._length), dtype=np.float32)
        if layers in ("trees", "both"):
            covered = self._tree_fraction(month)
        if layers in ("buildings", "both") and hour is not None and self._building_shade.shape[1]:
            buildings = self._building_fraction(month, day, hour, minute)
            covered = 1.0 - (1.0 - covered) * (1.0 - buildings)
        density = (covered * config.DENSITY_AT_FULL_COVERAGE).astype(np.float32)
        if len(self._density_cache) >= DENSITY_CACHE_SIZE:
            self._density_cache.pop(next(iter(self._density_cache)))
        self._density_cache[cache_key] = density
        return density

    def edge_costs(self, tree_weight: float, month: int, day: int = SHADE_ANCHOR_DAY,
                   hour: int | None = None, minute: int = 0, layers: str = "both") -> np.ndarray:
        """The plan's cost formula, vectorized over every edge."""
        density = self._edge_density(month, day, hour, minute, layers)
        return self._length / (1.0 + tree_weight * density)

    def route(self, start: SnapPoint, end: SnapPoint, tree_weight: float, month: int,
              day: int = SHADE_ANCHOR_DAY, hour: int | None = None, minute: int = 0,
              layers: str = "both") -> dict | None:
        """Cheapest path between two snapped points. None if unreachable.

        A SnapPoint sits partway along an edge, not on a real graph node,
        so Dijkstra can't start there directly. The search starts at the
        start edge's NEARER endpoint, and the far endpoint is folded in by
        re-pricing that one edge in this request's own cost array (the
        comment at the fold below says why that is exact); the end edge's
        two endpoints are both covered because get_shortest_paths(v,
        to=[...]) finds the cheapest path from one source to every listed
        target in a single run (inherent to Dijkstra, not a batching
        trick). Each candidate total adds the cost of walking the partial
        start/end edge to its endpoint, and the cheapest wins. So each
        weight costs exactly ONE Dijkstra run: it was two (one per start
        endpoint) until 2026-09-09, and four before the one-to-many
        targets. This is read-only on the graph — no mutation — because
        /route is a sync FastAPI handler that Starlette runs across a
        thread pool, and mutating the one shared igraph.Graph per request
        would need locking that serializes every routing request.

        Run count is what matters at citywide scale: each run pays ~9ms
        of fixed weight handling (the memoryview note below) plus
        exploration that grows with route length (re-profiled 2026-09-07
        on the citywide graph, dev Mac: ~10ms/run for a ~1km route,
        ~115ms/run at ~20km; per-route-length numbers in
        pipeline/config.py's cap note).

        When start and end land on the same edge, also try cutting
        straight between them along it — otherwise two nearby clicks on
        the same block would be forced through a corner and back for no
        reason. It's compared by cost like everything else, not assumed
        to win, since a leafy detour via a real corner can still cost less
        at a high tree_weight.
        """
        costs = self.edge_costs(tree_weight, month, day, hour, minute, layers)
        # Continuous per-edge shade credit (FIXES item 2): an edge
        # contributes min(density / DENSITY_AT_FULL_COVERAGE, 1) of its
        # length to shade_fraction, replacing the old shaded-or-not
        # threshold whose cliff-edge let near-identical routes read 0%
        # vs 100% -- see the constant's comment for the calibration.
        #
        # The fail-closed branch here (all-zero while the constant was None)
        # was removed on 2026-08-24 when the constant got a measured
        # sidewalk-era value of 0.02. Since the 2026-08-26 unification,
        # saturation applies to BOTH sides: _edge_density() caps at
        # DENSITY_AT_FULL_COVERAGE, so cost and display saturate together
        # and two fully-covered blocks tie on shade, resolving by length --
        # see _edge_density's docstring for why the earlier split-scale
        # design (unsaturated cost, saturated display) was falsified.
        shade_credit = np.minimum(
            self._edge_density(month, day, hour, minute, layers) / config.DENSITY_AT_FULL_COVERAGE, 1.0
        )

        best_plan = self._best_plan(start, end, costs)

        if best_plan is None:
            # A legitimate outcome now, not a bug: load() keeps every
            # component (see its own comment), so two points in genuinely
            # disconnected parts of NYC -- mainland and Governors Island,
            # eventually mainland and Staten Island -- hit this and get a
            # clean "no route" here rather than an error.
            return None

        legs: list[dict] = []  # raw per-edge legs; build_steps folds them

        def _leg(edge_idx: int, leg_length_m: float, leg_coords) -> None:
            legs.append({"name": self._names[edge_idx],
                         "side": self._sides[edge_idx],
                         "kind": self._kinds[edge_idx],
                         "fold_names": self._fold_names[edge_idx],
                         "length_m": float(leg_length_m),
                         "coords": [list(point) for point in leg_coords]})

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
            _leg(start.edge, direct_dist_m, coords)
        else:
            _, s_node, s_dist_m, e_node, e_dist_m, edge_path = best_plan

            coords = self._edge_substring(start.edge, start.point, self._node_lonlat[s_node].tolist())
            _leg(start.edge, s_dist_m, coords)

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
                _leg(e, float(self._length[e]), step.tolist())

            lead_out = self._edge_substring(end.edge, self._node_lonlat[e_node].tolist(), end.point)
            coords.extend(lead_out[1:])
            _leg(end.edge, e_dist_m, lead_out)

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
            # continuous redesign, see DENSITY_AT_FULL_COVERAGE's comment.)
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
            "segments": build_steps(legs),
        }

    def _best_plan(self, start: SnapPoint, end: SnapPoint, costs: np.ndarray) -> tuple | None:
        """The cheapest way to connect two snap points under `costs`, as
        a plan route() stitches: ("via_nodes", s_node, s_dist_m, e_node,
        e_dist_m, edge_path) -- enter the network at s_node, walk
        edge_path, leave it at e_node -- or ("direct", dist_m) along a
        shared edge, or None when nothing connects. Its own method so the
        one-run fold below can be checked against a two-run reference in
        isolation (tests/test_route_search_fold.py). `costs` is this
        request's own array: the fold re-prices one entry during the
        search and restores it before returning.
        """
        end_options = [(end.node_u, end.dist_to_u_m), (end.node_v, end.dist_to_v_m)]
        end_nodes = [e_node for e_node, _ in end_options]

        # Fold the two start endpoints into ONE run. The search begins at
        # the endpoint with the smaller lead-in (`near`), and the start
        # edge is priced, in this request's own cost array, at
        # far_cost - near_cost (>= 0). Dijkstra from near then settles
        # every node x at min(d_near(x), (far_cost - near_cost) +
        # d_far(x)); add near_cost and that is min(near_cost + d_near(x),
        # far_cost + d_far(x)) -- exactly the cheaper of the two runs this
        # used to make. The re-priced edge touches the source, so a
        # shortest path can only ever use it as its FIRST hop; no other
        # path's cost changes. Verified identical to the two-run search
        # (cost and path) on 5 hand-picked pairs x 4 weights and 44 seeded
        # random pairs, 2026-09-09. The array is route()'s own, built fresh
        # by edge_costs() per call, so nothing shared moves and this stays
        # lock-free.
        start_edge_cost = float(costs[start.edge])
        s_u_cost = start.dist_to_u_m / self._length[start.edge] * start_edge_cost
        s_v_cost = start.dist_to_v_m / self._length[start.edge] * start_edge_cost
        if s_u_cost <= s_v_cost:
            near_node, near_dist_m, near_cost = start.node_u, start.dist_to_u_m, s_u_cost
            far_node, far_dist_m, far_cost = start.node_v, start.dist_to_v_m, s_v_cost
        else:
            near_node, near_dist_m, near_cost = start.node_v, start.dist_to_v_m, s_v_cost
            far_node, far_dist_m, far_cost = start.node_u, start.dist_to_u_m, s_u_cost
        # A self-loop edge (u == v) has one endpoint to start from: nothing
        # to fold, and its re-pricing would sit on a loop no shortest path
        # walks. The cheaper lead-in still applies.
        if near_node != far_node:
            costs[start.edge] = far_cost - near_cost

        # Hand igraph the raw float64 buffer, not the ndarray: converting
        # the ndarray inside get_shortest_paths cost ~26ms per call on this
        # graph (Mac, 2026-09-07) -- more than a short route's entire
        # search -- vs ~9ms via memoryview. Same doubles either way; epaths
        # verified identical across every pair/weight/endpoint combination
        # and the 800-pair seeded harness (routing_harness.py A/B).
        costs_view = memoryview(costs)
        # output="epath" → the path as a list of edge positions, which
        # is what we need to sum attributes and stitch geometry. One
        # call covers both end_nodes (see this function's docstring).
        # igraph's C layer warns here ("Couldn't reach some vertices")
        # whenever near_node and a given e_node sit in different
        # components -- snap_pair() keeps the real /route flow from
        # ever reaching this with such a pair, so in practice this is
        # now only a defense-in-depth path (see the module-scope
        # filter comment above for why it's silenced there rather
        # than with a per-call warnings.catch_warnings(), which isn't
        # thread-safe and /route runs across Starlette's thread pool).
        edge_paths = self._graph.get_shortest_paths(
            near_node, to=end_nodes, weights=costs_view, output="epath"
        )
        costs[start.edge] = start_edge_cost  # the sums below want the real price

        best_cost: float | None = None
        best_plan: tuple | None = None
        for (e_node, e_dist_m), edge_path in zip(end_options, edge_paths):
            e_cost = e_dist_m / self._length[end.edge] * costs[end.edge]
            # A path that begins by walking the start edge entered the
            # network at the far endpoint: the lead-in runs from the snap
            # point straight to `far`, and the edge is not walked end to
            # end -- so it leaves the path, and the plan reads exactly as
            # the old far-endpoint run would have written it.
            if edge_path and edge_path[0] == start.edge:
                s_node, s_dist_m, s_cost = far_node, far_dist_m, far_cost
                edge_path = edge_path[1:]
            else:
                s_node, s_dist_m, s_cost = near_node, near_dist_m, near_cost
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

        return best_plan
