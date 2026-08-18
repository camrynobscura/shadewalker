"""Generic client for NYC Open Data's Socrata API, with pagination + disk cache.

Socrata caps each request at a page of rows, so "give me every tree in this
area" means requesting page after page until one comes back short. Every
successful full download is cached to data/raw/socrata/<name>.json; reruns
read the file instead of the network (delete the file or pass refresh=True
to force a re-download).
"""

import logging
import json
import time

import requests

from pipeline import config

logger = logging.getLogger(__name__)


CACHE_DIR = config.RAW_DIR / "socrata"

# Real case (Queens, tile 145/154, after Brooklyn+Manhattan+144 Queens
# tiles had already fetched cleanly): a single transient 503 from Socrata
# killed the whole borough run, with 144 tiles' worth of progress sitting
# safely cached on disk but the run itself dead. Mirrors streets.py's own
# MAX_FETCH_RETRIES/FETCH_RETRY_BACKOFF_S -- same shape, applied to
# Socrata's transient-failure pattern instead of Overpass's.
MAX_FETCH_RETRIES = 3
FETCH_RETRY_BACKOFF_S = 5


def auth_headers() -> dict[str, str]:
    """X-App-Token raises Socrata's per-IP rate limit; omitted entirely
    (rather than sent empty) when no token is configured, so anonymous
    requests keep working exactly as before. Public because
    pipeline/fetch/boundaries.py also needs it -- it doesn't go through
    fetch_all_rows() below (only 5 rows, no pagination), but still wants
    the same token behavior."""
    if config.SOCRATA_APP_TOKEN:
        return {"X-App-Token": config.SOCRATA_APP_TOKEN}
    return {}


def get_with_retry(url: str, params: dict | None = None,
                   headers: dict | None = None) -> requests.Response:
    """One GET, retrying on transient failures -- a 5xx status or a
    network-level ConnectionError/Timeout, the same kind of one-off
    server hiccup streets.py's own retry logic exists for. A 4xx status
    (a malformed query, a rejected app token) means retrying would just
    fail again identically, so those propagate immediately instead of
    burning through the retry budget.

    Public (FIXES item 10): parks.py and boundaries.py fetch whole files
    rather than paginated rows, so they don't go through fetch_all_rows()
    -- but a bare requests.get() there meant one network blip killed a
    whole pipeline run while every other fetcher retried. One helper, one
    retry standard."""
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=120)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc  # Python 3 unbinds `exc` itself once this block ends
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        if attempt == MAX_FETCH_RETRIES:
            raise last_exc
        wait_s = FETCH_RETRY_BACKOFF_S * (2 ** (attempt - 1))
        logger.warning(f"  [socrata] transient error ({last_exc}) on attempt "
              f"{attempt}/{MAX_FETCH_RETRIES}, retrying in {wait_s}s...")
        time.sleep(wait_s)


def fetch_all_rows(
    dataset_id: str,
    where: str,
    select: str,
    order: str,
    cache_name: str,
    refresh: bool = False,
) -> list[dict]:
    """Download every row matching `where`, paginating past the row cap.

    `order` matters: Socrata pages are only stable if the query has a fixed
    sort order — without one, rows can shift between pages and get skipped
    or duplicated. Sort by any unique column.
    """
    cache_path = CACHE_DIR / f"{cache_name}.json"

    if cache_path.exists() and not refresh:
        # Cache hit: read_text + json.loads ≈ JSON.parse(fs.readFileSync(...))
        rows = json.loads(cache_path.read_text())
        logger.info(f"  [socrata] {cache_name}: {len(rows)} rows (cached)")
        return rows

    url = f"{config.SOCRATA_BASE_URL}/{dataset_id}.json"
    headers = auth_headers()
    rows: list[dict] = []
    offset = 0

    while True:
        # The $-prefixed params are Socrata's query language (SoQL) — a
        # URL-encoded cousin of SQL: SELECT ... WHERE ... ORDER BY ... LIMIT/OFFSET.
        params = {
            "$select": select,
            "$where": where,
            "$order": order,
            "$limit": config.SOCRATA_PAGE_SIZE,
            "$offset": offset,
        }
        response = get_with_retry(url, params, headers)
        page = response.json()

        rows.extend(page)
        logger.info(f"  [socrata] {cache_name}: fetched {len(rows)} rows...")

        # A short page means we've reached the end of the matching rows.
        if len(page) < config.SOCRATA_PAGE_SIZE:
            break
        offset += config.SOCRATA_PAGE_SIZE

    # mkdir -p equivalent; then write the cache file for next time.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(rows))
    logger.info(f"  [socrata] {cache_name}: {len(rows)} rows (downloaded + cached)")
    return rows
