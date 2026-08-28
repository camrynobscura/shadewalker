"""Forestry-vs-raster gap census over sidewalks, per neighborhood.

THE QUESTION (user, 2026-08-27)
-------------------------------
How much real canopy sits over sidewalks that the public tree dataset
cannot account for -- private front-yard trees especially? For every
sidewalk edge inside each neighborhood box, two readings over the SAME
2m walker strip:

  displayed  what the app shows today: min((ev + dec x month_factor) /
             length / DENSITY_AT_FULL_COVERAGE, 1) at peak month --
             the server's own formula, server/graph_store.py:761-764.
  measured   what the 2021 land-cover raster sees: production
             canopy.leaf_fraction, the instrument the display scale was
             calibrated against (tools/audit/fit_exchange_rate.py).

gap = measured - displayed. Positive = shade the tree list can't see.

VALIDATE BEFORE TRUSTING (the instrument rule): against the 2026-08-27
production export this must reproduce, length-weighted:

  Carroll Gardens    881 edges / 29.6 km   displayed 43.3  raster 41.0
  Greenwich Village  442 edges / 20.5 km   displayed 46.8  raster 36.4
  Bed-Stuy           755 edges / 33.0 km   displayed 48.8  raster 47.3

WHAT THE 2026-08-27 RUN FOUND (full account:
history/garden-canopy-gap.md; decision: deferred until the successor
raster -- see REFETCH.md's raster watch):

- Neighborhood NETS are small (-2.2 / -10.4 / -1.5 pts) but hide two
  cancelling errors, same signature in all three boxes: garden blocks
  displaying ~34% where the raster sees ~53% (17-19% of brownstone
  sidewalk length understated >10 pts), and tree-packed blocks
  displaying 75-95% where the raster sees 55-75% (the linear
  trunk-inches -> canopy exchange rate over-pays overlapping crowns;
  Greenwich Village, all-public small-crowned pit trees, shows the
  overshoot unmasked).
- The decile cut below IS the diagnostic: overstatement concentrating
  in the top displayed deciles is the exchange-rate signature, not a
  neighborhood fact.

The per-edge map (data/audits/<date>/private_canopy_gap_map.html) colors
every edge by its gap; popups carry both numbers.

    uv run python tools/audit/measure_private_canopy_gap.py
"""

import argparse
import gzip
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.scoring import canopy  # noqa: E402
from pipeline.scoring.blocks import SHADED_KINDS  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("gap")

# (lat_min, lat_max, lon_min, lon_max) -- tight neighborhood cores,
# chosen 2026-08-27 to exclude parks (Von King and Fulton Park sit just
# outside the Bed-Stuy box, Washington Square just south of the Village
# one). Greenwich Village is the control: heavy canopy, essentially all
# of it public street trees, buildings at the lot line -- a fair
# instrument reads ~zero gap there. It read -10.4 instead, which is how
# the exchange-rate overshoot was caught.
NEIGHBORHOODS = {
    "Carroll Gardens": (40.6735, 40.6845, -74.0035, -73.9915),
    "Greenwich Village": (40.7320, 40.7385, -74.0080, -73.9960),
    "Bed-Stuy": (40.6795, 40.6870, -73.9530, -73.9330),
}
PEAK_MONTH_FACTOR = 1.0   # config.CANOPY_BY_MONTH, June-August

_to_raster = Transformer.from_crs(
    "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform


def inside(coords, box):
    lat_min, lat_max, lon_min, lon_max = box
    return all(lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
               for lon, lat in coords)


def displayed_coverage(rec):
    density = ((rec["tree_evergreen"] + rec["tree_deciduous"]
                * PEAK_MONTH_FACTOR) / rec["length_m"])
    return min(density / config.DENSITY_AT_FULL_COVERAGE, 1.0)


def weighted_percentile(rows, q):
    rows = sorted(rows, key=lambda r: r["gap"])
    target = sum(r["length_m"] for r in rows) * q
    acc = 0.0
    for r in rows:
        acc += r["length_m"]
        if acc >= target:
            return r["gap"]
    return rows[-1]["gap"]


def census(src, graph_edges, scores):
    per_hood = {name: [] for name in NEIGHBORHOODS}
    tallies = {name: {"fallback": 0, "no_reading": 0}
               for name in NEIGHBORHOODS}
    for edge in graph_edges:
        if edge.get("kind") not in SHADED_KINDS:
            continue
        rec = scores.get((edge["u"], edge["v"], int(edge["key"])))
        if rec is None:
            continue
        for name, box in NEIGHBORHOODS.items():
            if not inside(rec["coords"], box):
                continue
            # Raster-fallback edges (tree_park_canopy) already carry the
            # raster's own answer -- a gap of zero by construction.
            if rec.get("tree_park_canopy"):
                tallies[name]["fallback"] += 1
                break
            raster = canopy.leaf_fraction(src, rec["coords"], _to_raster)
            if raster is None:
                tallies[name]["no_reading"] += 1
                break
            disp = displayed_coverage(rec)
            per_hood[name].append({
                "coords": rec["coords"], "length_m": rec["length_m"],
                "displayed": round(disp, 4), "raster": round(raster, 4),
                "gap": round(raster - disp, 4),
            })
            break   # the boxes don't overlap
    return per_hood, tallies


def report(per_hood, tallies):
    print(f"\n(peak month factor {PEAK_MONTH_FACTOR}; rate "
          f"{config.DENSITY_AT_FULL_COVERAGE}; strip "
          f"{config.CANOPY_SAMPLE_STRIP_M}m; all figures length-weighted)")
    for name, rows in per_hood.items():
        t = tallies[name]
        total_len = sum(r["length_m"] for r in rows)
        wmean = lambda key: (sum(r[key] * r["length_m"] for r in rows)  # noqa: E731
                             / total_len)
        print(f"\n=== {name} {NEIGHBORHOODS[name]} ===")
        print(f"  {len(rows)} sidewalk edges / {total_len / 1000:.1f} km "
              f"({t['fallback']} raster-fallback and {t['no_reading']} "
              f"no-reading edges excluded)")
        print(f"  displayed {wmean('displayed') * 100:5.1f}%   "
              f"raster {wmean('raster') * 100:5.1f}%   "
              f"gap {wmean('gap') * 100:+5.1f} pts")
        pcts = {q: weighted_percentile(rows, q)
                for q in (0.10, 0.25, 0.50, 0.75, 0.90)}
        print("  gap percentiles: "
              + "  ".join(f"p{int(q * 100)} {v * 100:+.0f}"
                          for q, v in pcts.items()))
        over10 = sum(r["length_m"] for r in rows if r["gap"] > 0.10)
        over20 = sum(r["length_m"] for r in rows if r["gap"] > 0.20)
        under10 = sum(r["length_m"] for r in rows if r["gap"] < -0.10)
        print(f"  understated >10 pts: {over10 / total_len * 100:.0f}% of "
              f"length   >20 pts: {over20 / total_len * 100:.0f}%   "
              f"overstated >10 pts: {under10 / total_len * 100:.0f}%")
        print("  displayed-decile   km   mean displayed  mean raster   gap")
        for lo in range(0, 10):
            sub = [r for r in rows
                   if lo / 10 <= r["displayed"] < (lo + 1) / 10]
            if not sub:
                continue
            wl = sum(r["length_m"] for r in sub)
            d = sum(r["displayed"] * r["length_m"] for r in sub) / wl
            ra = sum(r["raster"] * r["length_m"] for r in sub) / wl
            print(f"  {lo / 10:.1f}-{(lo + 1) / 10:.1f}  {wl / 1000:9.1f}"
                  f"     {d * 100:5.1f}%       {ra * 100:5.1f}%   "
                  f"{(ra - d) * 100:+5.1f}")


def write_map(per_hood, out_path: Path):
    """Standalone Leaflet page, every edge colored by its gap. A local
    file for a browser, not an Artifact -- CDN assets are fine here."""
    data = {name: [{"c": [[round(lat, 6), round(lon, 6)]
                          for lon, lat in r["coords"]],
                    "d": r["displayed"], "r": r["raster"], "g": r["gap"]}
                   for r in rows]
            for name, rows in per_hood.items()}
    centers = {name: [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2]
               for name, b in NEIGHBORHOODS.items()}
    buttons = "".join(
        f"<button onclick=\"map.setView(CENTERS['{name}'], 16)\">"
        f"{name}</button>" for name in NEIGHBORHOODS)
    html = _MAP_TEMPLATE.replace("__DATA__", json.dumps(data)) \
                        .replace("__CENTERS__", json.dumps(centers)) \
                        .replace("__BUTTONS__", buttons)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    logger.info(f"[save] {out_path}")


_MAP_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>Displayed vs raster canopy gap</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0 }
  .bar { position: absolute; top: 10px; right: 10px; z-index: 1000;
         background: #fff; padding: 8px 10px; font: 13px/1.6 monospace;
         border: 1px solid #888 }
  .bar button { display: block; width: 100%; margin: 2px 0; font: inherit }
  .swatch { display: inline-block; width: 14px; height: 8px;
            margin-right: 4px }
</style></head><body><div id="map"></div>
<div class="bar">
  <b>gap = raster &minus; displayed</b><br>
  <span class="swatch" style="background:#2166ac"></span>overstated &gt;5<br>
  <span class="swatch" style="background:#bbb"></span>&plusmn;5 pts<br>
  <span class="swatch" style="background:#fdae61"></span>+5&ndash;15 pts<br>
  <span class="swatch" style="background:#d7301f"></span>+15&ndash;30 pts<br>
  <span class="swatch" style="background:#7f0000"></span>&gt;+30 pts<br>
  <hr>__BUTTONS__
</div>
<script>
const DATA = __DATA__;
const CENTERS = __CENTERS__;
const map = L.map('map').setView(CENTERS['Carroll Gardens'], 16);
// detectRetina off + explicit zoom ceiling: the grey-squares trap
// (web/CLAUDE.md) -- never ask a tile host for zooms it does not serve.
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxNativeZoom: 19, maxZoom: 19, detectRetina: false,
  attribution: '&copy; OpenStreetMap'
}).addTo(map);
function color(g) {
  if (g < -0.05) return '#2166ac';
  if (g <= 0.05) return '#bbb';
  if (g <= 0.15) return '#fdae61';
  if (g <= 0.30) return '#d7301f';
  return '#7f0000';
}
for (const [hood, rows] of Object.entries(DATA)) {
  for (const row of rows) {
    L.polyline(row.c, {color: color(row.g), weight: 4, opacity: 0.85})
      .bindPopup(`${hood}<br>displayed ${(row.d * 100).toFixed(0)}%` +
                 `<br>raster (2021) ${(row.r * 100).toFixed(0)}%` +
                 `<br>gap ${(row.g * 100) > 0 ? '+' : ''}` +
                 `${(row.g * 100).toFixed(0)} pts`)
      .addTo(map);
  }
}
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map-out", type=Path,
        default=Path(REPO) / "data" / "audits" / "2026-08-27"
        / "private_canopy_gap_map.html")
    args = parser.parse_args()

    t0 = time.monotonic()
    export_path = config.EXPORT_DIR / "citywide.json.gz"
    logger.info(f"[load] export: {export_path}")
    with gzip.open(export_path, "rt") as fh:
        payload = json.load(fh)
    scores = {(e["u"], e["v"], e["key"]): e for e in payload["edges"]}

    logger.info("[load] OSM read for kind (~2.5 min; the export drops it)")
    from pipeline.fetch.boundaries import fetch_borough_boundaries
    from pipeline.graph import pedestrian
    from pipeline.graph.boundary import nyc_boundary
    nyc_shape = nyc_boundary(fetch_borough_boundaries())
    ped_ways, _ = pedestrian.read_ways(config.OSM_EXTRACT_PATH, nyc_shape)
    _, graph_edges = pedestrian.build_graph(ped_ways)

    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        per_hood, tallies = census(src, graph_edges, scores)
    report(per_hood, tallies)
    write_map(per_hood, args.map_out)
    logger.info(f"[done] {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
