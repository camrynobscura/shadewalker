"""Generic client for NYC Open Data's Socrata API, with pagination + disk cache.

Socrata caps each request at a page of rows, so "give me every tree in this
area" means requesting page after page until one comes back short. Every
successful full download is cached to data/raw/socrata/<name>.json; reruns
read the file instead of the network (delete the file or pass refresh=True
to force a re-download).
"""

import json

import requests

from pipeline import config

CACHE_DIR = config.RAW_DIR / "socrata"


def _auth_headers() -> dict[str, str]:
    """X-App-Token raises Socrata's per-IP rate limit; omitted entirely
    (rather than sent empty) when no token is configured, so anonymous
    requests keep working exactly as before."""
    if config.SOCRATA_APP_TOKEN:
        return {"X-App-Token": config.SOCRATA_APP_TOKEN}
    return {}


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
        print(f"  [socrata] {cache_name}: {len(rows)} rows (cached)")
        return rows

    url = f"{config.SOCRATA_BASE_URL}/{dataset_id}.json"
    headers = _auth_headers()
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
        response = requests.get(url, params=params, headers=headers, timeout=120)
        response.raise_for_status()  # turn HTTP errors (4xx/5xx) into exceptions
        page = response.json()

        rows.extend(page)
        print(f"  [socrata] {cache_name}: fetched {len(rows)} rows...")

        # A short page means we've reached the end of the matching rows.
        if len(page) < config.SOCRATA_PAGE_SIZE:
            break
        offset += config.SOCRATA_PAGE_SIZE

    # mkdir -p equivalent; then write the cache file for next time.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(rows))
    print(f"  [socrata] {cache_name}: {len(rows)} rows (downloaded + cached)")
    return rows
