"""Map the places a sidewalk-only model could not route.

Draws measure_sidewalk_only_coverage.py's gap cells over satellite or
street tiles so the holes can be judged by eye rather than by percentage.

  ISLAND  pedestrian ways exist but are cut off from the main network
  HOLE    walkable street frontage, no pedestrian ways at all

Usage:
  uv run python tools/audit/build_coverage_gap_map.py
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

GAPS = "data/audits/2026-08-22/sidewalk_only_gaps.geojson"

HTML = """<!doctype html>
<meta charset="utf-8">
<title>Sidewalk coverage gaps</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%}
  #map{height:100%}
  .legend{position:absolute;z-index:500;right:10px;bottom:26px;background:#fffe;
    padding:10px 13px;border-radius:6px;box-shadow:0 1px 6px #0003;
    font:13px/1.7 ui-sans-serif,system-ui,sans-serif}
  .legend i{display:inline-block;width:14px;height:14px;vertical-align:-2px;
    margin-right:8px;opacity:.75}
  .hd{position:absolute;z-index:500;left:10px;top:10px;background:#fffe;
    padding:10px 14px;border-radius:6px;box-shadow:0 1px 6px #0003;
    font:13px/1.5 ui-sans-serif,system-ui,sans-serif;max-width:330px}
  .hd b{font-size:14px}
</style>
<div id="map"></div>
<div class="hd"><b>Where a sidewalk-only model can&rsquo;t route</b><br>
  Four boroughs, 250m cells. Everything unshaded is fine.
  <span id="counts"></span></div>
<div class="legend">
  <i style="background:#f59e0b"></i>ISLAND &mdash; paths exist, cut off<br>
  <i style="background:#dc2626"></i>HOLE &mdash; street, but no paths
</div>
<script>
const GAPS = __GAPS__;
const map = L.map('map').setView([40.72,-73.94], 11);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:19, attribution:'Esri'}).addTo(map);
const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap'});
const layers = {ISLAND:L.layerGroup(), HOLE:L.layerGroup()};
let n = {ISLAND:0, HOLE:0};
L.geoJSON(GAPS, {style:f=>({
    color: f.properties.verdict==='HOLE' ? '#dc2626' : '#f59e0b',
    weight:0, fillOpacity:.55}),
  onEachFeature:(f,l)=>{ n[f.properties.verdict]++; l.addTo(layers[f.properties.verdict]); }});
layers.ISLAND.addTo(map); layers.HOLE.addTo(map);
L.control.layers({'Satellite':map._layers[Object.keys(map._layers)[0]],'Street map':street},
  {'Islands':layers.ISLAND,'Holes':layers.HOLE},{collapsed:false}).addTo(map);
document.getElementById('counts').innerHTML =
  `<br><br>${n.HOLE} holes &middot; ${n.ISLAND} islands`;
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default="data/audits/2026-08-22/coverage_gap_map.html")
    args = ap.parse_args()
    gaps = json.load(open(os.path.join(REPO, GAPS)))
    path = os.path.join(REPO, args.out)
    with open(path, "w") as fh:
        fh.write(HTML.replace("__GAPS__", json.dumps(gaps)))
    print(f"wrote {args.out} ({len(gaps['features']):,} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
