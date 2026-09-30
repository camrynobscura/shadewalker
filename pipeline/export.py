"""Write the finished graph to a gzipped JSON file the server loads.

The exported file is the contract between the pipeline and the routing
server: plain JSON (gzipped), no Python-specific types.

One writer: write_citywide(), which emits one file for all five boroughs.

Everything written to the file is in lat/lon degrees (EPSG:4326): the
meter-based geometry is for pipeline math only and must never leak into
the export, or the frontend would try to draw UTM coordinates on a map.
Coordinate pairs are [lon, lat] to match the GeoJSON convention.
"""

import base64
import logging
import gzip
import json
import os
from datetime import date
from pathlib import Path

from pipeline import config

logger = logging.getLogger(__name__)

# Basename of the one citywide export. Any *.json.gz in EXPORT_DIR is loaded,
# so this only has to be stable, not special.
CITYWIDE_NAME = "citywide"



def _write_atomically(out_path, payload: dict) -> float:
    """Write payload as gzipped JSON to out_path. Returns its size in KB.

    Atomic write: write to a temp name in the same directory, then
    os.replace(), which POSIX guarantees is all-or-nothing, so no reader
    (a live server's load(), a mid-refresh restart) can ever see a
    truncated file. Same directory matters: os.replace() is only atomic
    within one filesystem, and a temp dir like /tmp can be a different
    one. The ".tmp" suffix keeps GraphStore.load()'s *.json.gz glob from
    ever matching a half-written file even before the rename.

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

    EXPORT_DIR can point outside the repo (SHADEWALKER_EXPORT_DIR, e.g. a
    scratch dir for a rebuild that must not touch data/export/) --
    relative_to() raises then, so fall back rather than crashing after the
    file is already written.
    """
    try:
        return path.relative_to(config.REPO_ROOT)
    except ValueError:
        return path


def write_citywide(nodes: dict, edges: list[dict], sun_table=None) -> Path:
    """Write the whole city's pedestrian graph as ONE file. Returns its path.

    The path is returned rather than left for the caller to reconstruct so
    that only this module knows the filename -- a caller that rebuilt it
    from CITYWIDE_NAME could drift out of step with where the file
    actually goes, and would then "verify" a file that isn't the one just
    written.

    Not tiles and not per-borough: borough lines cut streets, and every
    cut is a border to dedupe across. The client never downloads this --
    it loads into server RAM at startup.

    It lands in EXPORT_DIR under a *.json.gz name because that is what
    GraphStore.load() globs -- the server merges every matching file it
    finds there, which is also how the e2e tier serves the pilot fixture
    without a citywide build.

    Tree fields are written as zeros when the caller has not scored the
    edges. An unscored graph is a legitimate intermediate -- it routes by
    distance alone -- and the server requires the keys to be present
    either way (server/graph_store.py).
    """
    node_records = {node_id: [round(lon, 6), round(lat, 6)]
                    for node_id, (lon, lat) in nodes.items()}

    edge_records = []
    for edge in edges:
        record = {
            "u": edge["u"],
            "v": edge["v"],
            "key": int(edge["key"]),
            # Compass side of the parent street ("N"/"S"/"E"/"W"), "" when
            # none is honest or the edge has no street (crossings, park
            # paths). Geometric, not CSCL's L/R: see
            # BlockFaceIndex.compass_side.
            "side": edge["side"],
            # OSM's own classification ("footway/sidewalk",
            # "footway/crossing", "steps"...). Direction rendering folds
            # crossings into the street run they interrupt, which no
            # length heuristic can do (crossings run 15-31m and wider on
            # avenues).
            "kind": edge.get("kind", ""),
            "length_m": edge["length_m"],
            "name": edge["name"],
            "tree_deciduous": edge.get("tree_deciduous", 0.0),
            "tree_evergreen": edge.get("tree_evergreen", 0.0),
            # Float, not int. Block-face scoring gives each edge a share of
            # its block's trees in proportion to its own length, so a face
            # with 3 trees spread over 10 edges hands each one 0.3, which
            # int() would truncate to nothing. The cast itself guards
            # against a numpy int64, which json.dump can't serialise. The
            # walker-facing count stays a whole number: graph_store.py sums
            # the shares along a route and rounds once at the end.
            "tree_count": float(edge.get("tree_count", 0)),
            "tree_park_canopy": round(float(edge.get("tree_park_canopy", 0.0)), 3),
            # lat/lon degrees, [lon, lat] order — see the module docstring.
            "coords": edge["coords"],
        }
        # Only on UNNAMED edges, and only when naming found plausible
        # parents (pipeline/graph/naming.py): the folding evidence that
        # replaced a length threshold. Omitted otherwise -- 488k edges pay
        # for every key.
        fold_names = edge.get("fold_names")
        if fold_names:
            record["fold_names"] = fold_names
        # Building shade: 288 bytes per edge (pipeline/scoring/shadows.py),
        # month-major, value/255, as base64. Measured on the citywide table
        # (tools/audit/measure_export_candidates.py, #109): 78% zeros, so
        # it costs 31 MB gzipped and ~1.4 s of startup; a .npy sidecar
        # loaded 0.8 s faster at 141 MB more per deploy and a second file
        # to keep in step. Omitted when all-zero, the fold_names rule.
        shade = edge.get("building_shade")
        if shade and any(shade):
            record["building_shade"] = base64.b64encode(shade).decode("ascii")
        edge_records.append(record)

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
    if sun_table is not None:
        # The file describes its own shade table: which sun each slot was
        # computed for, and how to read a row. The server blends between
        # slots by the minute and by the day; it never recomputes the sun.
        payload["meta"]["sun_table"] = sun_table
        payload["meta"]["shade_slots"] = {
            "layout": "month-major: index = (month - 1) * 24 + hour",
            "months": 12,
            "hours": 24,
            "anchor": f"the {config.SUN_ANCHOR_DAY}th of the month, on the hour, "
                      f"{config.SUN_TIMEZONE}, year {config.SUN_ANCHOR_YEAR}",
            "value": "round(255 * shaded points / sampled points); night 0",
            "encoding": "building_shade = base64 of 288 uint8; absent = all zero",
            "sun": "[azimuth deg clockwise from north, elevation deg], null = night",
        }

    out_path = config.EXPORT_DIR / f"{CITYWIDE_NAME}.json.gz"
    size_kb = _write_atomically(out_path, payload)
    logger.info(f"  [export] {_display_path(out_path)}: "
                f"{len(node_records):,} nodes, {len(edge_records):,} edges, "
                f"{size_kb / 1024:.1f} MB gzipped")
    return out_path
