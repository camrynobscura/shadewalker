"""Write one tile's finished graph to data/tiles/<tile_id>.json.gz.

The exported file is the contract between the pipeline and the routing
server: plain JSON (gzipped), no Python-specific types.

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

    config.TILES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.TILES_DIR / f"{tile_id}.json.gz"
    # Atomic write (FIXES item 8, audit §2.5): write to a temp name in the
    # SAME directory, then os.replace() -- which POSIX guarantees is
    # all-or-nothing -- so no reader (a live server's load(), a
    # mid-refresh restart) can ever see a truncated tile, whether from a
    # concurrent read or a crash mid-write. Same directory matters:
    # os.replace() is only atomic within one filesystem, and a temp dir
    # like /tmp can be a different one. The ".tmp" suffix keeps
    # GraphStore.load()'s *.json.gz glob from ever matching a half-written
    # file even before the rename.
    #
    # gzip.open in text mode ("wt") lets json.dump write straight into a
    # compressed file — no intermediate uncompressed copy.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        with gzip.open(tmp_path, "wt") as f:
            json.dump(tile, f)
        os.replace(tmp_path, out_path)
    except BaseException:
        # a crashed write must not leave a stray .tmp behind to confuse
        # the next run (missing_ok: the open() itself may have failed)
        tmp_path.unlink(missing_ok=True)
        raise

    size_kb = out_path.stat().st_size / 1024
    logger.info(f"  [export] {out_path.relative_to(config.REPO_ROOT)}: "
          f"{len(node_records)} nodes, {len(edge_records)} edges, {size_kb:.0f} KB gzipped")
