"""Match every OSM sidewalk to a CSCL block face via NYC's kerb lines.

THE QUESTION
------------
Which block face does each piece of pavement belong to, and which side of
the street is it on? That pairing is the denominator for shade scoring: a
tree count is only meaningful divided by a stretch of pavement that means
something, and OSM's own edges do not -- 34.8% of them are under 5m and
hold 2.13% of the walking surface, so the same tree reads 1-per-2.6m on one
piece and 1-per-51.9m on the next.

METHOD
------
For each pedestrian edge, find the nearest NYC Pavement Edge (kerb) and
take its `blockf_id`, then resolve that through CSCL to a street name, a
side, and the street's own measured width.

The centerline distance is computed in the SAME pass, so the two can be
compared on an identical population rather than across two runs. That
comparison is the point: a kerb sits ~2m from its sidewalk whatever the
road width, a centerline sits at half the roadway.

Two things this deliberately does NOT do:
  - guess a threshold. It records the DISTANCE per edge; every "within Xm"
    question afterwards is a comparison against a stored number, not a
    re-run. That is what makes the sweep in measure_kerb_vs_centerline.py
    free.
  - use coords[len//2] as an edge's midpoint. For a 2-coordinate edge --
    293,356 of 488,638 of them -- that is the ENDPOINT, which puts every
    side and park judgement at a junction. Use line.interpolate(0.5,
    normalized=True).

Writes a per-edge table (.npz) plus a summary (.json). Local only,
read-only against the project's own data.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import osmium
from osmium.filter import EntityFilter
from shapely.geometry import LineString, Point, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config  # noqa: E402
from pipeline.fetch.boundaries import fetch_borough_boundaries  # noqa: E402
from pipeline.fetch.parks import fetch_park_properties  # noqa: E402
from pipeline.graph.boundary import nyc_boundary, park_polygon  # noqa: E402
from pipeline.graph.blockface import (  # noqa: E402
    build_face_lookup, build_kerb_index,
)
from pipeline.graph.naming import (  # noqa: E402
    _to_m, _probe_points, _K as LON_SCALE, _LAT_M as LAT_SCALE,
)
from pipeline.graph.pedestrian import (  # noqa: E402
    Way, find_junctions, _split_way, _chain_lengths_m,
    is_pedestrian, is_named_street, _touches_nyc, _normalize_name,
)
from tools.audit import fetch_planimetrics  # noqa: E402

SEARCH_CAP_M = 120.0
KEEP_TAGS = ("highway", "footway", "surface", "access", "foot", "area")


def read_pedestrian_with_tags(pbf_path, nyc_shape):
    """pedestrian.read_ways() drops tags, but `footway=sidewalk` is what
    separates a real sidewalk from a crossing or a park path, so this pass
    keeps them. Same filters, byte for byte."""
    prepared = prep(nyc_shape)
    bounds = nyc_shape.bounds
    ped, streets = [], []
    processor = (osmium.FileProcessor(pbf_path, osmium.osm.NODE | osmium.osm.WAY)
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
        lons, lats, node_ids = [], [], []
        for node in way.nodes:
            if node.location.valid():
                node_ids.append(node.ref)
                lons.append(node.lon)
                lats.append(node.lat)
        if len(node_ids) < 2 or not _touches_nyc(prepared, lons, lats, *bounds):
            continue
        parsed = Way(osm_id=way.id, name=_normalize_name(tags.get("name")),
                     node_ids=node_ids, lons=lons, lats=lats)
        if walkable:
            ped.append((parsed, {k: tags[k] for k in KEEP_TAGS if k in tags}))
        if street:
            streets.append(parsed)
    return ped, streets




def nearest(line, probes, index, geoms, cap):
    """(median probe distance, index) of the nearest geometry, or None.

    MEDIAN, not minimum: a cross street is close at one END of a sidewalk
    and far along the rest, so a minimum-distance rule calls almost every
    sidewalk ambiguous. naming.py made this same choice; the first version
    of its audit reported 16.1% instead of 94.0% by using the minimum.
    """
    best = None
    for position in index.query(line.buffer(cap)):
        distances = sorted(p.distance(geoms[position]) for p in probes)
        median = distances[len(distances) // 2]
        if best is None or median < best[0]:
            best = (median, position)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/audits/2026-08-23/sidewalk_kerb_match")
    args = ap.parse_args()
    started = time.perf_counter()

    def elapsed():
        return time.perf_counter() - started

    nyc = nyc_boundary(fetch_borough_boundaries())
    pave = fetch_planimetrics.load("pavement_edge")
    cscl = fetch_planimetrics.load("cscl")
    kerb_index, kerbs, kerb_face, kerb_conflated = build_kerb_index(pave)
    faces = build_face_lookup(cscl)
    print(f"[{elapsed():5.1f}s] {len(kerbs):,} kerb lines, "
          f"{len(faces):,} resolvable block faces")

    ped, street_ways = read_pedestrian_with_tags(config.OSM_EXTRACT_PATH, nyc)
    print(f"[{elapsed():5.1f}s] {len(ped):,} pedestrian ways, "
          f"{len(street_ways):,} named streets")

    # Street centerlines split at their own junctions == blocks, for the
    # side-by-side comparison against the kerb.
    street_junctions = find_junctions(street_ways)
    blocks = []
    for way in street_ways:
        for _, lons, lats in _split_way(way, street_junctions):
            if len(lons) >= 2:
                blocks.append(LineString([_to_m(a, b) for a, b in zip(lons, lats)]))
    block_index = STRtree(blocks)

    ped_ways = [w for w, _ in ped]
    tags_by_osm = {w.osm_id: t for w, t in ped}
    junctions = find_junctions(ped_ways)
    edges = []
    for way in ped_ways:
        for _, lons, lats in _split_way(way, junctions):
            if len(lons) >= 2:
                edges.append((way.osm_id, way.name, lons, lats))
    lengths = _chain_lengths_m([(None, lons, lats) for _, _, lons, lats in edges])
    n = len(edges)
    print(f"[{elapsed():5.1f}s] {n:,} pedestrian edges")

    parks = prep(park_polygon(fetch_park_properties()))

    cols = {
        "osm_id": np.array([e[0] for e in edges], np.int64),
        "own_name": np.array([e[1] for e in edges]),
        "length_m": lengths.astype(np.float32),
        "kerb_dist": np.full(n, np.inf, np.float32),
        "centerline_dist": np.full(n, np.inf, np.float32),
        "blockface": np.empty(n, object),
        "conflated": np.zeros(n, bool),
        "side": np.zeros(n, "U1"),
        "street": np.empty(n, object),
        "width_ft": np.full(n, np.nan, np.float32),
        "boro": np.empty(n, object),
        "kind": np.empty(n, object),
        "in_park": np.zeros(n, bool),
        # TRUE midpoint in lon/lat. Downstream tools (side verification,
        # Street View batches) need a point ON the edge, and it must not be
        # an endpoint -- a junction is the worst place to judge a side from.
        "mid_lon": np.full(n, np.nan, np.float64),
        "mid_lat": np.full(n, np.nan, np.float64),
    }

    for i, (osm_id, _, lons, lats) in enumerate(edges):
        tags = tags_by_osm.get(osm_id, {})
        footway = tags.get("footway")
        cols["kind"][i] = tags.get("highway", "?") + (f"/{footway}" if footway else "")
        cols["blockface"][i] = ""
        cols["street"][i] = ""
        cols["boro"][i] = ""

        line = LineString([_to_m(a, b) for a, b in zip(lons, lats)])
        if line.length < 1e-9:
            continue
        # Back to lon/lat for the park test -- _to_m is a plain scaling, so
        # dividing by the same constants inverts it exactly.
        middle = line.interpolate(0.5, normalized=True)
        mid_lon, mid_lat = middle.x / LON_SCALE, middle.y / LAT_SCALE
        cols["mid_lon"][i] = mid_lon
        cols["mid_lat"][i] = mid_lat
        cols["in_park"][i] = parks.contains(Point(mid_lon, mid_lat))

        probes = _probe_points(line)
        hit = nearest(line, probes, block_index, blocks, SEARCH_CAP_M)
        if hit is not None:
            cols["centerline_dist"][i] = hit[0]

        hit = nearest(line, probes, kerb_index, kerbs, SEARCH_CAP_M)
        if hit is None:
            continue
        cols["kerb_dist"][i] = hit[0]
        face = kerb_face[hit[1]]
        cols["blockface"][i] = face
        cols["conflated"][i] = bool(kerb_conflated[hit[1]])
        meta = faces.get(face)
        if meta:
            cols["side"][i] = meta.side
            cols["street"][i] = meta.street
            cols["width_ft"][i] = meta.width_ft
            cols["boro"][i] = meta.boro

        if (i + 1) % 100000 == 0:
            rate = (i + 1) / elapsed()
            print(f"[{elapsed():5.1f}s] {i + 1:,}/{n:,} ({rate:,.0f}/s)", flush=True)

    base = args.out
    os.makedirs(os.path.dirname(base), exist_ok=True)
    np.savez_compressed(
        base + ".npz",
        **{k: (v.astype(str) if v.dtype == object else v) for k, v in cols.items()})

    sidewalk = (cols["kind"].astype(str) == "footway/sidewalk") & (cols["length_m"] >= 5.0)
    summary = {
        "edges": int(n),
        "sidewalk_edges": int(sidewalk.sum()),
        "sidewalk_km": round(float(cols["length_m"][sidewalk].sum() / 1000), 1),
        "kerb_median_m": round(float(np.median(cols["kerb_dist"][sidewalk])), 2),
        "centerline_median_m": round(
            float(np.median(cols["centerline_dist"][sidewalk])), 2),
        "kerb_within_5m_pct": round(float(
            100 * cols["length_m"][sidewalk & (cols["kerb_dist"] <= 5)].sum()
            / cols["length_m"][sidewalk].sum()), 2),
    }
    with open(base + ".json", "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\n{json.dumps(summary, indent=2)}")
    print(f"\n[{elapsed():5.1f}s] wrote {base}.npz / .json")


if __name__ == "__main__":
    main()
