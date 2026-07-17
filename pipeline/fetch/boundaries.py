"""Fetch NYC's real borough-boundary polygons: the Borough Boundaries
dataset (Socrata id in config.BOUNDARIES_DATASET_ID), one MultiPolygon
feature per borough with water areas already excluded by the source. Used
to keep foreign territory (Jersey City, Bayonne) out of the pipeline's
data entirely -- replacing the hand-picked rectangles' overreach (see
PLAN.md's Stage 2 section, "Borough-boundary polygon + pruning-rule
change").

Only 5 rows, so unlike trees.py this needs no pagination: one direct
GeoJSON export request, cached whole rather than through socrata.py's
paginated fetch_all_rows.
"""

import json

import requests

from pipeline import config
from pipeline.fetch import socrata

CACHE_PATH = config.RAW_DIR / "socrata" / "borough_boundaries.geojson"


def fetch_borough_boundaries(refresh: bool = False) -> dict:
    """Return the raw GeoJSON FeatureCollection: one feature per borough,
    `boroname`/`borocode` properties, `geometry` a MultiPolygon."""
    if CACHE_PATH.exists() and not refresh:
        geojson = json.loads(CACHE_PATH.read_text())
        print(f"  [boundaries] {len(geojson['features'])} boroughs (cached)")
        return geojson

    url = f"{config.SOCRATA_BASE_URL}/{config.BOUNDARIES_DATASET_ID}.geojson"
    response = requests.get(url, headers=socrata.auth_headers(), timeout=60)
    response.raise_for_status()
    geojson = response.json()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson))
    print(f"  [boundaries] {len(geojson['features'])} boroughs (downloaded + cached)")
    return geojson
