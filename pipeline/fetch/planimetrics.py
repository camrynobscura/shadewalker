"""Fetch NYC's kerb lines and street centerlines from NYC Planimetrics.

WHY THIS EXISTS
---------------
Shade is trees divided by a length, and that length has to mean something.
OSM chops sidewalks at arbitrary points, so the same tree reads one density
on a 2.6m stub and another on the 51.9m run beside it. The fix is to group
pavement by BLOCK FACE -- one side of one block -- and NYC publishes the
key: every kerb line carries a `blockf_id` conflated to a CSCL block face.

Measuring to the KERB rather than the street centerline is the whole point.
A centerline sits in the middle of the roadway, so its distance to a
sidewalk is half the road width -- 5.93m on a side street, 13.38m on a
boulevard -- and no single threshold can be right for both. A kerb is the
roadway's edge and sits ~2.2m from its sidewalk whatever the street's
width (measured: kerb gap slope +0.0036 m/ft vs the centerline's +0.0618).

  Pavement Edge (vs44-rznx)  the kerb lines. `blockf_id` names a CSCL
                             left/right block face; `conflated` says
                             whether that link succeeded (91.9% do).
  CSCL         (inkn-q76z)   street centerlines, carrying
                             l_blockfaceid / r_blockfaceid,
                             full_street_name, streetwidth, physicalid.

CSCL IS ATTRIBUTES ONLY. Use it for which side and what name. Do NOT use
its geometry as a ruler -- a centerline segment can be far shorter than the
run of kerb conflated to it, and measuring block length that way produced a
false "13% of the city is mis-assigned" result on 2026-08-23. The
centerline is the wrong instrument for relating a sidewalk to a block, and
that is as true when it is a ruler as when it was a threshold.

WHY NOT socrata.fetch_all_rows()
--------------------------------
Two reasons, both about size. Pavement Edge is 182MB GZIPPED and well over
a gigabyte as plain text, so these caches are written compressed;
fetch_all_rows caches uncompressed .json. It also writes with a plain
write_text(), which can leave a half-written cache if a run dies mid-write
-- survivable for a small file, not for one this size. This module writes
to a temp name and os.replace()s, which POSIX guarantees is all-or-nothing.

TRAPS
-----
  - Pavement Edge also appears as `x9uq-u3qs`. That view returns EMPTY rows
    through the API; `vs44-rznx` is the queryable one.
  - Page with `$order=:id`. Pavement Edge has no `objectid` column, so
    ordering on that returns 400. `:id` is Socrata's own stable row key and
    exists on every dataset.
  - Roadbed (xgwd-7vhd) and Sidewalk (vfx9-tbb6) polygons carry NO street
    identifier. `blockf_id` on Pavement Edge is the only path to a street,
    which is why the linear kerb data wins over the polygons.

Capture rules:
https://github.com/CityOfNewYork/nyc-planimetrics/blob/main/Capture_Rules.md
"""

import gzip
import json
import logging
import time

from pipeline import config
from pipeline.fetch.socrata import get_with_retry

logger = logging.getLogger(__name__)

ROOT = "https://data.cityofnewyork.us/resource"

# Deliberately larger than config.SOCRATA_PAGE_SIZE: these are two whole
# citywide layers (178,947 + 122,256 rows) rather than a bbox slice, and
# fewer round trips matters more than a small page.
PAGE = 25_000

DATASETS = {
    "pavement_edge": "vs44-rznx",
    "cscl": "inkn-q76z",
}


def cache_path(label: str):
    return config.RAW_DIR / "socrata" / f"planimetrics_{label}.json.gz"


def fetch_all(dataset_id: str, label: str) -> list[dict]:
    """Every row of one layer, paged. See the docstring on `$order=:id`."""
    rows: list[dict] = []
    offset = 0
    started = time.perf_counter()
    while True:
        response = get_with_retry(
            f"{ROOT}/{dataset_id}.json",
            params={"$limit": PAGE, "$offset": offset, "$order": ":id"},
        )
        page = response.json()
        rows.extend(page)
        logger.info(f"  [planimetrics] {label}: {len(rows):,} rows "
                    f"({time.perf_counter() - started:.0f}s)")
        if len(page) < PAGE:
            return rows
        offset += PAGE


def load(label: str, refresh: bool = False) -> list[dict]:
    """Cached rows for one layer, fetching on first use.

    `label` is a key of DATASETS, not a dataset id -- callers name the
    layer they want ("cscl") and this module owns which id that is.
    """
    if label not in DATASETS:
        raise KeyError(f"unknown planimetrics layer {label!r}; "
                       f"expected one of {sorted(DATASETS)}")
    path = cache_path(label)
    if path.exists() and not refresh:
        with gzip.open(path, "rt") as fh:
            rows = json.load(fh)
        logger.info(f"  [planimetrics] {label}: {len(rows):,} rows (cached)")
        return rows

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = fetch_all(DATASETS[label], label)
    # Atomic: temp name in the SAME directory, then os.replace(). A reader
    # must never see a half-written cache, and same-directory matters
    # because replace() is only atomic within one filesystem.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(tmp, "wt") as fh:
            json.dump(rows, fh)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(f"  [planimetrics] {label}: {len(rows):,} rows "
                f"(downloaded + cached)")
    return rows
