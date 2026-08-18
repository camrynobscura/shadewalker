"""Fetch NYC Parks' own Trails dataset (Socrata id in
config.PARK_TRAILS_DATASET_ID) -- real, official park-interior trails,
some of which don't exist in OSM at all yet (FIXES.md item 1g).

~7,058 rows -- small enough for one direct GeoJSON export request, cached
whole rather than through socrata.py's paginated fetch_all_rows (same
reasoning as pipeline/fetch/parks.py). The GeoJSON export endpoint
silently caps at 1,000 rows without an explicit $limit -- confirmed live
on parks.py's own dataset -- so PAGE_LIMIT must stay comfortably above
this dataset's real row count too, not just "big enough to look safe".
"""

import logging
import json

import requests

from pipeline import config
from pipeline.fetch import socrata

logger = logging.getLogger(__name__)


CACHE_PATH = config.RAW_DIR / "socrata" / f"park_trails_{config.PARK_TRAILS_DATASET_ID}.geojson"
PAGE_LIMIT = 10_000


def fetch_park_trails(refresh: bool = False) -> dict:
    """Return the raw GeoJSON FeatureCollection: one feature per trail
    segment, `class`/`trail_name`/`park_name` properties among others,
    `geometry` a LineString or MultiLineString."""
    if CACHE_PATH.exists() and not refresh:
        geojson = json.loads(CACHE_PATH.read_text())
        logger.info(f"  [park_trails] {len(geojson['features'])} segments (cached)")
        return geojson

    url = f"{config.SOCRATA_BASE_URL}/{config.PARK_TRAILS_DATASET_ID}.geojson?$limit={PAGE_LIMIT}"
    response = requests.get(url, headers=socrata.auth_headers(), timeout=60)
    response.raise_for_status()
    geojson = response.json()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson))
    logger.info(f"  [park_trails] {len(geojson['features'])} segments (downloaded + cached)")
    return geojson
