"""Fetch NYC's real park-property polygons: the Parks Properties dataset
(Socrata id in config.PARKS_DATASET_ID), one polygon per NYC Parks
property with a `typecategory` field. Used to build the park canopy mask
(see PLAN.md's Park-canopy section) -- attributing a park's interior
canopy to nearby park-edge streets requires knowing where parks actually
are, not just guessing from street names.

~2,059 rows -- small enough for one direct GeoJSON export request, cached
whole rather than through socrata.py's paginated fetch_all_rows (same
reasoning as pipeline/fetch/boundaries.py). The GeoJSON export endpoint
silently caps at 1,000 rows without an explicit $limit -- confirmed live
(1,000 vs the real 2,059), so PAGE_LIMIT must stay comfortably above the
dataset's real row count, not just "big enough to look safe".
"""

import json

import requests

from pipeline import config
from pipeline.fetch import socrata

CACHE_PATH = config.RAW_DIR / "socrata" / f"parks_properties_{config.PARKS_DATASET_ID}.geojson"
PAGE_LIMIT = 10_000


def fetch_park_properties(refresh: bool = False) -> dict:
    """Return the raw GeoJSON FeatureCollection: one feature per park
    property, `typecategory`/`name311` properties among others, `geometry`
    a Polygon or MultiPolygon."""
    if CACHE_PATH.exists() and not refresh:
        geojson = json.loads(CACHE_PATH.read_text())
        print(f"  [parks] {len(geojson['features'])} properties (cached)")
        return geojson

    url = f"{config.SOCRATA_BASE_URL}/{config.PARKS_DATASET_ID}.geojson?$limit={PAGE_LIMIT}"
    response = requests.get(url, headers=socrata.auth_headers(), timeout=60)
    response.raise_for_status()
    geojson = response.json()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson))
    print(f"  [parks] {len(geojson['features'])} properties (downloaded + cached)")
    return geojson
