"""NYC Building Footprints (Socrata `5zhs-2jue`) -- the building-shade layer's
outlines and roof heights.

Two jobs, kept apart:

  - `load()`: every row of the dataset, cached. Mirrors planimetrics.py:
    paged on `$order=:id` (the only sort Socrata guarantees stable for a
    dataset with no unique numeric key), `$select`ed down to the eight
    columns the shade step reads (the full row is ~3x wider), gzipped on
    disk under data/raw/socrata/, written atomically. The cache is the raw
    download, so a rule change below never needs a re-fetch.
  - `usable()`: the rows that cast shade -- the height cap, the excluded
    statuses and codes, and non-positive heights applied, with one tally
    line so a build log says what was dropped and why. Rows stay raw
    dicts (heights are strings, the_geom is GeoJSON); the shade step
    parses them with `float()` and `shapely.geometry.shape`, the pattern
    blockface.py uses for the kerb lines.

The counts behind every rule are on the constants in pipeline/config.py.
"""

import gzip
import json
import logging
import time

from pipeline import config
from pipeline.fetch.socrata import get_with_retry

logger = logging.getLogger(__name__)

ROOT = "https://data.cityofnewyork.us/resource"

# The planimetrics page, for the same reason: one citywide layer, fewer
# round trips beat a small page.
PAGE = 25_000

SELECT = ",".join([
    "the_geom",
    "bin",
    "base_bbl",
    "feature_code",
    "height_roof",
    "ground_elevation",
    "last_status_type",
    "last_edited_date",
])


def cache_path():
    return (config.RAW_DIR / "socrata"
            / f"buildings_v{config.BUILDINGS_CACHE_VERSION}.json.gz")


def fetch_all() -> list[dict]:
    """Every row, paged, selected columns only."""
    rows: list[dict] = []
    offset = 0
    started = time.perf_counter()
    while True:
        response = get_with_retry(
            f"{ROOT}/{config.BUILDINGS_DATASET_ID}.json",
            params={"$select": SELECT, "$limit": PAGE, "$offset": offset,
                    "$order": ":id"},
        )
        page = response.json()
        rows.extend(page)
        logger.info(f"  [buildings] {len(rows):,} rows "
                    f"({time.perf_counter() - started:.0f}s)")
        if len(page) < PAGE:
            return rows
        offset += PAGE


def load(refresh: bool = False) -> list[dict]:
    """All cached rows, fetching on first use. Raw: apply `usable()` before
    scoring."""
    path = cache_path()
    if path.exists() and not refresh:
        with gzip.open(path, "rt") as fh:
            rows = json.load(fh)
        logger.info(f"  [buildings] {len(rows):,} rows (cached)")
        return rows

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = fetch_all()
    # Atomic: temp name in the same directory, then os.replace(), so a
    # reader never sees a half-written cache (planimetrics.py's reasoning).
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(tmp, "wt") as fh:
            json.dump(rows, fh)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(f"  [buildings] {len(rows):,} rows (downloaded + cached)")
    return rows


def height_ft(row: dict) -> float | None:
    """The row's roof height as a number, or None when it is missing or
    not a number. Socrata delivers every value as a string."""
    raw = row.get("height_roof")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def usable(rows: list[dict]) -> list[dict]:
    """The rows that cast shade, with the drop tally logged.

    Each row is counted under the first rule it fails, in this order:
    missing or non-positive height, over the cap, excluded status,
    excluded feature code.
    """
    kept: list[dict] = []
    non_positive = over_cap = excluded_status = excluded_code = 0
    for row in rows:
        height = height_ft(row)
        if height is None or height <= 0:
            non_positive += 1
            continue
        if height > config.BUILDING_HEIGHT_CAP_FT:
            over_cap += 1
            continue
        if row.get("last_status_type") in config.BUILDING_EXCLUDED_STATUSES:
            excluded_status += 1
            continue
        if row.get("feature_code") in config.BUILDING_EXCLUDED_FEATURE_CODES:
            excluded_code += 1
            continue
        kept.append(row)

    logger.info(f"  [buildings] {len(rows):,} rows, {len(kept):,} kept; dropped "
                f"{non_positive:,} non-positive height, {over_cap:,} over "
                f"{config.BUILDING_HEIGHT_CAP_FT} ft cap, {excluded_status:,} "
                f"excluded status, {excluded_code:,} placeholder code")
    return kept
