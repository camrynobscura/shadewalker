"""Draw NYC's surveyed kerb and our routing graph over satellite imagery.

WHAT THIS IS FOR
----------------
Seeing, by eye, where the app has nothing to route on. NYC surveyed every
kerb photogrammetrically (Planimetrics Pavement Edge), so a kerb line with
no routing line beside it means the city says there is street frontage
here and OSM has drawn nothing. That is the 66.9% coverage figure drawn
instead of summarised, and it answers the question no number has: do the
gaps cluster somewhere, or scatter evenly?

NOTHING IS COMPUTED. There is no join between the two layers, no distance
threshold, no verdict, no cells -- both layers are drawn as they are and
the reader does the comparing. That is the point of the design, not a
shortcut in it: every machine answer to "is there a sidewalk beside this
kerb" needs a distance constant, and this project's history with those is
bad (TREE_BUFFER_M crept 12 -> 14). A picture needs none, and the eye is
better at it anyway.

RED IS NOT A TO-DO LIST. OSM is frequently right to have drawn nothing --
E 167 St is tagged `sidewalk=no` and the user confirmed one side genuinely
has none. THE GOVERNING RULE STANDS: we do not draw sidewalks OSM is
missing. This exists to know where the app is blind, nothing more.

WHAT REPLACED WHAT
------------------
This file used to render `measure_sidewalk_only_coverage.py`'s 250m gap
CELLS from `data/audits/2026-08-22/sidewalk_only_gaps.geojson`. That input
was deleted with its dated directory, so the tool had been crashing on
load rather than merely being stale -- and cells could never answer "which
SIDE of which street", which is the whole question under a per-side model.
The old version is at `git show 50db191:tools/audit/build_coverage_gap_map.py`.

Output is deliberately NOT written to a dated directory. Dating it is what
turned the last version into dead code: the map is a view of current data,
regenerated whenever the data moves, not a measurement pinned to a day.

Usage:
  uv run python tools/audit/build_coverage_gap_map.py
  uv run python tools/audit/build_coverage_gap_map.py --borough Bronx
"""
import argparse
import gzip
import json
import math
import sys
import time
from pathlib import Path

from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline import config
from pipeline.fetch import planimetrics
from pipeline.fetch.boundaries import fetch_borough_boundaries

# Douglas-Peucker tolerance for the kerb lines. Pavement Edge is
# photogrammetric survey carrying 17 decimal places, with consecutive
# vertices about a centimetre apart: 170,985 lines hold 12,709,669
# vertices, roughly 280MB of coordinates, which no browser will draw.
#
# 0.25m removes only vertices closer together than a screen pixel. At zoom
# 19 -- the deepest Esri serves -- a pixel is
# 156543 * cos(40.7 deg) / 2**19 = 0.227m, so a moved vertex shifts by at
# most about one pixel at maximum zoom. Nothing is filtered and no line is
# dropped: 170,985 in, 170,985 out. Measured reduction 12.7M -> 1.16M
# vertices (9.1%). Coarser settings were measured too, and are here so
# nobody re-runs the sweep: 0.5m -> 906,485 (7.1%), 1.0m -> 677,307 (5.3%).
SIMPLIFY_M = 0.25

# Coordinates are emitted at 6 decimal places (~0.11m), one step finer than
# SIMPLIFY_M so rounding is never the thing you see. 5dp (~1.1m) would be
# coarser than the simplification and would visibly quantise the kerb.
COORD_DP = 6

# How far past a borough's boundary to keep drawing, so the map doesn't end
# in a hard cliff mid-street when you pan to the edge of one file.
#
# THIS BUFFERS IN DEGREES, DELIBERATELY. Buffering in degrees is the #1 bug
# class in this project and it is normally wrong -- but this is a viewing
# margin, not a measurement: nothing downstream reads it and no number is
# derived from it. The anisotropy is the honest cost: 400/111320 of a
# degree is 400m north-south and 400/cos(40.7 deg) = 527m east-west. Both
# are "a few blocks", which is all this needs to be.
BORDER_OVERLAP_M = 400.0

# Metres per degree of latitude on the WGS84 ellipsoid at NYC's latitude.
# Only ever used for the two approximations above, both of which say so.
M_PER_DEG_LAT = 111_320.0


def simplify_lonlat(coords, tolerance_m):
    """Douglas-Peucker in metres without leaving lon/lat.

    Simplification needs an isotropic space. A degree of longitude at NYC's
    latitude is ~84.4km against ~111.3km for a degree of latitude, so a
    plain degree tolerance would cut 32% harder east-west than north-south
    and the kerb would lose detail differently depending on which way the
    street runs. Scaling longitude by cos(lat) first makes the two axes the
    same size, so one tolerance means the same distance in both.

    Cheaper than pushing 171k lines through pyproj, and exact enough here:
    the residual error is the change in cos(lat) along a single kerb line,
    which is nil at block scale.
    """
    scale = math.cos(math.radians(coords[0][1]))
    if scale <= 0.0:
        return coords
    stretched = LineString([(lon * scale, lat) for lon, lat in coords])
    thinned = stretched.simplify(tolerance_m / M_PER_DEG_LAT,
                                 preserve_topology=False)
    return [(lon / scale, lat) for lon, lat in thinned.coords]


def load_boroughs(overlap_m):
    """(names, buffered polygons, STRtree, unbuffered bounds per borough).

    The polygon is buffered so lines just outside a borough still land in
    its file -- see BORDER_OVERLAP_M for why that buffer is in degrees.
    """
    geojson = fetch_borough_boundaries()
    names, shapes, bounds = [], [], {}
    for feature in geojson["features"]:
        props = feature["properties"]
        name = props.get("boroname") or props.get("boro_name") or "?"
        geom = shape(feature["geometry"])
        min_lon, min_lat, max_lon, max_lat = geom.bounds
        bounds[name] = [[min_lat, min_lon], [max_lat, max_lon]]
        names.append(name)
        shapes.append(geom.buffer(overlap_m / M_PER_DEG_LAT))
    return names, shapes, STRtree(shapes), bounds


def boroughs_touching(coords, names, shapes, tree):
    """Which borough files this line belongs in, by its two endpoints.

    Both ends, not just the first: a line that starts outside every buffered
    borough but ends well inside one is real geometry we want to see. Not the
    MIDPOINT -- on a two-vertex line that is a fabricated point, and this
    project has paid for midpoint reasoning four times.
    """
    found = []
    for lon, lat in (coords[0], coords[-1]):
        point = Point(lon, lat)
        for position in tree.query(point):
            if names[position] in found:
                continue
            if shapes[position].contains(point):
                found.append(names[position])
    return found


def round_latlon(coords):
    """Leaflet order ([lat, lon]) at COORD_DP, ready to inline."""
    return [[round(lat, COORD_DP), round(lon, COORD_DP)] for lon, lat in coords]


def collect_kerb(names, shapes, tree):
    """NYC's surveyed kerb, Road Edge only, simplified, bucketed by borough."""
    rows = planimetrics.load("pavement_edge")
    buckets = {name: [] for name in names}
    kept = dropped = 0
    for row in rows:
        # Road Edge only. Pavement Edge also carries alleys (2270) and
        # runway/taxiway edge (2230) -- 7,962 lines that are not street
        # frontage and would read as gaps that nobody should be walking in.
        if row.get("feat_code") != config.ROAD_EDGE_FEAT_CODE:
            dropped += 1
            continue
        for part in row["the_geom"]["coordinates"]:
            if len(part) < 2:
                continue
            thinned = simplify_lonlat(part, SIMPLIFY_M)
            if len(thinned) < 2:
                continue
            drawn = round_latlon(thinned)
            for name in boroughs_touching(part, names, shapes, tree):
                buckets[name].append(drawn)
            kept += 1
    return buckets, kept, dropped


def collect_routing(names, shapes, tree):
    """Every edge the router can actually walk, from the citywide export.

    Not the .pbf: the export is what the server loads, so it is already
    split at junctions and already filtered, and drawing it means the map
    shows what the app HAS rather than what OSM holds. No simplification --
    these average 2.88 vertices per edge already.
    """
    path = config.TILES_DIR / "citywide.json.gz"
    if not path.exists():
        raise SystemExit(
            f"no export at {path} -- run `uv run python -m pipeline.build` first"
        )
    with gzip.open(path) as fh:
        export = json.load(fh)
    buckets = {name: [] for name in names}
    for edge in export["edges"]:
        coords = edge["coords"]
        if len(coords) < 2:
            continue
        drawn = round_latlon(coords)
        for name in boroughs_touching(coords, names, shapes, tree):
            buckets[name].append(drawn)
    return buckets, len(export["edges"])


HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;background:#0b1410}
  #map{height:100%}
  .panel{position:absolute;z-index:500;left:12px;top:12px;width:290px;
    background:#0f1a15f2;color:#dcefe4;border:1px solid #2f5a45;
    padding:14px 16px;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
  .panel h1{font-size:14px;margin:0 0 2px;letter-spacing:.04em}
  .panel .sub{color:#7fae95;font-size:11px;margin:0 0 12px}
  .row{display:flex;align-items:center;gap:9px;padding:5px 0}
  .row label{flex:1;cursor:pointer}
  .row input[type=checkbox]{width:16px;height:16px;accent-color:#3ddc97;cursor:pointer}
  .sw{width:22px;height:0;border-top-width:3px;border-top-style:solid;flex:none}
  .n{color:#7fae95;font-size:10.5px;font-variant-numeric:tabular-nums}
  .fade{margin:10px 0 2px}
  .fade input{width:100%;accent-color:#3ddc97}
  .note{margin-top:13px;padding-top:11px;border-top:1px solid #2f5a45;
    color:#8dbba2;font-size:10.5px;line-height:1.5}
  .note b{color:#dcefe4}
  .hint{position:absolute;z-index:500;left:12px;bottom:12px;
    background:#0f1a15f2;color:#8dbba2;border:1px solid #2f5a45;
    padding:7px 11px;font:10.5px/1.4 ui-monospace,Menlo,monospace}
</style>
<div id="map"></div>
<div class="panel">
  <h1>__TITLE__</h1>
  <p class="sub">__STAMP__</p>
  <div class="row">
    <span class="sw" style="border-top-color:#ff2d95"></span>
    <label for="kerb">NYC surveyed kerb</label>
    <input type="checkbox" id="kerb" checked>
  </div>
  <div class="n">__KERB_N__ lines &middot; Pavement Edge, Road Edge only</div>
  <div class="row">
    <span class="sw" style="border-top-color:#22e0ff"></span>
    <label for="ped">Our routing graph</label>
    <input type="checkbox" id="ped" checked>
  </div>
  <div class="n">__PED_N__ edges &middot; everything the router can walk</div>
  <div class="fade">
    <div class="n">Imagery brightness</div>
    <input type="range" id="fade" min="15" max="100" value="100">
  </div>
  <div class="note">
    Kerb with <b>no cyan beside it</b> is street frontage the city surveyed
    and OSM never drew.<br><br>
    <b>Not a to-do list.</b> OSM is often right that nothing is there.
    Nothing on this page is computed &mdash; both layers are drawn as they
    are, and no rule decides what counts as a gap.
  </div>
</div>
<div class="hint">Zoom in &mdash; the two layers sit ~2m apart</div>
<script>
const KERB = __KERB__;
const PED  = __PED__;
const BOUNDS = __BOUNDS__;

const map = L.map('map', {preferCanvas: true, zoomControl: false});
L.control.zoom({position: 'bottomright'}).addTo(map);
map.fitBounds(BOUNDS);

// maxNativeZoom lets the deepest real tile be upscaled past z19 instead of
// the layer returning nothing. Without it, zooming in past what the host
// actually serves paints grey squares over the map -- the same bug that hit
// web/src/components/MapView.tsx on 2026-08-24.
const sat = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxNativeZoom: 19, maxZoom: 21, attribution: 'Esri World Imagery'}).addTo(map);
const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxNativeZoom: 19, maxZoom: 21, attribution: '&copy; OpenStreetMap'});
L.control.layers({'Satellite': sat, 'Street map': street}, null,
  {collapsed: true, position: 'topright'}).addTo(map);

// ONE multi-part polyline per layer, not one object per line. Queens holds
// 241k lines between the two layers, and a Leaflet object each would cost
// hundreds of MB and stall every pan; a multi-part polyline on the canvas
// renderer draws the same pixels from two objects total.
const kerbLayer = L.polyline(KERB, {color:'#ff2d95', weight:1.6, opacity:.95}).addTo(map);
const pedLayer  = L.polyline(PED,  {color:'#22e0ff', weight:2.1, opacity:.95}).addTo(map);

function bind(id, layer) {
  document.getElementById(id).addEventListener('change', e => {
    e.target.checked ? layer.addTo(map) : map.removeLayer(layer);
  });
}
bind('kerb', kerbLayer);
bind('ped', pedLayer);

document.getElementById('fade').addEventListener('input', e => {
  const v = e.target.value / 100;
  for (const el of document.querySelectorAll('.leaflet-tile-pane'))
    el.style.opacity = v;
});
</script>
"""


def write_map(out_dir, borough, kerb, ped, bounds, stamp):
    """One self-contained page. `bounds` frames the BOROUGH, not the data:
    a long parkway kerb whose far end lands in another borough belongs in
    this file (both endpoints are tested) but must not set the view, or the
    map opens zoomed out to nothing."""
    page = (HTML
            .replace("__TITLE__", borough)
            .replace("__STAMP__", stamp)
            .replace("__KERB_N__", f"{len(kerb):,}")
            .replace("__PED_N__", f"{len(ped):,}")
            .replace("__BOUNDS__", json.dumps(bounds))
            .replace("__KERB__", json.dumps(kerb, separators=(",", ":")))
            .replace("__PED__", json.dumps(ped, separators=(",", ":"))))
    path = out_dir / f"{borough.lower().replace(' ', '_')}.html"
    path.write_text(page)
    return path, len(page)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/audits/coverage_maps",
                        help="output directory (undated on purpose)")
    parser.add_argument("--borough", action="append",
                        help="limit to one borough; repeatable")
    args = parser.parse_args()

    started = time.perf_counter()
    out_dir = config.REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    names, shapes, tree, bounds = load_boroughs(BORDER_OVERLAP_M)
    print(f"boroughs: {', '.join(names)}")

    kerb, kerb_kept, kerb_dropped = collect_kerb(names, shapes, tree)
    print(f"kerb:    {kerb_kept:,} Road Edge lines "
          f"({kerb_dropped:,} alley/runway dropped)  "
          f"[{time.perf_counter() - started:.0f}s]")

    ped, ped_total = collect_routing(names, shapes, tree)
    print(f"routing: {ped_total:,} edges  "
          f"[{time.perf_counter() - started:.0f}s]")

    wanted = args.borough or names
    stamp = time.strftime("%Y-%m-%d")
    print()
    for borough in names:
        if borough not in wanted:
            continue
        path, size = write_map(out_dir, borough, kerb[borough], ped[borough],
                               bounds[borough], stamp)
        print(f"  {borough:<16} {len(kerb[borough]):>7,} kerb  "
              f"{len(ped[borough]):>7,} edges  {size / 1e6:>6.1f} MB  "
              f"{path.relative_to(config.REPO_ROOT)}")
    print(f"\n{time.perf_counter() - started:.0f}s total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
