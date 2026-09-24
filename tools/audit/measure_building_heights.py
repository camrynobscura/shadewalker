"""Cross-check NYC Building Footprints' `height_roof` against independent records.

WHY THIS EXISTS
---------------
The building-shade layer (PLAN `building-shadows`) stands on one column of
one dataset that publishes NO accuracy statement for height. Before a
citywide shadow table is built on it, this measures how far it can be
trusted, citywide, and where it is known to be wrong. It replaces the
2026-09-08 scratchpad checks (five areas, n=14,865; Wikidata tall tail,
185 matched), which found no systematic bias and one real stale-height
case (425 Park Ave reading the demolished 1957 tower). BUILDING-SHADOWS.md
section 4.7 holds those numbers; this is the durable rebuild, run on the
whole fetch.

Four checks, each independent of the others:

  (i)   PLUTO floors. Join single-building lots (PLUTO numbldgs == 1 AND
        exactly one footprint on the lot) to `numfloors` and look at feet
        per floor. Real buildings sit at ~10-16 ft/floor; far outside that
        band one of the two records is wrong.
  (ii)  Wikidata. Every item with a height (P2048) inside CITY_BBOX,
        matched by point-in-footprint to the tall tail (>= --tall-ft).
        Demolished items (P576) are excluded automatically; proposed
        never-built towers are not machine-detectable, so outliers are
        LISTED BY NAME for a human to read. Wikidata's P2048 is usually
        the architectural height (spire included), so the footprint being
        close to it is NOT proof it is a roof height -- the second column,
        feet per Wikidata floor (P1101), is the spire detector: a roof
        height divides to ~12-16 ft/floor, a spire height to more.
  (iii) Stale heights. Footprints whose lot was built or altered (PLUTO
        yearbuilt / yearalter1 / yearalter2) AFTER the footprint's own
        last_edited_date. A LOWER BOUND, validated 2026-09-24 against the
        two known stale tall buildings and catching NEITHER: 425 Park Ave
        (footprint 389 ft = the demolished 1957 tower; PLUTO still says
        built 1957, altered 2015, because the 860 ft rebuild was filed as
        an alteration) and 270 Park Ave (footprint re-initialised
        2026-08-21 at 700 ft = the demolished Union Carbide building, on
        a lot PLUTO dates 2021). Stale heights in the tall tail are found
        by check (ii) only; this check sees the low-rise churn.
  (iv)  A Street View batch for human floor-counting: --batch-n buildings,
        stratified by borough and floor band, PLUTO's count hidden until
        the reviewer has their own. The user's eyes are the only real
        ground truth here.

OSM `height` tags are deliberately NOT a check: NYC's were imported from
this same dataset (verified 2026-09-08, ratio p50 1.00), so agreement
would be provenance, not independence.

Read-only against the pipeline. Inputs are cached: the buildings fetch
under data/raw/socrata/ (pipeline/fetch/buildings.py), PLUTO's lot table
under data/raw/socrata/pluto_lots_v1.json (fetch_all_rows), and the
Wikidata answer beside the report. Writes a Markdown report and the
batch HTML into --out-dir.

    uv run python tools/audit/measure_building_heights.py
"""

import argparse
import collections
import datetime as dt
import html
import json
import logging
import os
import random
import statistics
import sys

import numpy as np
import requests
from shapely.geometry import shape, Point
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import config                       # noqa: E402
from pipeline.fetch import buildings, socrata     # noqa: E402

logger = logging.getLogger("measure_building_heights")

PLUTO_DATASET_ID = "64uk-42ks"
PLUTO_SELECT = "bbl,address,borough,numbldgs,numfloors,yearbuilt,yearalter1,yearalter2,bldgclass"
PLUTO_CACHE_NAME = "pluto_lots_v1"

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

# Wikidata unit items -> feet.
FT_PER_UNIT = {
    "Q11573": 3.280839895,   # metre
    "Q3710": 1.0,            # foot
    "Q174728": 0.03280839895,  # centimetre
    "Q174789": 0.003280839895,  # millimetre
}

SPARQL = """
SELECT ?item ?itemLabel ?coord ?height ?unit ?demolished ?floors WHERE {
  SERVICE wikibase:box {
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point(%(lon_min)s %(lat_min)s)"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point(%(lon_max)s %(lat_max)s)"^^geo:wktLiteral .
  }
  ?item p:P2048 ?st . ?st psv:P2048 ?v .
  ?v wikibase:quantityAmount ?height ; wikibase:quantityUnit ?unit .
  OPTIONAL { ?item wdt:P576 ?demolished }
  OPTIONAL { ?item wdt:P1101 ?floors }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
"""

# Feet per floor outside this band is flagged as suspicious in check (i).
FT_PER_FLOOR_LOW = 6.0
FT_PER_FLOOR_HIGH = 20.0
# Footprint height per WIKIDATA floor above this reads as a spire, not a roof.
SPIRE_FT_PER_FLOOR = 18.0


# ── helpers ─────────────────────────────────────────────────────────────────

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(values, qs=(10, 25, 50, 75, 90)):
    arr = np.asarray(values, dtype=float)
    return {q: float(np.percentile(arr, q)) for q in qs}


def _pct_line(values) -> str:
    p = _pct(values)
    return " / ".join(f"p{q} {p[q]:.1f}" for q in (10, 25, 50, 75, 90))


def _borough(bbl: str) -> str:
    return BOROUGHS.get((bbl or "")[:1], "?")


def _edited_year(row: dict):
    raw = row.get("last_edited_date") or ""
    return int(raw[:4]) if raw[:4].isdigit() else None


# ── inputs ──────────────────────────────────────────────────────────────────

def load_pluto() -> dict[str, dict]:
    rows = socrata.fetch_all_rows(
        dataset_id=PLUTO_DATASET_ID,
        where="bbl IS NOT NULL",
        select=PLUTO_SELECT,
        order="bbl",
        cache_name=PLUTO_CACHE_NAME,
    )
    lots = {}
    for row in rows:
        bbl = row.get("bbl")
        if bbl is None:
            continue
        # PLUTO's bbl arrives as "1000010010" or "1000010010.0" depending
        # on the export; footprints carry the plain 10-digit form.
        lots[str(bbl).split(".")[0]] = row
    return lots


def load_wikidata(cache_path: str) -> list[dict]:
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh)
    bbox = config.CITY_BBOX
    query = SPARQL % {"lon_min": bbox.lon_min, "lat_min": bbox.lat_min,
                      "lon_max": bbox.lon_max, "lat_max": bbox.lat_max}
    response = requests.get(
        "https://query.wikidata.org/sparql",
        params={"query": query},
        headers={"Accept": "application/sparql-results+json",
                 "User-Agent": "shadewalker-audit/1.0 (building height cross-check)"},
        timeout=120,
    )
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]
    items = []
    for b in bindings:
        unit = b["unit"]["value"].rsplit("/", 1)[1]
        if unit not in FT_PER_UNIT:
            continue
        lon, lat = b["coord"]["value"][len("Point("):-1].split()
        items.append({
            "qid": b["item"]["value"].rsplit("/", 1)[1],
            "label": b.get("itemLabel", {}).get("value", "?"),
            "lat": float(lat), "lon": float(lon),
            "height_ft": float(b["height"]["value"]) * FT_PER_UNIT[unit],
            "demolished": b.get("demolished", {}).get("value"),
            "floors": _num(b.get("floors", {}).get("value")),
        })
    with open(cache_path, "w") as fh:
        json.dump(items, fh)
    return items


# ── check (i): PLUTO floors ─────────────────────────────────────────────────

def check_pluto_floors(kept: list[dict], lots: dict, report: list) -> list[dict]:
    per_bbl = collections.Counter(r.get("base_bbl") for r in kept)
    joined = []
    for row in kept:
        bbl = row.get("base_bbl")
        lot = lots.get(bbl)
        if lot is None or per_bbl[bbl] != 1:
            continue
        if _num(lot.get("numbldgs")) != 1:
            continue
        floors = _num(lot.get("numfloors"))
        if not floors or floors <= 0:
            continue
        height = buildings.height_ft(row)
        joined.append({"bin": row["bin"], "bbl": bbl, "height_ft": height,
                       "floors": floors, "ft_per_floor": height / floors,
                       "address": lot.get("address", ""), "borough": _borough(bbl),
                       "lat": None, "lon": None, "row": row})

    fpf = [j["ft_per_floor"] for j in joined]
    low = [j for j in joined if j["ft_per_floor"] < FT_PER_FLOOR_LOW]
    high = [j for j in joined if j["ft_per_floor"] > FT_PER_FLOOR_HIGH]
    report.append("## (i) PLUTO floors -- feet per floor on single-building lots\n")
    report.append(f"Joined n = {len(joined):,} (one footprint on the lot, PLUTO "
                  f"numbldgs = 1, numfloors > 0) of {len(kept):,} usable footprints.\n")
    report.append(f"- ft/floor: {_pct_line(fpf)}")
    report.append(f"- < {FT_PER_FLOOR_LOW:g} ft/floor: {len(low):,} ({100 * len(low) / len(joined):.2f}%); "
                  f"> {FT_PER_FLOOR_HIGH:g} ft/floor: {len(high):,} ({100 * len(high) / len(joined):.2f}%)")
    one_floor = [j for j in high if j["floors"] == 1]
    multi = [j for j in high if j["floors"] > 1]
    report.append(f"  - of the > {FT_PER_FLOOR_HIGH:g}: {len(one_floor):,} are PLUTO one-floor lots "
                  f"(median height {statistics.median([j['height_ft'] for j in one_floor]):.1f} ft -- "
                  f"pitched-roof ridge heights, real, not errors); the suspicious remainder on 2+ "
                  f"floor lots is {len(multi):,} ({100 * len(multi) / len(joined):.2f}%)")
    report.append("- by floors: " + "; ".join(
        f"{label} {statistics.median(v):.1f} (n={len(v):,})"
        for label, v in _floor_bands(joined).items() if v))
    report.append("- by borough: " + "; ".join(
        f"{b} {statistics.median([j['ft_per_floor'] for j in joined if j['borough'] == b]):.1f}"
        for b in BOROUGHS.values() if any(j['borough'] == b for j in joined)))
    report.append("")
    report.append("Worst tall-per-floor (a spire, a wrong floor count, or a wrong height):\n")
    for j in sorted(high, key=lambda j: -j["ft_per_floor"])[:12]:
        report.append(f"  - BIN {j['bin']} {j['address']} ({j['borough']}): "
                      f"{j['height_ft']:.0f} ft / {j['floors']:.0f} floors = {j['ft_per_floor']:.1f} ft/floor")
    report.append("\nWorst short-per-floor (height far below its floor count):\n")
    for j in sorted(low, key=lambda j: j["ft_per_floor"])[:12]:
        report.append(f"  - BIN {j['bin']} {j['address']} ({j['borough']}): "
                      f"{j['height_ft']:.0f} ft / {j['floors']:.0f} floors = {j['ft_per_floor']:.1f} ft/floor")
    report.append("")
    return joined


def _floor_bands(joined):
    bands = {"1-2": [], "3-4": [], "5-7": [], "8-15": [], "16-40": [], "41+": []}
    for j in joined:
        f = j["floors"]
        key = ("1-2" if f <= 2 else "3-4" if f <= 4 else "5-7" if f <= 7
               else "8-15" if f <= 15 else "16-40" if f <= 40 else "41+")
        bands[key].append(j["ft_per_floor"])
    return bands


# ── check (ii): Wikidata tall tail ──────────────────────────────────────────

def check_wikidata(kept: list[dict], items: list[dict], tall_ft: float, report: list):
    tall = [r for r in kept if buildings.height_ft(r) >= tall_ft]
    geoms = [shape(r["the_geom"]) for r in tall]
    tree = STRtree(geoms)
    live = [i for i in items if not i["demolished"]]
    demolished = [i for i in items if i["demolished"]]

    matches = []
    for item in live:
        hits = tree.query(Point(item["lon"], item["lat"]), predicate="within")
        for idx in hits:
            row = tall[idx]
            fp = buildings.height_ft(row)
            matches.append({"label": item["label"], "qid": item["qid"], "bin": row["bin"],
                            "footprint_ft": fp, "wikidata_ft": item["height_ft"],
                            "ratio": fp / item["height_ft"], "floors": item["floors"],
                            "ft_per_wd_floor": (fp / item["floors"]) if item["floors"] else None})

    ratios = [m["ratio"] for m in matches]
    within10 = sum(1 for r in ratios if 0.9 <= r <= 1.1)
    report.append(f"## (ii) Wikidata -- the >= {tall_ft:g} ft tail\n")
    report.append(f"Footprints >= {tall_ft:g} ft: {len(tall):,}. Wikidata items with a height in "
                  f"CITY_BBOX: {len(items)} ({len(demolished)} demolished, excluded). "
                  f"Matched by point-in-footprint: {len(matches)} statements "
                  f"({len({m['bin'] for m in matches})} footprints).\n")
    if ratios:
        report.append(f"- footprint / Wikidata: {_pct_line(ratios)}; within +/-10%: "
                      f"{within10}/{len(ratios)} ({100 * within10 / len(ratios):.0f}%)")
    spire = [m for m in matches if m["ft_per_wd_floor"] and m["ft_per_wd_floor"] > SPIRE_FT_PER_FLOOR
             and 0.95 <= m["ratio"] <= 1.05]
    report.append(f"- footprint agrees with Wikidata within 5% BUT exceeds {SPIRE_FT_PER_FLOOR:g} ft per "
                  f"Wikidata floor (reads as a spire/architectural height, not a roof): {len(spire)}")
    for m in sorted(spire, key=lambda m: -m["footprint_ft"])[:15]:
        report.append(f"  - {m['label']} (BIN {m['bin']}): footprint {m['footprint_ft']:.0f} ft, "
                      f"Wikidata {m['wikidata_ft']:.0f} ft, {m['floors']:.0f} floors -> "
                      f"{m['ft_per_wd_floor']:.1f} ft/floor")
    report.append("\nOutliers beyond +/-10% (read the names: demolished predecessors and "
                  "never-built proposals are Wikidata-side, a wrong footprint height is ours):\n")
    for m in sorted(matches, key=lambda m: -abs(m["ratio"] - 1)):
        if 0.9 <= m["ratio"] <= 1.1:
            continue
        floors = f", {m['floors']:.0f} WD floors" if m["floors"] else ""
        report.append(f"  - {m['label']} (BIN {m['bin']}): footprint {m['footprint_ft']:.0f} ft vs "
                      f"Wikidata {m['wikidata_ft']:.0f} ft (ratio {m['ratio']:.2f}{floors})")
    report.append("\nThe ten tallest footprints and what Wikidata says:\n")
    seen = set()
    for m in sorted(matches, key=lambda m: -m["footprint_ft"]):
        if m["bin"] in seen:
            continue
        seen.add(m["bin"])
        floors = f", {m['floors']:.0f} floors -> {m['ft_per_wd_floor']:.1f} ft/floor" if m["floors"] else ""
        report.append(f"  - {m['label']} (BIN {m['bin']}): footprint {m['footprint_ft']:.0f} ft, "
                      f"Wikidata {m['wikidata_ft']:.0f} ft{floors}")
        if len(seen) == 10:
            break
    report.append("")


# ── check (iii): stale heights ──────────────────────────────────────────────

def check_stale(kept: list[dict], lots: dict, report: list):
    stale = []
    with_year = 0
    for row in kept:
        edited = _edited_year(row)
        lot = lots.get(row.get("base_bbl"))
        if edited is None or lot is None:
            continue
        with_year += 1
        years = [y for y in (_num(lot.get("yearbuilt")), _num(lot.get("yearalter1")),
                             _num(lot.get("yearalter2"))) if y and y > 1600]
        newest = max(years) if years else None
        if newest and newest > edited:
            kind = "built" if newest == _num(lot.get("yearbuilt")) else "altered"
            stale.append({"bin": row["bin"], "address": lot.get("address", ""),
                          "borough": _borough(row.get("base_bbl")),
                          "height_ft": buildings.height_ft(row), "edited": edited,
                          "lot_year": int(newest), "kind": kind})
    built = [s for s in stale if s["kind"] == "built"]
    altered = [s for s in stale if s["kind"] == "altered"]
    report.append("## (iii) Stale heights -- lot built/altered after the footprint was last edited\n")
    report.append("LOWER BOUND. Validated against the two known stale tall buildings and it "
                  "catches neither: 425 Park Ave (PLUTO yearbuilt 1957 / yearalter1 2015 -- the "
                  "860 ft rebuild was filed as an alteration) and 270 Park Ave (footprint "
                  "re-initialised 2026-08 at the demolished building's 700 ft, lot dated 2021). "
                  "A zero in the tall bands below means this method cannot see them, not that "
                  "there are none; the tall tail's stale heights are check (ii)'s job.\n")
    report.append(f"Footprints with a PLUTO lot and an edit year: {with_year:,}. "
                  f"Lot NEWER than the footprint edit: {len(stale):,} "
                  f"({100 * len(stale) / max(with_year, 1):.2f}%) -- {len(built):,} rebuilt "
                  f"(yearbuilt), {len(altered):,} altered (yearalter1/2).\n")
    report.append("- rebuilt, by borough: " + "; ".join(
        f"{b} {sum(1 for s in built if s['borough'] == b):,}" for b in BOROUGHS.values()))
    report.append("- rebuilt, by footprint height: " + "; ".join(
        f"{label} {n:,}" for label, n in _height_bands(built).items()))
    report.append("\nTallest footprints on a lot REBUILT since their edit (the 425 Park pattern):\n")
    for s in sorted(built, key=lambda s: -s["height_ft"])[:20]:
        report.append(f"  - BIN {s['bin']} {s['address']} ({s['borough']}): footprint {s['height_ft']:.0f} ft "
                      f"edited {s['edited']}, lot built {s['lot_year']}")
    report.append("")


def _height_bands(rows):
    bands = collections.OrderedDict((("< 50 ft", 0), ("50-100", 0), ("100-300", 0), (">= 300", 0)))
    for r in rows:
        h = r["height_ft"]
        key = "< 50 ft" if h < 50 else "50-100" if h < 100 else "100-300" if h < 300 else ">= 300"
        bands[key] += 1
    return bands


# ── check (iv): Street View floor-count batch ───────────────────────────────

BATCH_PAGE = """<!doctype html><meta charset="utf-8">
<title>Building Height Spot Check</title>
<style>
 body{font:15px/1.6 ui-monospace,SF Mono,Menlo,monospace;max-width:52rem;
      margin:0 auto;padding:32px 20px 80px;background:#e6f2e9;color:#0b2418}
 h1{font-size:1.5rem;margin:0 0 6px}
 .sub{color:#4a7360;margin:0 0 24px}
 ol{list-style:none;padding:0;margin:0}
 li{display:grid;grid-template-columns:2rem 1fr auto;gap:12px;align-items:baseline;
    padding:11px 13px;border:1px solid #cfe5d6;border-bottom:0;background:#fff}
 li:last-child{border-bottom:1px solid #cfe5d6}
 .n{color:#4a7360;font-size:.75rem;font-weight:600}
 .st{font-weight:600}
 .meta{font-size:.68rem;color:#4a7360}
 a.go{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
      text-decoration:none;background:#007a4d;color:#fff;padding:6px 10px;white-space:nowrap}
 details{margin-top:24px;background:#eff8f1;border:1px solid #9dc7ac;padding:12px 16px}
 summary{cursor:pointer;font-weight:600;font-size:.82rem}
 table{border-collapse:collapse;width:100%%;font-size:.75rem;margin-top:10px}
 td,th{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid #cfe5d6}
</style>
<h1>Building Height Spot Check</h1>
<p class="sub">For each building: open Street View, COUNT THE FLOORS above the
sidewalk, and write the number down before opening the answer table. The
dataset's roof height is shown; ~13 ft per floor is typical, so the implied
floor count is height / 13. A count far from that means the height is
wrong, the floors are unusually tall or short, or a spire/bulkhead is
being counted.</p>
<ol>
%(items)s
</ol>
<details><summary>Answer table (open AFTER counting): PLUTO's floor count</summary>
<table><tr><th>#</th><th>BIN</th><th>height_roof</th><th>PLUTO floors</th><th>ft/floor</th><th>borough</th></tr>
%(answers)s
</table></details>
"""


def build_batch(joined: list[dict], n: int, seed: int, out_path: str, report: list):
    rng = random.Random(seed)
    # Countable from the street: not so tall that the top is out of frame.
    pool = [j for j in joined if j["height_ft"] <= 200]
    per_borough = collections.defaultdict(list)
    for j in pool:
        per_borough[j["borough"]].append(j)
    bands = (lambda f: "1-3" if f <= 3 else "4-7" if f <= 7 else "8+")
    chosen = []
    per_b = max(1, n // len(BOROUGHS))
    for borough in BOROUGHS.values():
        candidates = per_borough.get(borough, [])
        by_band = collections.defaultdict(list)
        for j in candidates:
            by_band[bands(j["floors"])].append(j)
        picks = []
        for band in ("1-3", "4-7", "8+"):
            if by_band[band]:
                picks.extend(rng.sample(by_band[band], min(per_b // 3 or 1, len(by_band[band]))))
        while len(picks) < per_b and candidates:
            extra = rng.choice(candidates)
            if extra not in picks:
                picks.append(extra)
        chosen.extend(picks[:per_b])
    rng.shuffle(chosen)

    items, answers = [], []
    for i, j in enumerate(chosen, 1):
        centroid = shape(j["row"]["the_geom"]).centroid
        link = f"https://www.google.com/maps?layer=c&cbll={centroid.y:.5f},{centroid.x:.5f}"
        implied = j["height_ft"] / 13
        items.append(
            f'<li><span class="n">{i:02d}</span><span><span class="st">'
            f'{html.escape(j["address"] or "(no address)")}</span>, {j["borough"]}<br>'
            f'<span class="meta">height_roof {j["height_ft"]:.0f} ft &rarr; about '
            f'{implied:.0f} floors at 13 ft; BIN {j["bin"]}</span></span>'
            f'<a class="go" href="{link}" target="_blank" rel="noopener">Street View</a></li>')
        answers.append(f"<tr><td>{i:02d}</td><td>{j['bin']}</td><td>{j['height_ft']:.0f} ft</td>"
                       f"<td>{j['floors']:.0f}</td><td>{j['ft_per_floor']:.1f}</td><td>{j['borough']}</td></tr>")
    with open(out_path, "w") as fh:
        fh.write(BATCH_PAGE % {"items": "\n".join(items), "answers": "\n".join(answers)})
    report.append("## (iv) Street View floor-count batch\n")
    report.append(f"{len(chosen)} buildings <= 200 ft, {per_b} per borough across floor bands "
                  f"1-3 / 4-7 / 8+, seed {seed}: `{out_path}`. Count floors, then open the "
                  f"answer table. Verdicts are yours; record them in the session notes.\n")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=f"data/audits/{dt.date.today().isoformat()}")
    parser.add_argument("--tall-ft", type=float, default=300.0)
    parser.add_argument("--batch-n", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260924)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    os.makedirs(args.out_dir, exist_ok=True)
    report = [f"# Building height cross-check -- {dt.date.today().isoformat()}\n",
              f"Dataset `{config.BUILDINGS_DATASET_ID}` via pipeline/fetch/buildings.py "
              f"(cache v{config.BUILDINGS_CACHE_VERSION}); PLUTO `{PLUTO_DATASET_ID}`; "
              f"Wikidata P2048/P1101/P576. Seed {args.seed}.\n"]

    rows = buildings.load()
    kept = buildings.usable(rows)
    lots = load_pluto()
    logger.info(f"PLUTO lots: {len(lots):,}")
    items = load_wikidata(os.path.join(args.out_dir, "wikidata_heights.json"))
    logger.info(f"Wikidata items with a height: {len(items)}")

    joined = check_pluto_floors(kept, lots, report)
    check_wikidata(kept, items, args.tall_ft, report)
    check_stale(kept, lots, report)
    build_batch(joined, args.batch_n, args.seed,
                os.path.join(args.out_dir, "building_heights_street_view.html"), report)

    text = "\n".join(report)
    report_path = os.path.join(args.out_dir, "building_heights_report.md")
    with open(report_path, "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
