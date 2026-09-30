"""Fetch NYC's real borough-boundary polygons: the Borough Boundaries
(water areas included) dataset (Socrata id in config.BOUNDARIES_DATASET_ID),
one MultiPolygon feature per borough. Used to keep foreign territory
(Jersey City, Bayonne) out of the pipeline's data entirely.

Deliberately the water-included sibling dataset, not the water-excluded
one that shares the same schema: a bridge's midspan sits directly over
water, and pipeline/graph/pedestrian.py drops any way that never
touches this polygon, so the water-excluded version severs every
inter-borough bridge crossing. This version's jurisdiction still stops at
the real state line (verified against the NJ side of the George
Washington Bridge and mid-Hudson River), it just also covers NYC's own
rivers between its boroughs.

Only 5 rows, so unlike trees.py this needs no pagination: one direct
GeoJSON export request, cached whole rather than through socrata.py's
paginated fetch_all_rows.
"""

import logging
import json

from pipeline import config
from pipeline.fetch import socrata

logger = logging.getLogger(__name__)


# Filename keyed to the dataset id so switching datasets can't silently
# keep serving a stale cache from the old one.
CACHE_PATH = config.RAW_DIR / "socrata" / f"borough_boundaries_{config.BOUNDARIES_DATASET_ID}.geojson"


def fetch_borough_boundaries(refresh: bool = False) -> dict:
    """Return the raw GeoJSON FeatureCollection: one feature per borough,
    `boroname`/`borocode` properties, `geometry` a MultiPolygon."""
    if CACHE_PATH.exists() and not refresh:
        geojson = json.loads(CACHE_PATH.read_text())
        logger.info(f"  [boundaries] {len(geojson['features'])} boroughs (cached)")
        return geojson

    url = f"{config.SOCRATA_BASE_URL}/{config.BOUNDARIES_DATASET_ID}.geojson"
    # socrata.get_with_retry, not a bare requests.get: one transient blip
    # must not kill a whole pipeline run.
    response = socrata.get_with_retry(url, headers=socrata.auth_headers())
    geojson = response.json()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson))
    logger.info(f"  [boundaries] {len(geojson['features'])} boroughs (downloaded + cached)")
    return geojson
