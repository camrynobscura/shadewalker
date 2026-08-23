"""Write the finished graph to a gzipped JSON file the server loads.

The exported file is the contract between the pipeline and the routing
server: plain JSON (gzipped), no Python-specific types.

Two writers, for the two models:

  write_citywide()  the sidewalk model — ONE file for all five boroughs.
  write_tile()      the retired centerline model's per-tile writer, still
                    here only because the committed test fixture and the
                    tests around it are still centerline-shaped. Goes with
                    the tile grid once those are regenerated.

Hard-won rule encoded here (this exact bug shipped in the v1 prototype):
everything written to the file is in lat/lon degrees (EPSG:4326) — the
meter-based geometry is for pipeline math only and must never leak into
the export, or the frontend would try to draw UTM coordinates on a map.
Coordinate pairs are [lon, lat] to match the GeoJSON convention.
"""

import logging
import gzip
import json
import os
from datetime import date

import geopandas as gpd

from pipeline import config

logger = logging.getLogger(__name__)

# Basename of the one citywide export. Any *.json.gz in TILES_DIR is loaded,
# so this only has to be stable, not special.
CITYWIDE_NAME = "citywide"



def _node_id_str(node_id, tile_id: str) -> str:
    """A node id as the string the exported file (and so the server) uses.

    Real OSM ids (positive ints, globally unique) export as-is. Synthetic
    ids (negative ints, minted per-tile from -1 by interior sidewalks/park
    trails in pipeline/fetch/streets.py) get namespaced with the tile id —
    "-1" → "r16c12:-1" — because the per-tile numbering means the same bare
    id names a DIFFERENT real-world point in every tile that has one, and
    server/graph_store.py's load() merges all tiles on this exact string,
    first-tile-wins. Un-namespaced, one tile's "-1" swallowed every other
    tile's "-1": 94.5k edges wired to nodes in the wrong borough, routes
    teleporting up to 49km (see FIXES.md, 2026-08-13).

    int(node_id), not isinstance(node_id, int): iterrows() yields numpy
    int64 values, which isinstance can miss depending on platform/numpy
    version — and a missed check here silently resurrects the collision.
    """
    if int(node_id) < 0:
        return f"{tile_id}:{int(node_id)}"
    return str(node_id)


def write_tile(tile_id: str, nodes: gpd.GeoDataFrame, edges: gpd.GeoDataFrame) -> None:
    node_records = {}
    # nodes.iterrows() yields (node_id, row) — osmnx stores lon as x, lat as y.
    for node_id, row in nodes.iterrows():
        node_records[_node_id_str(node_id, tile_id)] = [round(row["x"], 6), round(row["y"], 6)]

    edge_records = []
    for (u, v, key), row in edges.iterrows():
        edge_records.append({
            "u": _node_id_str(u, tile_id),
            "v": _node_id_str(v, tile_id),
            "key": int(key),        # disambiguates rare parallel edges (same u,v)
            "side": "C",            # centerline; "L"/"R" reserved for Stage 3
            "length_m": row["length_m"],
            "name": row["name"],
            "tree_deciduous": row["tree_deciduous"],
            "tree_evergreen": row["tree_evergreen"],
            "tree_count": int(row["tree_count"]),
            # the slice of tree_deciduous that came from park-canopy area
            # rather than countable trees (FIXES.md item 4) -- already
            # included in tree_deciduous, never add both
            "tree_park_canopy": round(float(row["tree_park_canopy"]), 3),
            # row["geometry"] is the LAT/Lon one — see module docstring.
            "coords": [[round(lon, 6), round(lat, 6)] for lon, lat in row["geometry"].coords],
            # The OSM node id at each geometry vertex, aligned 1:1 with
            # "coords" (FIXES item 13). Namespaced exactly like u/v so the
            # server matches them; null where the vertex is a shape point
            # or a coordinate collision, never a guess. The server splits
            # cross-tile edges at interior ids it also loaded as nodes,
            # rejoining streets severed at a tile border.
            "node_ids": [
                None if nid is None else _node_id_str(nid, tile_id)
                for nid in row["node_ids"]
            ],
        })

    tile = {
        "meta": {
            "tile_id": tile_id,
            "created": date.today().isoformat(),
            "crs": "EPSG:4326",
            "coord_order": "lon,lat",
            "node_count": len(node_records),
            "edge_count": len(edge_records),
        },
        "nodes": node_records,
        "edges": edge_records,
    }

    out_path = config.TILES_DIR / f"{tile_id}.json.gz"
    size_kb = _write_atomically(out_path, tile)
    logger.info(f"  [export] {_display_path(out_path)}: "
          f"{len(node_records)} nodes, {len(edge_records)} edges, {size_kb:.0f} KB gzipped")


def _write_atomically(out_path, payload: dict) -> float:
    """Write payload as gzipped JSON to out_path. Returns its size in KB.

    Atomic write (FIXES item 8, audit §2.5): write to a temp name in the
    SAME directory, then os.replace() -- which POSIX guarantees is
    all-or-nothing -- so no reader (a live server's load(), a
    mid-refresh restart) can ever see a truncated file, whether from a
    concurrent read or a crash mid-write. Same directory matters:
    os.replace() is only atomic within one filesystem, and a temp dir
    like /tmp can be a different one. The ".tmp" suffix keeps
    GraphStore.load()'s *.json.gz glob from ever matching a half-written
    file even before the rename.

    gzip.open in text mode ("wt") lets json.dump write straight into a
    compressed file — no intermediate uncompressed copy.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        with gzip.open(tmp_path, "wt") as f:
            json.dump(payload, f)
        os.replace(tmp_path, out_path)
    except BaseException:
        # a crashed write must not leave a stray .tmp behind to confuse
        # the next run (missing_ok: the open() itself may have failed)
        tmp_path.unlink(missing_ok=True)
        raise
    return out_path.stat().st_size / 1024


def _display_path(path):
    """A repo-relative path for logging, falling back to the absolute one.

    TILES_DIR can point outside the repo (SHADEWALKER_TILES_DIR, e.g. a
    scratch dir for a rebuild that must not touch data/tiles/) --
    relative_to() raises then, so fall back rather than crashing after the
    file is already written.
    """
    try:
        return path.relative_to(config.REPO_ROOT)
    except ValueError:
        return path


def write_citywide(nodes: dict, edges: list[dict]) -> None:
    """Write the whole city's pedestrian graph as ONE file.

    Not tiles and not per-borough: borough lines cut streets, which
    reintroduces the border-dedupe bug class the centerline model was
    deleted to escape. The client never downloads this -- it loads into
    server RAM at startup.

    It still lands in TILES_DIR under a *.json.gz name because that is
    what GraphStore.load() globs (server/graph_store.py:301); the
    directory keeps its old name until the tile grid is stripped.

    Tree fields are written as zeros when the caller has not scored the
    edges. An unscored graph is a legitimate intermediate -- it routes by
    distance alone, which is exactly what the spine is for -- and the
    server requires the keys to be present either way
    (server/graph_store.py:399-401).
    """
    node_records = {node_id: [round(lon, 6), round(lat, 6)]
                    for node_id, (lon, lat) in nodes.items()}

    edge_records = []
    for edge in edges:
        edge_records.append({
            "u": edge["u"],
            "v": edge["v"],
            "key": int(edge["key"]),
            "side": edge["side"],
            "length_m": edge["length_m"],
            "name": edge["name"],
            "tree_deciduous": edge.get("tree_deciduous", 0.0),
            "tree_evergreen": edge.get("tree_evergreen", 0.0),
            "tree_count": int(edge.get("tree_count", 0)),
            "tree_park_canopy": round(float(edge.get("tree_park_canopy", 0.0)), 3),
            # lat/lon degrees, [lon, lat] order — see the module docstring.
            "coords": edge["coords"],
        })

    payload = {
        "meta": {
            "tile_id": CITYWIDE_NAME,
            "created": date.today().isoformat(),
            "crs": "EPSG:4326",
            "coord_order": "lon,lat",
            "node_count": len(node_records),
            "edge_count": len(edge_records),
        },
        "nodes": node_records,
        "edges": edge_records,
    }

    out_path = config.TILES_DIR / f"{CITYWIDE_NAME}.json.gz"
    size_kb = _write_atomically(out_path, payload)
    logger.info(f"  [export] {_display_path(out_path)}: "
                f"{len(node_records):,} nodes, {len(edge_records):,} edges, "
                f"{size_kb / 1024:.1f} MB gzipped")
