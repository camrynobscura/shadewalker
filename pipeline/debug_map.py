"""Render an exported tile as an interactive HTML map for eyeball verification.

    uv run python -m pipeline.debug_map pilot
    → data/debug/pilot.html  (open in a browser)

Streets are colored by tree density (grey = bare → deep green = lush) and
every scoreable tree is a small dot. If the coloring matches your knowledge
of the neighborhood — leafy brownstone blocks green, industrial Gowanus
grey — the pipeline's math survived contact with reality.

folium is a Python wrapper around Leaflet (the same map library the real
frontend will use) — dev-only dependency, not part of the shipped app.
"""

import logging
import argparse
import gzip
import json

import folium

from pipeline import config
from pipeline.fetch import trees as tree_fetch

logger = logging.getLogger(__name__)


DEBUG_DIR = config.DATA_DIR / "debug"

# Density thresholds (tree value per meter) → color. Calibrated from the
# pilot tile's distribution: median ≈ 0.011, p90 ≈ 0.048.
DENSITY_COLORS = [
    (0.06, "#1a9850"),   # lush
    (0.03, "#66bd63"),
    (0.015, "#a6d96a"),
    (0.005, "#d9ef8b"),
    (0.0, "#aaaaaa"),    # bare
]


def color_for_density(density: float) -> str:
    for threshold, color in DENSITY_COLORS:
        if density >= threshold:
            return color
    return DENSITY_COLORS[-1][1]


def render(tile_id: str) -> None:
    tile_path = config.TILES_DIR / f"{tile_id}.json.gz"
    tile = json.loads(gzip.open(tile_path, "rt").read())

    bbox = config.get_tile_bbox(tile_id)
    center = [(bbox.lat_min + bbox.lat_max) / 2, (bbox.lon_min + bbox.lon_max) / 2]
    # prefer_canvas draws with <canvas> instead of one SVG element per shape —
    # the difference between smooth and unusable at 10k+ dots.
    fmap = folium.Map(location=center, zoom_start=15, tiles="cartodbpositron",
                      prefer_canvas=True)

    # Streets, colored by density. folium wants [lat, lon] (Leaflet order);
    # our file stores [lon, lat] (GeoJSON order) — flip each pair.
    for edge in tile["edges"]:
        tree_total = edge["tree_deciduous"] + edge["tree_evergreen"]
        density = tree_total / max(edge["length_m"], 1)
        folium.PolyLine(
            locations=[[lat, lon] for lon, lat in edge["coords"]],
            color=color_for_density(density),
            weight=4,
            opacity=0.9,
            tooltip=f"{edge['name'] or '(unnamed)'} — {edge['tree_count']} trees, "
                    f"density {density:.3f}",
        ).add_to(fmap)

    # Trees from the raw cache, as small dots.
    for row in tree_fetch.fetch_trees(bbox, tile_id):
        lon, lat = row["location"]["coordinates"]
        folium.CircleMarker(
            location=[lat, lon], radius=1.5,
            color="#2b7a2b", fill=True, fill_opacity=0.5, weight=0,
        ).add_to(fmap)

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DEBUG_DIR / f"{tile_id}.html"
    fmap.save(str(out_path))
    logger.info(f"  [debug] map written to {out_path.relative_to(config.REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a tile as an HTML debug map.")
    parser.add_argument("tile_id")
    args = parser.parse_args()
    render(args.tile_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
