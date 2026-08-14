"""Fetch NYC's Interior Sidewalk Centerline data (ArcGIS FeatureServer,
feature code 3810) -- the off-right-of-way walking paths inside parks,
NYCHA campuses, hospital/school campuses, and ordinary residential
complexes that ANY_SIDEWALK_FILTER's park-only rescue (pipeline/fetch/
streets.py) misses (FIXES.md item 1a).

~43,843 segments citywide -- over the FeatureServer's own 2,000-row
maxRecordCount cap (confirmed live), so fetching everything needs real
pagination (resultOffset), unlike parks.py's single-request whole-city
fetch. Cached whole afterwards, same reasoning as parks.py: this is a
stable, infrequently-updated survey product, not something to re-query
per tile.

The source data is natively EPSG:2263 (State Plane feet); outSR=4326
below asks the server to reproject to lon/lat itself, avoiding a separate
pyproj step for this one dataset -- confirmed this works against the live
API.
"""

import json

import requests

from pipeline import config

CACHE_PATH = config.RAW_DIR / "arcgis" / "interior_sidewalks_2022.geojson"
FEATURE_SERVER_URL = (
    "https://services6.arcgis.com/yG5s3afENB5iO9fj/arcgis/rest/services/"
    "Sidewalk_Line_2022/FeatureServer/23/query"
)
PAGE_SIZE = 2000  # the server's own maxRecordCount cap -- confirmed live


def fetch_interior_sidewalks(refresh: bool = False) -> dict:
    """Return the raw GeoJSON FeatureCollection: one feature per interior
    sidewalk centerline segment, geometry a LineString already in
    EPSG:4326."""
    if CACHE_PATH.exists() and not refresh:
        geojson = json.loads(CACHE_PATH.read_text())
        print(f"  [interior_sidewalks] {len(geojson['features'])} segments (cached)")
        return geojson

    features = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        response = requests.get(FEATURE_SERVER_URL, params=params, timeout=60)
        response.raise_for_status()
        page = response.json()
        page_features = page.get("features", [])
        features.extend(page_features)
        if len(page_features) < PAGE_SIZE:
            break
        offset += len(page_features)

    geojson = {"type": "FeatureCollection", "features": features}
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson))
    print(f"  [interior_sidewalks] {len(features)} segments (downloaded + cached)")
    return geojson
