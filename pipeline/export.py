"""Write the finished graph to a gzipped JSON file the server loads.

The exported file is the contract between the pipeline and the routing
server: plain JSON (gzipped), no Python-specific types.

One writer: write_citywide(), which emits ONE file for all five boroughs.

The centerline model's per-tile writer, write_tile(), was deleted on
2026-08-23 along with the tile grid and the committed fixture that were the
only reasons it was still here. Its per-tile synthetic-node namespacing
(_node_id_str) went with it: that existed because each tile minted negative
ids from -1 independently, so the same bare id named a different real place
in every tile. A single citywide export has one id space and cannot have
that collision.

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
from pathlib import Path

from pipeline import config

logger = logging.getLogger(__name__)

# Basename of the one citywide export. Any *.json.gz in TILES_DIR is loaded,
# so this only has to be stable, not special.
CITYWIDE_NAME = "citywide"



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


def write_citywide(nodes: dict, edges: list[dict]) -> Path:
    """Write the whole city's pedestrian graph as ONE file. Returns its path.

    The path is returned rather than left for the caller to reconstruct so
    that only this module knows the filename -- a caller that rebuilt it
    from CITYWIDE_NAME could drift out of step with where the file
    actually goes, and would then "verify" a file that isn't the one just
    written.

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
            # FLOAT, not int. Block-face scoring gives each edge a SHARE of
            # its block's trees in proportion to its own length, so a face
            # with 3 trees spread over 10 edges hands each one 0.3 --
            # int() truncated that to 0 and all three trees vanished from
            # the export. The cast is still here for the reason it was
            # added (a numpy int64 is not JSON-serialisable and json.dump
            # raises TypeError on it), just widened. The user-facing count
            # stays a whole number: graph_store.py sums the shares along a
            # route and rounds once at the end.
            "tree_count": float(edge.get("tree_count", 0)),
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
    return out_path
