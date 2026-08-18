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

# The hide rule (FIXES item 1, the scraps arc's final step, 2026-08-17):
# a disconnected component whose total edge length is under this bar is
# excluded from click-snapping AND the drawn coverage boundary. The 2026-
# 08-15/16 citywide cause audit classified every such component (7,645
# post-weld): sidewalk orphans, policy-excluded connectors, cross-tile
# ghost slivers, imported orphans, fence-blocked and golf/island meshes --
# none reachable from the street network, so the only thing snapping onto
# one can produce is a route trapped inside a sub-5km fragment, or a 422
# in seemingly-covered area. Hiding is the industry treatment (OSRM
# deletes small components outright; we keep the data, just stop
# advertising it). The bar is the audit's own "real network" threshold:
# Staten Island (3,015km) and Governors Island (49km) clear it easily.
# Two safety properties: the LARGEST component is always kept whatever
# its length (so toy datasets and sliver tiles keep working), and the
# rule is size-based, not a blacklist -- any fragment a future batch
# genuinely connects becomes visible again automatically.
HIDDEN_COMPONENT_MAX_LEN_M = 5000.0

# Curated exceptions: isolated-but-real public places that stay clickable
# despite being under the bar, because routing WITHIN them is genuinely
# useful. Coordinate-keyed, not node-keyed -- node ids renumber every
# refetch, coordinates don't move. Each point sits ON the component it
# vouches for; load() keeps that point's whole component visible. Both
# entries come from the user's 21-case review of every >=2km fragment
# (2026-08-16/17, data/audits/2026-08-16/hide_rule_over2km_review.md):
# 19 of 21 were confirmed hide (golf meshes, airport enclosures, a gated
# cemetery, ghost slivers), these two were confirmed real.
KEEP_VISIBLE_ISOLATED_PLACES: list[tuple[float, float, str]] = [
    (40.690830, -74.045350, "Liberty Island"),
    (40.912055, -73.907690, "College of Mount Saint Vincent"),
]


def _tiles_fingerprint(tile_paths: list) -> str:
    """A cheap fingerprint of every loaded tile's identity (name, size,
    mtime) — changes whenever a tile is added, removed, or re-exported,
    which is exactly when the coverage cache (above) needs recomputing
    rather than reused.

    The hide rule's parameters are part of the fingerprint too: the
    drawn coverage now depends on WHICH components are visible, so a
    threshold change or a keep-list edit must invalidate the cache the
    same way a re-exported tile does — without this, editing the rule
    silently serves rings computed under the old rule (the exact trap
    FIXES item 1 warned about)."""
    parts = sorted(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in tile_paths)
    rule = f"hide<{HIDDEN_COMPONENT_MAX_LEN_M}|keep:{sorted(KEEP_VISIBLE_ISOLATED_PLACES)}"
    return hashlib.sha256(("\n".join(parts) + "\n" + rule).encode()).hexdigest()


def _load_cached_coverage_rings(cache_path, fingerprint: str) -> list[list[list[float]]] | None:
    """The on-disk coverage cache, if its fingerprint matches the tiles
    being loaded right now — None on any mismatch, missing file, or
    corrupt cache, all treated the same way (recompute), since this is
    strictly a speed optimization with no correctness dependency on it."""
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("fingerprint") != fingerprint:
        return None
    return cached.get("rings")


def _save_cached_coverage_rings(cache_path, fingerprint: str, rings: list[list[list[float]]]) -> None:
    cache_path.write_text(json.dumps({"fingerprint": fingerprint, "rings": rings}))


# Manually verified real-world OSM node-id pairs that are the same
# physical corner but got recorded as two different nodes -- a genuine
# digitization gap in the source data, not a filter/tagging issue (see
# PLAN.md's Rockaway/Cross Bay Bridge finding, 2026-07-18). Each entry is
# a specific, individually-reviewed correction, never a general "connect
# anything within N meters" rule: a citywide check for other close-but-
# disconnected node pairs turned up 419 more within 30m, and every one
# checked was an unnamed cemetery/park-style interior path network --
# exactly the kind of deliberately-separate fragment snap_pair() already
# protects from being reconnected to the street grid. Bridging those the
# same way this pair is bridged would undo that protection, so this
# stays a short, explicit list rather than an algorithm.
#
# The 2026-07-19 batch below has a different root cause: not a
# digitization gap, but pipeline/fetch/streets.py's per-tile Overpass
# fetch truncating a long way (a greenway, esplanade, or pedestrian
# bridge) at a different real OSM vertex in each of two adjacent tiles,
# when that way's vertices happen to be spaced further apart than
# FETCH_BUFFER_M's overlap -- see HISTORY.md's compose-before-simplify
# sweep entry for the full mechanism. Tried a bigger FETCH_BUFFER_M and
# osmnx's truncate_by_edge=True as general, root-cause fixes first;
# tested against real data via the actual fetch/clip/simplify/export
# pipeline, neither reliably closed the gap even on the one case fully
# verified, so per-case bridging is the deployed fix, not a stopgap
# ahead of a "real" one. Every entry below is confirmed via a direct
# Overpass query on the underlying way's tags, not picked by distance
# alone -- the same sweep flagged Roosevelt Island Bridge too, and that
# one was checked and rejected this way: its "gap" is real distance
# between two different real things (a foot=no roadway and a
# separately-mapped sidewalk that doesn't touch these nodes), not a
# path split in two.
_CURATED_KNOWN_NODE_GAPS: list[tuple[str, str, str]] = [
    # Cross Bay Bridge's shared foot+bike path (its Rockaway-side
    # landing) <-> East 21st Road, Broad Channel/Rockaway -- ~12m apart
    # in OSM's own data, confirmed via a direct Overpass query: the same
    # real corner, recorded as two different node ids.
    ("608478726", "42938246", "Cross Bay Bridge"),

    # Pulaski Bridge's own footway <-> nearby footway=crossing/sidewalk
    # infrastructure at its landing -- 1-12m apart, the same small-scale
    # digitization-gap pattern as the entry above, not a filter
    # exclusion: PLAN.md's original 2026-07-18 finding ("only connects
    # via excluded footway=sidewalk, not easily fixable") pre-dates
    # footway=crossing being un-excluded from WALK_FILTER and didn't
    # have this data to check against. Confirmed via direct Overpass
    # query: every way touching this area is genuinely walkable
    # (footway=sidewalk/crossing with marked/signaled crossings, a
    # highway=path, Pulaski Bridge's own footway) -- nothing tagged
    # foot=no or vehicle-only, unlike Roosevelt Island Bridge above.
    ("4384787164", "9785884677", "Pulaski Bridge"),
    ("739651503", "11622964702", "Pulaski Bridge"),
    ("9690694933", "11211160285", "Pulaski Bridge"),

    # Ed Koch Queensboro Bridge Outer Roadway -- found 2026-08-15 by the
    # 100-route external batch (5/5 flagged routes were this one gap; the
    # path was a 3-node + 2-node island pair, forcing every midtown<->LIC
    # walk 4km north over the RFK). Three joints, each verified by
    # way-membership (no deck-to-ground pair; the one deck-vs-ground
    # candidate 24.7m mid-span was correctly REJECTED) + OSRM advisory
    # (4m/28m/9m walks) before shipping, per FIXES item 1's rule:
    ("7792410664", "13892069996", "Queensboro Bridge Outer Roadway"),   # Manhattan entrance, 4.8m
    ("3785648023", "2089938144", "Queensboro Bridge Outer Roadway"),    # anchorage ramp joint, 28.8m
    ("8315072991", "11520108686", "Queensboro Bridge Outer Roadway"),   # Crescent St touchdown, 17.4m

    # Tile-boundary truncation gaps -- see the comment above.
    #
    # The 2026-08-15 post-v19 dead-entry audit
    # (data/audits/2026-08-15/curated_gap_verdicts.py) retired ten
    # entries here whose gaps the v19 data now walks directly (walk within
    # ~1.1x of straight-line at the same coords -- the cycleway widening
    # made the greenways themselves routable), and re-derived two whose
    # gap is still real but whose node id fell out of the v19 export
    # (both ids verified alive in OSM; simplification absorbed them):
    ("9191842218", "9191842217", "Manhattan Bridge Pedestrian Path"),
    # Re-derived 2026-08-15: was 11638917883, v19 node 1.7m away.
    ("3564754694", "8279851182", "Manhattan Bridge Pedestrian Path"),
    ("12644027075", "12152905164", "Hudson River Park Esplanade"),
    ("8729985306", "12198069447", "Bronx River Greenway"),
    ("1024175662", "3616599502", "Mosholu-Pelham Greenway"),
    ("387181476", "387181479", "East River Esplanade"),
    ("7782217038", "6304586882", "East River Esplanade"),
    ("348444405", "2350521367", "Harlem River Pathway"),
    ("466530316", "2356694584", "Flatbush Avenue Greenway"),
    ("401828152", "401828132", "Harlem River Drive Greenway"),
    ("1100356499", "8151268693", "Putnam Greenway"),
    ("2346900217", "2346900228", "Pugsley Creek Greenway"),
    # Re-derived 2026-08-15: was 608491459, v19 node 1.3m away; the gap
    # still forces a 7.8km detour on the Jamaica Bay Greenway without it.
    ("12472019883", "6382627345", "Jamaica Bay Greenway"),
    ("42830977", "608478724", "Cross Bay Bridge"),

    # Marine Parkway (Gil Hodges) Bridge -- found 2026-08-16 by the
    # post-v20 250-pair external batch (lead 2: Breezy Point <-> Coney
    # Island read 34.6km vs OSRM 12.6km, a 21km detour around Jamaica
    # Bay). The bridge walkway exists in our data as two overlapping
    # greenway strands that never share a node: "Flatbush Avenue
    # Greenway" (bridge deck, dead-ending at 40.578765,-73.888378) and
    # "Beach Channel Drive Greenway" (Riis-side approach, dead-ending
    # 320m up the deck at 40.580979,-73.890817) -- OSRM transitions
    # between the same two ways at exactly our terminus point, so OSM
    # connects them and our fetch lost the junction. Bridged at the
    # closest cross-strand node pair, 99m apart ALONG the shared bridge
    # approach (both nodes on the same structure -- no deck-to-ground
    # risk). Verified: the entry cuts the probe route to 14.5km, ratio
    # 1.15 vs OSRM, under the batch flag bar.
    ("466530483", "466530490", "Marine Parkway Bridge"),
]


def _load_known_node_gaps(path: Path) -> list[tuple[str, str, str, float | None]]:
    """Load the bulk-verified batch from its own committed JSON file.
    Two entry shapes coexist:

    [node_a, node_b, name]            -- the 2026-08-01 four-signal batch;
                                         all gaps <= 25m, so the bridge's
                                         length is the chord (close enough
                                         at that scale).
    [node_a, node_b, name, length_m]  -- the 2026-08-13 OSRM-verified
                                         dead-end-seam batch; gaps run up
                                         to 100m, where a straight chord
                                         understates the real walk, so the
                                         length OSRM actually measured is
                                         stored explicitly.

    Kept separate from _CURATED_KNOWN_NODE_GAPS above: at ~14k entries
    this can't stay a Python list literal the way the curated batch does,
    and unlike the curated batch it has no individual per-entry story
    worth a comment."""
    out: list[tuple[str, str, str, float | None]] = []
    for entry in json.loads(path.read_text()):
        if len(entry) == 3:
            node_a, node_b, name = entry
            out.append((node_a, node_b, name, None))
        else:
            node_a, node_b, name, length_m = entry
            out.append((node_a, node_b, name, float(length_m)))
    return out


KNOWN_NODE_GAPS: list[tuple[str, str, str, float | None]] = [
    (node_a, node_b, name, None) for node_a, node_b, name in _CURATED_KNOWN_NODE_GAPS
] + _load_known_node_gaps(Path(__file__).parent / "known_node_gaps.json")


def _load_phantom_connectors(path: Path) -> list[tuple[list[float], list[float], str]]:
    """The inverse of KNOWN_NODE_GAPS: edges the imported-path layers
    created that provably do NOT exist as walks in the real world --
    connectors that jump a vertical boundary (a street node snapped onto
    a bridge-deck path 30m overhead, with no stairs anywhere near). Each
    entry was confirmed by the 2026-08-13 reverse-OSRM audit: OSRM either
    can't walk between the edge's endpoints at all or needs hundreds of
    meters where our edge claims a few. See FIXES.md item 0.

    Entries are [[lon_a, lat_a], [lon_b, lat_b], note] -- endpoint
    COORDINATES, not node ids, deliberately: the phantom edges' synthetic
    node ids renumber on any re-export, and an id-keyed blocklist would
    go silently stale (the vacuous-fixture failure mode all over again).
    Coordinates come from the same source data, so they survive
    re-exports; matching is by proximity (~2m) at load time."""
    return [(a, b, note) for a, b, note in json.loads(path.read_text())]


PHANTOM_CONNECTORS: list[tuple[list[float], list[float], str]] = _load_phantom_connectors(
    Path(__file__).parent / "phantom_connectors.json"
)


def _is_phantom_connector(lon_u: float, lat_u: float, lon_v: float, lat_v: float) -> bool:
    """Does this edge's endpoint pair match a PHANTOM_CONNECTORS entry
    (either orientation, ~2m tolerance per endpoint)?"""
    for (lon_a, lat_a), (lon_b, lat_b), _note in PHANTOM_CONNECTORS:
        # cheap prefilter before the real distance math
        if abs(lat_u - lat_a) > 0.0001 and abs(lat_u - lat_b) > 0.0001:
            continue
        if (_local_distance_m(lat_u, lon_u, lat_a, lon_a) <= 2.0
                and _local_distance_m(lat_v, lon_v, lat_b, lon_b) <= 2.0):
            return True
        if (_local_distance_m(lat_u, lon_u, lat_b, lon_b) <= 2.0
                and _local_distance_m(lat_v, lon_v, lat_a, lon_a) <= 2.0):
            return True
    return False


def _local_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance between two nearby points -- fine at the
    few-meters-to-tens-of-meters scale KNOWN_NODE_GAPS entries are at
    (same approximation pipeline/config.py's buffered_bbox() already
    uses for short local distances, just inverted)."""
    mean_lat = (lat1 + lat2) / 2
    dlat_m = (lat2 - lat1) * METERS_PER_DEGREE_LAT
    dlon_m = (lon2 - lon1) * METERS_PER_DEGREE_LAT * math.cos(math.radians(mean_lat))
    return math.hypot(dlat_m, dlon_m)


# How close a synthetic node must sit to ANOTHER tile's synthetic-path line
# to count as provably the same physical path (see
# _cross_tile_synthetic_stitches). Deliberately tight: duplicate copies come
# from the same citywide source dataset, so where they overlap they coincide
# essentially exactly (6dp export rounding = ~0.11m) -- measured citywide,
# flagged near-coincident pairs split cleanly into <=1.5m (all duplicates)
# and >5m (genuinely separate paths, possibly fenced apart); nothing
# ambiguous survives this cutoff, and paths 2m+ apart are never joined.
STITCH_ON_LINE_TOLERANCE_M = 1.5

# Longest bridge a stitch may add, node to nearest endpoint of the twin
# path. Measured over every real stitchable pair citywide: median 1.1m,
# p90 11.3m, max 24.3m -- so 25m loses nothing real, while capping how far
# a straight bridge segment can deviate from a curvy path it shortcuts.
STITCH_MAX_HOP_M = 25.0


def _cross_tile_synthetic_stitches(
    id_to_idx: dict[str, int],
    node_lonlat: list[list[float]],
    edge_pairs: list[tuple[int, int]],
    coords_per_edge: list[list[list[float]]],
) -> list[tuple[int, int]]:
    """Node-index pairs to bridge so cross-tile duplicate copies of the same
    synthetic path become walkable as one.

    Why duplicates exist at all: every tile fetches FETCH_BUFFER_M past its
    own edges (so neighbors overlap and real OSM border edges merge via
    their shared, globally-unique node ids), and both neighbors build their
    own copy of any synthetic path (interior sidewalks, park trails) in the
    overlap band. Synthetic ids are minted per tile (namespaced
    "r16c12:-1", see pipeline/export.py), so the copies CAN'T share ids the
    way real border edges do -- they load as two coincident, disconnected
    paths, and a walker standing on one "needs" a multi-hundred-meter
    detour to reach the other, i.e. to reach where they already are.

    The stitch rule: a synthetic node that lies ON a different tile's
    synthetic-path line (within STITCH_ON_LINE_TOLERANCE_M) is provably a
    point on the same physical path, so it gets a short bridge edge to that
    line's nearest endpoint node (capped at STITCH_MAX_HOP_M). Same bridge
    mechanics as KNOWN_NODE_GAPS above.

    What this deliberately does NOT do:
    - Same-tile pairs are never stitched -- within one tile the pipeline
      already decided what connects (with barrier checks this load-time
      pass can't replicate); its output isn't second-guessed here.
    - Nearby-but-off-the-line pairs (>1.5m) are never stitched -- two
      separate paths a few meters apart can have a real fence between
      them; only exact coincidence is treated as identity.
    - Copies aren't merged or deduplicated, just connected -- both stay
      drawn, routing simply stops paying a phantom detour between them.
    """
    synth_tile = {
        idx: node_id.split(":", 1)[0] for node_id, idx in id_to_idx.items() if ":" in node_id
    }
    if not synth_tile:
        return []

    candidate_edges = [
        i for i, (u, v) in enumerate(edge_pairs) if u in synth_tile or v in synth_tile
    ]
    if not candidate_edges:
        return []

    lonlat = np.asarray(node_lonlat)
    mean_lat = float(np.mean(lonlat[:, 1]))
    lat_scale = math.cos(math.radians(mean_lat))
    scale = np.array([lat_scale, 1.0])

    # Batched LineStrings in the same cos-scaled space _build_edge_index
    # uses, for the same reason (see its docstring).
    points_per_edge = [len(coords_per_edge[i]) for i in candidate_edges]
    stacked = np.concatenate(
        [np.asarray(coords_per_edge[i], dtype=np.float64) for i in candidate_edges]
    )
    lines = shapely.linestrings(
        stacked * scale, indices=np.repeat(np.arange(len(candidate_edges)), points_per_edge)
    )
    tree = STRtree(lines)

    synth_idxs = list(synth_tile)
    points = shapely.points(lonlat[synth_idxs] * scale)
    radius_deg = STITCH_ON_LINE_TOLERANCE_M / METERS_PER_DEGREE_LAT
    hits = tree.query(points, predicate="dwithin", distance=radius_deg)

    # Nearest qualifying twin line per node -- a node's own tile's lines
    # (including its own incident edges, at distance 0) never qualify.
    best_for_node: dict[int, tuple[float, int]] = {}
    for point_i, line_j in zip(hits[0], hits[1]):
        node_idx = synth_idxs[point_i]
        u, v = edge_pairs[candidate_edges[line_j]]
        edge_tile = synth_tile.get(u) or synth_tile.get(v)
        if edge_tile == synth_tile[node_idx]:
            continue
        dist_deg = float(lines[line_j].distance(points[point_i]))
        current = best_for_node.get(node_idx)
        if current is None or dist_deg < current[0]:
            best_for_node[node_idx] = (dist_deg, candidate_edges[line_j])

    already_connected = {(min(u, v), max(u, v)) for u, v in edge_pairs}
    stitches: list[tuple[int, int]] = []
    for node_idx, (_, edge_i) in best_for_node.items():
        u, v = edge_pairs[edge_i]
        lon_n, lat_n = lonlat[node_idx]
        hop_u = _local_distance_m(lat_n, lon_n, lonlat[u][1], lonlat[u][0])
        hop_v = _local_distance_m(lat_n, lon_n, lonlat[v][1], lonlat[v][0])
        endpoint, hop_m = (u, hop_u) if hop_u <= hop_v else (v, hop_v)
        if endpoint == node_idx or hop_m > STITCH_MAX_HOP_M:
            continue
        pair = (min(node_idx, endpoint), max(node_idx, endpoint))
        if pair in already_connected:
            continue
        already_connected.add(pair)
        stitches.append(pair)
    return stitches


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
        self._edge_lines_scaled: np.ndarray | None = None  # see _build_edge_index
        self._strtree: STRtree | None = None
        self._lat_scale = 1.0  # see _build_edge_index
        self._bounds: tuple[float, float, float, float] | None = None  # lon_min, lat_min, lon_max, lat_max

        # Edge attribute arrays, all aligned by edge position.
        self._length = np.empty(0, dtype=np.float32)
        # Which connected component each edge belongs to -- see
        # snap_pair() for why this is tracked at all: every component is
        # kept (below), including small disconnected fragments that
        # shouldn't ever capture a click meant for the real street grid.
        self._edge_component = np.empty(0, dtype=np.int32)
        # The hide rule's outputs (see HIDDEN_COMPONENT_MAX_LEN_M): which
        # edges are visible to snapping/coverage, and the mapping from the
        # snap STRtree's positions (built over visible edges only) back to
        # real edge indices. Routing arrays stay indexed by real edge ids;
        # hidden edges simply can never be snapped onto.
        self._visible_edge_mask = np.empty(0, dtype=bool)
        self._strtree_edge_ids = np.empty(0, dtype=np.int64)
        self._tree_deciduous = np.empty(0, dtype=np.float32)
        self._tree_evergreen = np.empty(0, dtype=np.float32)
        self._tree_count = np.empty(0, dtype=np.int32)
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
        # How many cross-tile duplicate-path stitches load() added. Kept so
        # the merge-fixture precondition test can assert the stitch pass
        # actually exercised (a fixture without cross-tile synthetic data
        # would make the merge-integrity tests pass vacuously -- exactly how
        # the id-collision bug stayed invisible).
        self._stitch_count = 0

    # ── Loading ───────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read every tile in data/tiles/ into one merged graph."""
        tile_paths = sorted(config.TILES_DIR.glob("*.json.gz"))
        if not tile_paths:
            raise FileNotFoundError(
                f"No tiles in {config.TILES_DIR} — run the pipeline first "
                "(uv run python -m pipeline.run_tile pilot)"
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
        phantom_skipped = 0

        for path in tile_paths:
            tile = json.loads(gzip.open(path, "rt").read())

            for node_id, (lon, lat) in tile["nodes"].items():
                if node_id not in self._id_to_idx:
                    self._id_to_idx[node_id] = len(node_lonlat)
                    node_lonlat.append([lon, lat])

            for edge in tile["edges"]:
                # Confirmed-phantom connectors (see PHANTOM_CONNECTORS)
                # never enter the graph. Only short edges can match --
                # every confirmed phantom is a <120m snap connector.
                if edge["length_m"] <= 150.0 and PHANTOM_CONNECTORS:
                    lon_u, lat_u = tile["nodes"][edge["u"]]
                    lon_v, lat_v = tile["nodes"][edge["v"]]
                    if _is_phantom_connector(lon_u, lat_u, lon_v, lat_v):
                        phantom_skipped += 1
                        continue

                # Border edges appear in two neighboring tiles; a canonical
                # (sorted) node pair makes both copies hash identically.
                # Each tile scored its copy against only its own tree
                # fetch, so the copies can disagree -- when they do, keep
                # the better-scored one, not the first-seen one. Both
                # copies count trees in the identical corridor, so a copy
                # can only be MISSING trees its tile's fetch didn't cover,
                # never have extras: higher tree value == closer to
                # complete. (First-seen-wins silently kept the worse copy
                # 3,740 times citywide, including a 1.7km Harlem River
                # Drive Greenway edge held at 0 trees while its other
                # copy had 95.)
                dedupe_key = (*sorted((edge["u"], edge["v"])), edge["key"], edge["side"])
                existing = seen_edges.get(dedupe_key)
                if existing is not None:
                    stored_value = deciduous[existing] + evergreen[existing]
                    if edge["tree_deciduous"] + edge["tree_evergreen"] > stored_value:
                        length[existing] = edge["length_m"]
                        deciduous[existing] = edge["tree_deciduous"]
                        evergreen[existing] = edge["tree_evergreen"]
                        counts[existing] = edge["tree_count"]
                        canopy_credit[existing] = edge.get("tree_park_canopy", 0.0)
                        names[existing] = edge["name"]
                        coords_per_edge[existing] = edge["coords"]
                    continue

                # OSM itself sometimes contains the same way twice --
                # identical geometry between the same two nodes, which
                # osmnx keeps as parallel edges under different multigraph
                # keys (159 confirmed citywide, all within a single tile).
                # Keep one: same endpoints, so dropping the extra copy
                # can't disconnect anything. Hashing the coords (direction-
                # insensitive) instead of storing them keeps this set small;
                # genuinely different parallel edges between the same nodes
                # (a street and a separate path) hash differently and both
                # survive.
                forward = tuple(tuple(point) for point in edge["coords"])
                geometry_key = (*dedupe_key[:2], edge["side"],
                                min(hash(forward), hash(forward[::-1])))
                if geometry_key in seen_geometries:
                    continue
                seen_geometries.add(geometry_key)

                seen_edges[dedupe_key] = len(edge_pairs)
                edge_pairs.append((self._id_to_idx[edge["u"]], self._id_to_idx[edge["v"]]))
                length.append(edge["length_m"])
                deciduous.append(edge["tree_deciduous"])
                evergreen.append(edge["tree_evergreen"])
                counts.append(edge["tree_count"])
                canopy_credit.append(edge.get("tree_park_canopy", 0.0))
                names.append(edge["name"])
                coords_per_edge.append(edge["coords"])

        if phantom_skipped:
            logger.info(f"[graph_store] skipped {phantom_skipped} confirmed phantom connector(s)")

        bridged_count = 0
        dangling: list[tuple[str, str, str]] = []
        for node_a, node_b, gap_name, gap_length_m in KNOWN_NODE_GAPS:
            if node_a not in self._id_to_idx or node_b not in self._id_to_idx:
                # Not in this dataset. Two very different situations share
                # this branch, told apart by proportion below: a PARTIAL
                # dataset (the pilot-only test tile misses ~14k entries --
                # normal, quiet) vs. a citywide load where a refetch's OSM
                # drift orphaned entries (a silently-dead fix; the v18
                # refetch killed 11 hand-curated bridges including
                # "Manhattan Bridge Pedestrian Path" and nothing noticed
                # for a day -- found 2026-08-14).
                dangling.append((node_a, node_b, gap_name))
                continue
            idx_a, idx_b = self._id_to_idx[node_a], self._id_to_idx[node_b]
            lon_a, lat_a = node_lonlat[idx_a]
            lon_b, lat_b = node_lonlat[idx_b]
            edge_pairs.append((idx_a, idx_b))
            # Entries with a measured length (OSRM's actual walk) use it;
            # the rest fall back to the chord, honest at their <=25m scale.
            if gap_length_m is not None:
                length.append(gap_length_m)
            else:
                length.append(_local_distance_m(lat_a, lon_a, lat_b, lon_b))
            deciduous.append(0.0)
            evergreen.append(0.0)
            counts.append(0)
            canopy_credit.append(0.0)
            names.append(gap_name)
            coords_per_edge.append([[lon_a, lat_a], [lon_b, lat_b]])
            bridged_count += 1
        # One line per entry was fine at 28 hand-curated entries; the bulk
        # batch (server/known_node_gaps.json) makes that ~14k lines on every
        # startup instead -- a single count is all a normal boot needs.
        if bridged_count:
            logger.info(f"[graph_store] bridged {bridged_count} known node gap(s)")
        # Mostly-bridged with a few dangling = a citywide dataset where
        # entries went dead (refetch drift) -- say so LOUDLY, per entry.
        # Mostly-dangling = a partial dataset (pilot/CI) -- one quiet line.
        if dangling and bridged_count > len(dangling):
            # logger.warning, and no literal "WARNING" in the text -- the
            # level carries it now (FIXES item 9), and the caplog-based
            # tests assert the LEVEL, which a filtered production handler
            # also acts on.
            logger.warning(f"[graph_store] {len(dangling)} known-gap entr"
                  f"{'y' if len(dangling) == 1 else 'ies'} reference nodes "
                  f"missing from this dataset -- each was a shipped fix that "
                  f"is now silently inactive (OSM drift after a refetch?); "
                  f"re-derive or retire them:")
            for node_a, node_b, gap_name in dangling[:20]:
                logger.warning(f"[graph_store]   dead entry: {node_a} <-> {node_b} ({gap_name!r})")
            if len(dangling) > 20:
                logger.warning(f"[graph_store]   ...and {len(dangling) - 20} more")
        elif dangling:
            logger.info(f"[graph_store] {len(dangling)} known-gap entries not in "
                  f"this dataset (partial dataset, e.g. the pilot tile)")

        # Cross-tile duplicate synthetic paths (see
        # _cross_tile_synthetic_stitches): connect each copy's nodes onto
        # its twin where they provably coincide, with the same bridge shape
        # KNOWN_NODE_GAPS uses. length gets a small floor -- two coincident
        # trim points can sit at the exact same rounded coordinate, and a
        # true zero-length edge would divide by zero in route()'s partial-
        # edge cost math.
        stitches = _cross_tile_synthetic_stitches(
            self._id_to_idx, node_lonlat, edge_pairs, coords_per_edge
        )
        for idx_a, idx_b in stitches:
            lon_a, lat_a = node_lonlat[idx_a]
            lon_b, lat_b = node_lonlat[idx_b]
            edge_pairs.append((idx_a, idx_b))
            length.append(_local_distance_m(lat_a, lon_a, lat_b, lon_b))
            deciduous.append(0.0)
            evergreen.append(0.0)
            counts.append(0)
            canopy_credit.append(0.0)
            names.append("")
            coords_per_edge.append([[lon_a, lat_a], [lon_b, lat_b]])
        self._stitch_count = len(stitches)
        if stitches:
            logger.info(f"[graph_store] stitched {len(stitches)} cross-tile synthetic duplicate(s)")

        self._names = names
        self._node_lonlat = np.array(node_lonlat)
        # The data's actual extent — whatever tiles happen to be loaded —
        # rather than a hardcoded bbox from pipeline/config.py, so this
        # stays correct without a server change once Stage 2 adds more tiles.
        lon_min, lat_min = self._node_lonlat.min(axis=0)
        lon_max, lat_max = self._node_lonlat.max(axis=0)
        self._bounds = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
        # Floored at 0.01m, in ONE place for every edge source (tile files,
        # KNOWN_NODE_GAPS bridges, synthetic stitches): 960 real exported
        # edges have length_m 0.0 -- a sub-5cm connector rounds to 0.0 at
        # export (pipeline/export.py rounds to 0.1m) -- and a snap landing
        # on a zero-length edge turns route()'s partial-edge division into
        # 0/0 -> NaN -> a crash at int(round(tree_count)). Found live: a
        # Central Park test route did exactly this once the stitch pass
        # changed which component snaps resolve onto. 1cm on a <5cm
        # connector distorts nothing.
        self._length = np.maximum(np.array(length, dtype=np.float32), 0.01)
        self._tree_deciduous = np.array(deciduous, dtype=np.float32)
        self._tree_evergreen = np.array(evergreen, dtype=np.float32)
        self._tree_count = np.array(counts, dtype=np.int32)
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
        self._apply_hide_rule(len(components))

        cached_rings = _load_cached_coverage_rings(coverage_cache_path, coverage_fingerprint)
        if cached_rings is not None:
            self._coverage_rings = cached_rings
        else:
            self._coverage_rings = self._compute_coverage_rings()
            _save_cached_coverage_rings(coverage_cache_path, coverage_fingerprint, self._coverage_rings)

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
        # Visible edges only (hide rule): the drawn boundary IS the
        # acceptance region, and hidden components no longer accept
        # clicks, so they must not be advertised either.
        footprint = shapely.union_all(
            shapely.buffer(self._edge_lines_scaled[self._visible_edge_mask], radius_deg, quad_segs=2)
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

    def _apply_hide_rule(self, component_count: int) -> None:
        """Exclude small disconnected components from snapping and
        coverage (see HIDDEN_COMPONENT_MAX_LEN_M's comment for the why),
        then rebuild the snap STRtree over visible edges only — one
        change at the index level makes _nearest_edge, _nearby_components,
        snap_pair and in_coverage all hidden-aware at once, while routing
        arrays stay indexed by real edge ids and untouched (a hidden edge
        can never be routed over because it can never be snapped onto).
        """
        comp_len = np.bincount(
            self._edge_component, weights=self._length.astype(np.float64),
            minlength=component_count,
        )
        hidden = comp_len < HIDDEN_COMPONENT_MAX_LEN_M
        # The largest component is the network itself, whatever its
        # absolute length — a toy test dataset or a sliver tile must keep
        # its main network clickable.
        hidden[int(np.argmax(comp_len))] = False

        for lat, lon, name in KEEP_VISIBLE_ISOLATED_PLACES:
            point = Point(lon * self._lat_scale, lat)
            idx, dist_deg = self._strtree.query_nearest(point, return_distance=True)
            dist_m = float(dist_deg[0]) * METERS_PER_DEGREE_LAT
            if dist_m > config.MAX_SNAP_DISTANCE_M:
                # partial dataset (tests, a single-tile load) — the place
                # just isn't in this data; nothing to keep visible
                continue
            component = int(self._edge_component[int(idx[0])])
            if hidden[component]:
                hidden[component] = False
                logger.info(f"[graph_store] keep-visible: {name} "
                      f"({comp_len[component] / 1000:.1f}km, curated exception)")

        self._visible_edge_mask = ~hidden[self._edge_component]
        hidden_edges = int((~self._visible_edge_mask).sum())
        if hidden_edges:
            hidden_comps = int(hidden.sum())
            hidden_km = float(comp_len[hidden].sum()) / 1000
            logger.info(f"[graph_store] hide rule: {hidden_comps} small component(s) "
                  f"({hidden_edges} edges, {hidden_km:.0f}km) excluded from "
                  f"snapping + coverage")
            self._strtree_edge_ids = np.where(self._visible_edge_mask)[0]
            self._strtree = STRtree(self._edge_lines_scaled[self._visible_edge_mask])
        else:
            self._strtree_edge_ids = np.arange(len(self._edge_lines_scaled))

    # ── Routing ───────────────────────────────────────────────────────────────

    def _nearest_edge(self, lat: float, lon: float) -> tuple[int, float]:
        """Nearest visible edge to a point, and the real-meters distance
        to it — the snap tree holds visible edges only (hide rule), so
        positions map back to real edge ids via _strtree_edge_ids."""
        point = Point(lon * self._lat_scale, lat)
        idx, dist_deg = self._strtree.query_nearest(point, return_distance=True)
        return int(self._strtree_edge_ids[int(idx[0])]), float(dist_deg[0]) * METERS_PER_DEGREE_LAT

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
        for tree_pos in candidates:
            edge = int(self._strtree_edge_ids[int(tree_pos)])
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
        SHADE_SATURATION_DENSITY for reporting)."""
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
