"""Where the tree data is, and where it isn't -- one page per borough.

    uv run python tools/audit/build_tree_gap_map.py

WHY THIS EXISTS
---------------
`park-canopy` rests on a claim: that Forestry (`hn5i-inap`) cannot see a
large part of the canopy in NYC's parks. That claim has been measured three
ways and every one of them is a ratio. A ratio cannot tell you whether the
trees in a park you have WALKED are on the map, and the user has walked most
of these parks. So: draw the trees, draw the park outlines, and let a human
who knows the ground look at it.

NOTHING HERE IS COMPUTED. Same discipline as build_coverage_gap_map.py --
two raw layers over imagery, no threshold, no verdict, no colour scale
encoding a judgement. A park outline with no green in it is a park with no
trees on record, and that is a fact you can see rather than a number you
have to trust.

THE THREE LAYERS
----------------
  green dots     every living, usable Forestry tree (tree_value() is not
                 None -- the same test pipeline/scoring/blocks.py applies,
                 so this is what scoring actually sees, not the raw feed)
  amber outline  parks NYC's own Parks Properties knows about, filtered by
                 boundary._is_real_park() so medians and traffic triangles
                 stay out -- the production park mask
  violet outline parks only OSM knows about. Parks Properties is
                 city-agencies-only by construction (every `jurisdiction`
                 value is a city agency), so every state and federal park in
                 NYC is missing from it. These are drawn from the pinned
                 .pbf, which needs no new fetch and follows the same
                 "OSM is the source of truth" rule as the routing graph.

WHY POINTS NEED A HAND-WRITTEN CANVAS LAYER
-------------------------------------------
The coverage map draws ONE multi-part polyline per layer, because a Leaflet
object per line would cost hundreds of MB. Points have no equivalent trick:
`L.circleMarker` is one object each, and Queens alone carries ~200k trees.
So DotLayer below owns a single <canvas> and draws every visible tree itself
on each pan. Two consequences worth knowing:
  - the dots are drawn with fillRect below zoom 17 and arc() above it.
    A 1px circle costs the same as a 1px square and looks identical; the
    arc only earns its cost once the dot is big enough to read as round.
  - points are bounds-filtered before projecting, which is what keeps a
    deep zoom cheap. At borough zoom nothing is filtered and every tree is
    projected, which is the slow case and still lands under ~200ms.
"""

import argparse
import json
import math
import time
from datetime import date

import osmium
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from shapely import make_valid

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from pipeline import config                                      # noqa: E402
from pipeline.fetch.boundaries import fetch_borough_boundaries   # noqa: E402
from pipeline.fetch.parks import fetch_park_properties           # noqa: E402
from pipeline.fetch.trees import fetch_trees                     # noqa: E402
from pipeline.graph.boundary import _is_real_park, nyc_boundary  # noqa: E402
from pipeline.scoring.trees import tree_value                    # noqa: E402

# Park outlines only need to read as the right shape, not to measure
# anything, so they are thinned hard. 2m is ~9 pixels at zoom 19 and
# invisible at the zooms a park outline is actually looked at.
OUTLINE_SIMPLIFY_M = 2.0

# Tree coordinates: 5dp is ~1.1m at NYC's latitude, well under the ~5m
# positional accuracy of the Forestry points themselves and half the size
# of the dot drawn for them. 6dp would inflate every borough file for
# precision the source does not have.
TREE_DP = 5
OUTLINE_DP = 6

M_PER_DEG_LAT = 111_320.0

# Smallest OSM park worth drawing as a "the city list misses this" outline.
# Below this the layer fills with back gardens tagged leisure=garden and the
# real finding (Floyd Bennett Field, Clay Pit Ponds) is lost in it.
MIN_OSM_PARK_HA = 2.0

# The all-in-one page. Every layer is accumulated under this key alongside
# the per-borough ones, in the same pass -- so the citywide page can never
# drift out of step with the five it is made from. Parks are added here ONCE
# rather than per borough, or a park straddling a boundary would be drawn
# (and tooltipped) twice.
CITYWIDE = "All five boroughs"


def simplify_lonlat(coords, tolerance_m):
    """Douglas-Peucker in degrees, with longitude scaled by cos(lat) first.

    A degree of longitude is ~84.4km at NYC's latitude against ~111.3km for
    latitude, so a plain degree tolerance cuts ~32% harder east-west and a
    park outline would lose detail depending on which way it happens to run.
    Lifted from build_coverage_gap_map.py, same reasoning.
    """
    if len(coords) < 4:
        return coords
    scale = math.cos(math.radians(coords[0][1]))
    if scale <= 0.0:
        return coords
    # A LineString, NOT a Polygon: simplifying a polygon can pinch it into a
    # MultiPolygon (a park joined by a path narrower than the tolerance), and
    # then there is no single .exterior to read back. A ring IS a closed
    # line, so thinning it as one keeps the result a single coordinate list.
    thinned = LineString([(lon * scale, lat) for lon, lat in coords]).simplify(
        tolerance_m / M_PER_DEG_LAT, preserve_topology=False)
    if thinned.is_empty or len(thinned.coords) < 4:
        return coords
    return [(lon / scale, lat) for lon, lat in thinned.coords]


def rings_of(geom):
    """Exterior rings of a Polygon or MultiPolygon, as lon/lat lists.

    Interior rings (a lake inside a park) are dropped on purpose: this is an
    outline to recognise a place by, and a hole in it reads as a second park.
    """
    parts = getattr(geom, "geoms", [geom])
    out = []
    for part in parts:
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        ring = list(part.exterior.coords)
        if len(ring) >= 4:
            out.append(simplify_lonlat(ring, OUTLINE_SIMPLIFY_M))
    return out


def load_boroughs():
    """(names, prepared polygons, STRtree, per-borough view bounds).

    No border overlap buffer here, unlike the coverage map: a tree belongs
    to exactly one borough and a park outline that straddles a line is
    better drawn twice than clipped. Parks are tested by INTERSECTION, trees
    by containment.
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
        shapes.append(geom)
    return names, [prep(s) for s in shapes], STRtree(shapes), bounds


def collect_trees(names, prepared, tree_index):
    """Living, usable Forestry trees, bucketed by borough as [lat,lon,...].

    A FLAT array, not a list of pairs: 887k pairs of two-element lists cost
    roughly twice the JSON of one flat array of numbers, and the canvas layer
    walks it with a stride of 2 anyway.
    """
    rows = fetch_trees(config.CITY_BBOX, "citywide")
    per_borough = {name: [] for name in names}
    per_borough[CITYWIDE] = []
    kept = skipped = 0
    for row in rows:
        coords = (row.get("location") or {}).get("coordinates")
        if not coords:
            skipped += 1
            continue
        # The same test scoring applies, so a dot on this map means a tree
        # that can actually contribute shade -- not a stump or a dead trunk.
        if tree_value(row) is None:
            skipped += 1
            continue
        lon, lat = coords[0], coords[1]
        point = Point(lon, lat)
        for position in tree_index.query(point):
            if prepared[position].contains(point):
                pair = [round(lat, TREE_DP), round(lon, TREE_DP)]
                per_borough[names[position]] += pair
                per_borough[CITYWIDE] += pair
                kept += 1
                break
    return per_borough, kept, skipped


def collect_city_parks(names, shapes_tree, borough_shapes):
    """Production park mask outlines, bucketed by borough.

    _is_real_park() is the pipeline's own predicate, not a copy of it: this
    page has to show the mask that scoring would actually use, medians and
    traffic triangles already removed.
    """
    geojson = fetch_park_properties()
    per_borough = {name: [] for name in names}
    labels = {name: [] for name in names}
    per_borough[CITYWIDE], labels[CITYWIDE] = [], []
    kept = dropped = 0
    for feature in geojson["features"]:
        props = feature["properties"]
        if not _is_real_park(props):
            dropped += 1
            continue
        try:
            geom = make_valid(shape(feature["geometry"]))
        except Exception:
            dropped += 1
            continue
        if geom.is_empty:
            dropped += 1
            continue
        rings = rings_of(geom)
        if not rings:
            continue
        kept += 1
        label = (props.get("signname") or props.get("name311") or "").strip()
        drawn = [[[round(lat, OUTLINE_DP), round(lon, OUTLINE_DP)]
                  for lon, lat in ring] for ring in rings]
        per_borough[CITYWIDE] += drawn
        labels[CITYWIDE] += [label] * len(drawn)
        for position in shapes_tree.query(geom):
            if borough_shapes[position].intersects(geom):
                per_borough[names[position]] += [
                    [[round(lat, OUTLINE_DP), round(lon, OUTLINE_DP)]
                     for lon, lat in ring] for ring in rings]
                labels[names[position]] += [label] * len(rings)
    return per_borough, labels, kept, dropped


def _parkish(tags):
    """OSM tags that mean green land a person could be under a tree in.

    Deliberately wider than leisure=park: the parks Parks Properties misses
    are state and federal, and OSM tags those with boundary=protected_area
    (Gateway NRA), leisure=nature_reserve (Clay Pit Ponds) and natural=wood
    at least as often as leisure=park.
    """
    return (tags.get("leisure") in {"park", "nature_reserve", "garden"}
            or tags.get("boundary") == "protected_area"
            or tags.get("landuse") in {"recreation_ground", "forest"}
            or tags.get("natural") == "wood")


def collect_osm_only_parks(names, shapes_tree, borough_shapes, cache_path):
    """Parks OSM has that the CITY LIST DOES NOT, bucketed by borough.

    The expensive half (an area pass over the 472MB extract, ~85s) is cached
    as GeoJSON, because this changes only when the pinned .pbf changes.

    "Not in the city list" is <10% of the OSM park's area covered by the
    union of real city parks. Not zero: OSM and NYC draw the same park with
    different boundaries all the time, and a few percent of incidental
    overlap does not make Floyd Bennett Field a city park.
    """
    if cache_path.exists():
        features = json.loads(cache_path.read_text())["features"]
        geoms = [(f["properties"]["name"], shape(f["geometry"]))
                 for f in features]
    else:
        nyc = prep(nyc_boundary(fetch_borough_boundaries()))
        city_union = make_valid(unary_union([
            make_valid(shape(f["geometry"]))
            for f in fetch_park_properties()["features"]
            if _is_real_park(f["properties"])]))
        city = prep(city_union)

        processor = (osmium.FileProcessor(config.OSM_EXTRACT_PATH)
                     .with_areas()
                     .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
        geoms = []
        for area in processor:
            tags = dict(area.tags)
            name = tags.get("name")
            if not name or not _parkish(tags):
                continue
            try:
                parts = []
                for outer in area.outer_rings():
                    shell = [(node.lon, node.lat) for node in outer]
                    if len(shell) >= 4:
                        parts.append(Polygon(shell))
                if not parts:
                    continue
                geom = make_valid(unary_union(parts))
            except Exception:
                continue
            if geom.is_empty or not nyc.intersects(geom):
                continue
            # Area in degrees^2 -> hectares would be wrong; this is a
            # coarse size gate, so scale longitude by cos(lat) and use the
            # local metres-per-degree instead of a full reprojection.
            centre_lat = geom.centroid.y
            scale = math.cos(math.radians(centre_lat))
            approx_ha = (geom.area * scale
                         * M_PER_DEG_LAT ** 2) / 10_000.0
            if approx_ha < MIN_OSM_PARK_HA:
                continue
            if city.intersects(geom):
                try:
                    if geom.intersection(city_union).area / geom.area >= 0.10:
                        continue
                except Exception:
                    continue
            geoms.append((name, geom))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "properties": {"name": n},
                          "geometry": g.__geo_interface__}
                         for n, g in geoms]}))

    per_borough = {name: [] for name in names}
    labels = {name: [] for name in names}
    per_borough[CITYWIDE], labels[CITYWIDE] = [], []
    for name, geom in geoms:
        rings = rings_of(geom)
        if not rings:
            continue
        drawn = [[[round(lat, OUTLINE_DP), round(lon, OUTLINE_DP)]
                  for lon, lat in ring] for ring in rings]
        per_borough[CITYWIDE] += drawn
        labels[CITYWIDE] += [name] * len(drawn)
        for position in shapes_tree.query(geom):
            if borough_shapes[position].intersects(geom):
                per_borough[names[position]] += [
                    [[round(lat, OUTLINE_DP), round(lon, OUTLINE_DP)]
                     for lon, lat in ring] for ring in rings]
                labels[names[position]] += [name] * len(rings)
    return per_borough, labels, len(geoms)


HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ &mdash; tree coverage</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;background:#0b1410}
  #map{height:100%}
  .panel{position:absolute;z-index:500;left:12px;top:12px;width:300px;
    background:#0f1a15f2;color:#dcefe4;border:1px solid #2f5a45;
    padding:14px 16px;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
  .panel h1{font-size:14px;margin:0 0 2px;letter-spacing:.04em}
  .panel .sub{color:#7fae95;font-size:11px;margin:0 0 12px}
  .row{display:flex;align-items:center;gap:9px;padding:5px 0}
  .row label{flex:1;cursor:pointer}
  .row input[type=checkbox]{width:16px;height:16px;accent-color:#3ddc97;cursor:pointer}
  .sw{width:22px;height:0;border-top-width:3px;border-top-style:solid;flex:none}
  .dot{width:22px;flex:none;text-align:center;color:#3ddc97;font-size:15px;line-height:1}
  .n{color:#7fae95;font-size:10.5px;font-variant-numeric:tabular-nums}
  .fade{margin:10px 0 2px}
  .fade input{width:100%;accent-color:#3ddc97}
  .note{margin-top:13px;padding-top:11px;border-top:1px solid #2f5a45;
    color:#8dbba2;font-size:10.5px;line-height:1.5}
  .note b{color:#dcefe4}
  .hint{position:absolute;z-index:500;left:12px;bottom:12px;
    background:#0f1a15f2;color:#8dbba2;border:1px solid #2f5a45;
    padding:7px 11px;font:10.5px/1.4 ui-monospace,Menlo,monospace}
  .leaflet-container{background:#0b1410}
</style>
<div id="map"></div>
<div class="panel">
  <h1>__TITLE__</h1>
  <p class="sub">__STAMP__</p>
  <div class="row">
    <span class="dot">&#9679;</span>
    <label for="trees">Trees on record</label>
    <input type="checkbox" id="trees" checked>
  </div>
  <div class="n">__TREE_N__ trees &middot; Forestry hn5i-inap, living only</div>
  <div class="row">
    <span class="sw" style="border-top-color:#ffb02e"></span>
    <label for="city">City parks</label>
    <input type="checkbox" id="city" checked>
  </div>
  <div class="n">__CITY_N__ outlines &middot; the mask scoring would use</div>
  <div class="row">
    <span class="sw" style="border-top-color:#c77dff"></span>
    <label for="osm">Parks the city list misses</label>
    <input type="checkbox" id="osm" checked>
  </div>
  <div class="n">__OSM_N__ outlines &middot; state + federal, from OSM</div>
  <div class="fade">
    <div class="n">Imagery brightness</div>
    <input type="range" id="fade" min="15" max="100" value="100">
  </div>
  <div class="note">
    An outline with <b>no green inside it</b> is a park whose trees are not
    in the data we score from. Compare against what you can see in the
    imagery &mdash; and against parks you have walked.<br><br>
    <b>Nothing here is computed.</b> Trees are drawn where the dataset puts
    them; outlines are drawn as published. No rule decides what counts as
    a gap.
  </div>
</div>
<div class="hint">Click any outline for its name</div>
<script>
const TREES  = __TREES__;
const CITY   = __CITY__;
const OSM    = __OSM__;
const CITYL  = __CITYL__;
const OSML   = __OSML__;
const BOUNDS = __BOUNDS__;

const map = L.map('map', {preferCanvas: true, zoomControl: false});
L.control.zoom({position: 'bottomright'}).addTo(map);
map.fitBounds(BOUNDS);

// Deliberately on window: `const` at script top level is not a global, and
// both the render check that verifies this page and anyone poking at it in
// devtools need a handle on the map and its data.
window.__map = map;
window.__data = {trees: TREES, city: CITY, osm: OSM};

// maxNativeZoom lets the deepest real tile upscale past z19 instead of the
// layer returning nothing and painting grey squares -- the bug that hit
// web/src/components/MapView.tsx on 2026-08-24.
const sat = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxNativeZoom: 19, maxZoom: 21, attribution: 'Esri World Imagery'}).addTo(map);
const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxNativeZoom: 19, maxZoom: 21, attribution: '&copy; OpenStreetMap'});
L.control.layers({'Satellite': sat, 'Street map': street}, null,
  {collapsed: true, position: 'topright'}).addTo(map);

// One canvas for every tree. L.circleMarker would be one Leaflet object per
// tree and Queens carries ~200k of them; this draws the same pixels from a
// single object. Bounds-filtering before projecting is what makes a deep
// zoom cheap -- at borough zoom nothing filters and every tree projects.
const DotLayer = L.Layer.extend({
  initialize(flat, options) {
    // Float32Array, not a JS array: 1.8M plain numbers carry per-element
    // boxing overhead, and this one is walked in full on every zoom.
    this._flat = Float32Array.from(flat);
    this._pz = null;
    L.setOptions(this, options);
  },
  onAdd(map) {
    this._canvas = L.DomUtil.create('canvas', 'leaflet-layer');
    this._canvas.style.pointerEvents = 'none';
    map.getPanes().overlayPane.appendChild(this._canvas);
    map.on('move zoom viewreset resize', this._reset, this);
    this._reset();
    return this;
  },
  onRemove(map) {
    L.DomUtil.remove(this._canvas);
    map.off('move zoom viewreset resize', this._reset, this);
    return this;
  },
  // Project every tree ONCE per zoom level and keep the pixel coordinates.
  // Projection is zoom-dependent but pan-independent, so panning then costs
  // one subtraction per tree instead of a full coordinate transform. That
  // is the difference between the citywide page being usable and not:
  // 887k transforms per pan is ~380ms, 887k subtractions is a few ms.
  _project() {
    const map = this._map, z = map.getZoom();
    if (this._pz === z) return;
    const flat = this._flat, n = flat.length / 2;
    const xy = new Float32Array(n * 2);
    for (let i = 0; i < n; i++) {
      const p = map.project([flat[i * 2], flat[i * 2 + 1]], z);
      xy[i * 2] = p.x; xy[i * 2 + 1] = p.y;
    }
    this._xy = xy; this._pz = z;
  },
  _reset() {
    const map = this._map, size = map.getSize();
    L.DomUtil.setPosition(this._canvas, map.containerPointToLayerPoint([0, 0]));
    if (this._canvas.width !== size.x)  this._canvas.width  = size.x;
    if (this._canvas.height !== size.y) this._canvas.height = size.y;
    this._draw();
  },
  _draw() {
    const map = this._map;
    this._project();
    const xy = this._xy, ctx = this._canvas.getContext('2d');
    const w = this._canvas.width, h = this._canvas.height;
    ctx.clearRect(0, 0, w, h);
    const z = map.getZoom();
    const r = z >= 18 ? 3.2 : z >= 17 ? 2.4 : z >= 16 ? 1.7 : z >= 14 ? 1.2 : 0.8;
    // Pixel origin of the current view: container x = projected x - origin.
    const origin = map.getPixelBounds().min;
    const ox = origin.x, oy = origin.y;
    ctx.fillStyle = '#3ddc97';
    // A sub-1.5px circle is indistinguishable from a square of the same
    // size and costs a fraction as much to rasterise, so only the dots big
    // enough to read as round get an arc().
    const square = r < 1.5, d = r * 2;
    for (let i = 0; i < xy.length; i += 2) {
      const x = xy[i] - ox, y = xy[i + 1] - oy;
      if (x < -4 || y < -4 || x > w + 4 || y > h + 4) continue;
      if (square) { ctx.fillRect(x, y, d, d); }
      else { ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill(); }
    }
  }
});

const treeLayer = new DotLayer(TREES).addTo(map);

function outlines(rings, labels, colour) {
  const group = L.layerGroup();
  for (let i = 0; i < rings.length; i++) {
    const poly = L.polygon(rings[i], {color: colour, weight: 1.7, opacity: .95,
                                      fill: true, fillOpacity: .04});
    const name = labels[i] || '(unnamed)';
    poly.bindTooltip(name, {sticky: true});
    group.addLayer(poly);
  }
  return group;
}
const cityLayer = outlines(CITY, CITYL, '#ffb02e').addTo(map);
const osmLayer  = outlines(OSM,  OSML,  '#c77dff').addTo(map);

function bind(id, layer) {
  document.getElementById(id).addEventListener('change', e => {
    e.target.checked ? layer.addTo(map) : map.removeLayer(layer);
  });
}
bind('trees', treeLayer);
bind('city', cityLayer);
bind('osm', osmLayer);

document.getElementById('fade').addEventListener('input', e => {
  const v = e.target.value / 100;
  for (const el of document.querySelectorAll('.leaflet-tile-pane'))
    el.style.opacity = v;
});
</script>
"""


def write_map(out_dir, borough, trees, city, city_labels, osm, osm_labels,
              bounds, stamp):
    """One self-contained page. `bounds` frames the BOROUGH, not the data:
    a park outline straddling a border belongs in both files but must not
    set the view, or the map opens zoomed out to nothing."""
    page = (HTML
            .replace("__TITLE__", borough)
            .replace("__STAMP__", stamp)
            .replace("__TREE_N__", f"{len(trees) // 2:,}")
            .replace("__CITY_N__", f"{len(city):,}")
            .replace("__OSM_N__", f"{len(osm):,}")
            .replace("__BOUNDS__", json.dumps(bounds))
            .replace("__TREES__", json.dumps(trees, separators=(",", ":")))
            .replace("__CITY__", json.dumps(city, separators=(",", ":")))
            .replace("__OSM__", json.dumps(osm, separators=(",", ":")))
            .replace("__CITYL__", json.dumps(city_labels, separators=(",", ":")))
            .replace("__OSML__", json.dumps(osm_labels, separators=(",", ":"))))
    path = out_dir / f"{borough.lower().replace(' ', '_')}.html"
    path.write_text(page)
    return path, len(page)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/audits/tree_maps",
                        help="output directory (undated on purpose)")
    parser.add_argument("--borough", action="append",
                        help="limit to one borough; repeatable")
    args = parser.parse_args()

    started = time.perf_counter()
    out_dir = config.REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"built {date.today().isoformat()}"

    print("[map] boroughs")
    names, prepared, tree_index, bounds = load_boroughs()
    raw_shapes = [shape(f["geometry"])
                  for f in fetch_borough_boundaries()["features"]]
    shapes_tree = STRtree(raw_shapes)

    print("[map] trees")
    trees, kept, skipped = collect_trees(names, prepared, tree_index)
    print(f"      {kept:,} drawn, {skipped:,} skipped (dead or no location)")

    print("[map] city parks")
    city, city_labels, n_city, n_drop = collect_city_parks(
        names, shapes_tree, raw_shapes)
    print(f"      {n_city:,} real parks, {n_drop:,} excluded "
          f"(medians, slivers, non-park)")

    print("[map] parks the city list misses (OSM areas)")
    osm, osm_labels, n_osm = collect_osm_only_parks(
        names, shapes_tree, raw_shapes, out_dir / "osm_only_parks.geojson")
    print(f"      {n_osm:,} parks >= {MIN_OSM_PARK_HA} ha absent from the "
          f"city list")

    # Citywide view bounds: the envelope of the five borough envelopes.
    all_lat = [b[i][0] for b in bounds.values() for i in (0, 1)]
    all_lon = [b[i][1] for b in bounds.values() for i in (0, 1)]
    bounds[CITYWIDE] = [[min(all_lat), min(all_lon)],
                        [max(all_lat), max(all_lon)]]

    wanted = args.borough or names + [CITYWIDE]
    print()
    for borough in names + [CITYWIDE]:
        if borough not in wanted:
            continue
        path, size = write_map(out_dir, borough, trees[borough],
                               city[borough], city_labels[borough],
                               osm[borough], osm_labels[borough],
                               bounds[borough], stamp)
        print(f"  {borough:<16}{len(trees[borough]) // 2:>8,} trees  "
              f"{len(city[borough]):>5} city  {len(osm[borough]):>4} osm  "
              f"{size / 1e6:>6.1f} MB  {path.name}")

    print(f"\n[map] done in {time.perf_counter() - started:.0f}s -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
