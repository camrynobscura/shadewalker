"""Do big trees or particular species carry more real shade than we credit?

THE QUESTION (user, 2026-09-07, canopy-derivation)
--------------------------------------------------
The per-tree formula is deliberately crude: value is linear in trunk
diameter capped at 30in, identical across species. The user's street
experience -- a massive tree shading far more than its share -- asks
whether that crudeness UNDERCOUNTS big trees. Aggregate calibration
(fit_exchange_rate.py) cannot answer this: a fitted scale absorbs
uniform bias but not tree-to-tree scatter.

METHOD -- residual slicing
--------------------------
Same sample frame as the exchange-rate fit (sidewalk edges >= 20m,
raster-fallback and fragment-artifact edges excluded, same seed). For
each edge, two independent numbers: predicted coverage
(peak density / DENSITY_AT_FULL_COVERAGE) and measured coverage (the
2m-strip raster read production uses). The residual -- measured minus
predicted, in coverage points -- is then sliced by the attributes of the
trees actually attached to that edge's block faces (re-derived here with
the production 8m match; the export doesn't carry per-tree data):

    - mean attached dbh, value-weighted, in bands
    - presence of a capped tree (dbh >= DBH_CAP_IN)
    - dominant genus (>= 60% of attached value), overall AND within one
      mid-size dbh band, since species and size confound each other

A consistently POSITIVE residual in a slice means reality holds more
canopy there than the formula predicts -- undercounting.

READ WITH CARE
--------------
- Saturated edges (density above the exchange rate) are excluded: their
  prediction clips at 100% by design, so residuals there measure the
  clip, not the formula.
- The overall median residual is the instrument check: the sample frame
  is the one the exchange rate was fitted on, so it must sit near zero
  or this script is broken (validate-the-instrument, MEMORY).
- Big trees cluster in leafy neighbourhoods with unrecorded private
  canopy -- the marker effect that capped TREE_ATTACH_MAX_M at 8m. A
  positive big-tree residual is only believable if it is DOSE-RESPONSIVE
  across dbh bands, not a flat neighbourhood offset.
- Multi-face edges attribute tree weight by metres-on-face only (no
  face-length normalisation); exact shares would need the citywide
  face-length pass for a second-order correction.

USAGE
-----
    uv run python tools/audit/measure_size_species_residuals.py
"""

import argparse
import gzip
import json
import logging
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import rasterio  # noqa: E402
from pyproj import Transformer  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.fetch import planimetrics  # noqa: E402
from pipeline.fetch.boundaries import fetch_borough_boundaries  # noqa: E402
from pipeline.fetch.trees import fetch_trees  # noqa: E402
from pipeline.graph import pedestrian  # noqa: E402
from pipeline.graph.blockface import BlockFaceIndex  # noqa: E402
from pipeline.graph.boundary import nyc_boundary  # noqa: E402
from pipeline.scoring import canopy  # noqa: E402
from pipeline.scoring.blocks import SHADED_KINDS  # noqa: E402
from pipeline.scoring.trees import tree_value  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("residuals")

DENSITY_OUTLIER = 0.2         # fragment artifacts, same as the fit
DOMINANT_SHARE = 0.6          # a genus "dominates" an edge above this
DBH_BANDS = [(0, 6), (6, 12), (12, 18), (18, 24), (24, 30), (30, 999)]
MATCHED_DBH_BAND = (10, 22)   # mid-size band for the species comparison
MIN_SLICE_N = 100             # don't print a slice too thin to trust


def geometric_mid(coords) -> tuple[float, float]:
    """The (lon, lat) halfway along the line BY LENGTH. Planar local
    metric is plenty for a point to stand at in Street View."""
    import math
    if len(coords) < 2:
        return tuple(coords[0])
    cos_lat = math.cos(math.radians(coords[0][1]))
    seg = []
    total = 0.0
    for (lon0, lat0), (lon1, lat1) in zip(coords, coords[1:]):
        d = math.hypot((lon1 - lon0) * cos_lat, lat1 - lat0)
        seg.append(d)
        total += d
    walked = 0.0
    for (lon0, lat0), (lon1, lat1), d in zip(coords, coords[1:], seg):
        if walked + d >= total / 2 and d > 0:
            t = (total / 2 - walked) / d
            return (lon0 + (lon1 - lon0) * t, lat0 + (lat1 - lat0) * t)
        walked += d
    return tuple(coords[-1])


def dbh_of(row) -> float:
    """The same parse tree_value applies, kept in sync by hand."""
    try:
        dbh = float(row.get("dbh") or 0)
    except (TypeError, ValueError):
        return 0.0
    return dbh if dbh > 0 else 0.0


def genus_of(row) -> str:
    return (row.get("genusspecies") or "").split(" ")[0]


def describe(residuals) -> str:
    """Median [p25, p75] in coverage POINTS (x100), plus n."""
    pts = sorted(r * 100 for r in residuals)
    mid = statistics.median(pts)
    p25 = pts[len(pts) // 4]
    p75 = pts[(3 * len(pts)) // 4]
    return f"median {mid:+6.1f}  [p25 {p25:+6.1f}, p75 {p75:+6.1f}]  n={len(pts):,}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=25_000)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--min-edge-m", type=float, default=20.0)
    parser.add_argument("--dump", help="also write the per-edge records as "
                        "JSON here, for offline slicing (e.g. residuals at "
                        "matched predicted coverage, the regression-to-the-"
                        "mean control) without re-running the pipeline pass")
    args = parser.parse_args()
    started = time.monotonic()

    export_path = config.EXPORT_DIR / "citywide.json.gz"
    logger.info(f"[residuals] export: {export_path}")
    with gzip.open(export_path, "rt") as fh:
        payload = json.load(fh)
    scores = {(e["u"], e["v"], e["key"]): e for e in payload["edges"]}

    logger.info("[residuals] OSM read (the export drops `kind`)")
    nyc_shape = nyc_boundary(fetch_borough_boundaries())
    ped_ways, _ = pedestrian.read_ways(config.OSM_EXTRACT_PATH, nyc_shape)
    _, edges = pedestrian.build_graph(ped_ways)

    candidates = []
    for edge in edges:
        if edge.get("kind") not in SHADED_KINDS:
            continue
        rec = scores.get((edge["u"], edge["v"], int(edge["key"])))
        if rec is None or rec["length_m"] < args.min_edge_m:
            continue
        if rec.get("tree_park_canopy"):
            continue  # raster-scored: regressing the raster on itself
        density = (rec["tree_deciduous"] + rec["tree_evergreen"]) / rec["length_m"]
        if density > DENSITY_OUTLIER:
            continue  # fragment artifact, same as the fit
        if density > config.DENSITY_AT_FULL_COVERAGE:
            continue  # saturated: prediction clips at 100% by design
        candidates.append((edge, rec, density))
    logger.info(f"[residuals] {len(candidates):,} unsaturated sidewalk "
                f"edges >= {args.min_edge_m}m")

    rng = random.Random(args.seed)
    sample = rng.sample(candidates, min(args.sample, len(candidates)))

    logger.info("[residuals] block-face index (production machinery)")
    index = BlockFaceIndex(planimetrics.load("pavement_edge"),
                           planimetrics.load("cscl"))

    logger.info("[residuals] trees -> faces, keeping per-tree attributes")
    trees = fetch_trees(config.CITY_BBOX, "citywide")
    trees_of_face: dict[str, list] = {}
    attached = 0
    for row in trees:
        value = tree_value(row)
        if value is None:
            continue
        coords = (row.get("location") or {}).get("coordinates")
        if not coords:
            continue
        match = index.match_point(coords[0], coords[1],
                                  max_m=config.TREE_ATTACH_MAX_M)
        if match is None:
            continue
        total = value.evergreen + value.deciduous  # peak season, like the fit
        trees_of_face.setdefault(match.face.face_id, []).append(
            (total, dbh_of(row), genus_of(row)))
        attached += 1
    logger.info(f"[residuals] {attached:,} trees attached to "
                f"{len(trees_of_face):,} faces")

    logger.info(f"[residuals] profiles + raster over {len(sample):,} edges")
    to_raster = Transformer.from_crs(
        "EPSG:4326", config.CANOPY_RASTER_CRS, always_xy=True).transform
    rows = []
    no_profile = no_raster = 0
    with rasterio.open(config.CANOPY_RASTER_PATH) as src:
        for position, (edge, rec, density) in enumerate(sample):
            if position and position % 5000 == 0:
                logger.info(f"[residuals] {position:,} edges done")
            profile = index.match_line_profile(edge["coords"])
            if not profile:
                no_profile += 1
                continue
            fraction = canopy.leaf_fraction(src, rec["coords"], to_raster)
            if fraction is None:
                no_raster += 1
                continue

            # Attribute aggregation over the faces this edge touches,
            # each tree weighted by its value x the edge's metres on
            # that face (see READ WITH CARE).
            w_total = w_dbh = 0.0
            max_dbh = 0.0
            genus_weight: dict[str, float] = {}
            for face_id, metres in profile.items():
                for total, dbh, genus in trees_of_face.get(face_id, []):
                    w = total * metres
                    w_total += w
                    w_dbh += w * dbh
                    max_dbh = max(max_dbh, dbh)
                    genus_weight[genus] = genus_weight.get(genus, 0.0) + w

            predicted = density / config.DENSITY_AT_FULL_COVERAGE
            residual = fraction - predicted
            mean_dbh = (w_dbh / w_total) if w_total > 0 else None
            dominant = None
            if w_total > 0:
                genus, weight = max(genus_weight.items(), key=lambda kv: kv[1])
                if weight / w_total >= DOMINANT_SHARE and genus:
                    dominant = genus
            # Mid-edge point, for Street View spot-check batches built
            # from the dump (never an endpoint -- corners corrupt
            # verdicts, build_street_view_batch.py's lesson). GEOMETRIC
            # middle, not the middle vertex: a straight two-point edge's
            # middle vertex IS its endpoint -- a corner (hit for real,
            # 2026-09-07 batch, spot 1 landed on a crosswalk).
            mid_lon, mid_lat = geometric_mid(rec["coords"])
            rows.append({"residual": residual, "predicted": predicted,
                         "fraction": fraction, "mean_dbh": mean_dbh,
                         "max_dbh": max_dbh, "dominant": dominant,
                         "lat": mid_lat, "lon": mid_lon,
                         "length_m": rec["length_m"],
                         # A spot-check on a per-side model must NAME the
                         # side or the question is unanswerable (CLAUDE.md
                         # trap; hit for real on the 2026-09-07 batch).
                         "name": rec.get("name", ""),
                         "side": rec.get("side", "")})
    logger.info(f"[residuals] {len(rows):,} usable ({no_profile:,} no "
                f"profile, {no_raster:,} no raster reading)")

    if args.dump:
        with open(args.dump, "w") as fh:
            json.dump(rows, fh)
        logger.info(f"[residuals] per-edge records -> {args.dump}")

    print(f"\nResiduals in COVERAGE POINTS: positive = more real canopy "
          f"than the formula predicts (undercounting).")
    print(f"\nINSTRUMENT CHECK -- all edges (must sit near zero):")
    print(f"  {describe([r['residual'] for r in rows])}")

    treed = [r for r in rows if r["mean_dbh"] is not None]
    print(f"\nBY MEAN ATTACHED DBH (value-weighted), edges with trees:")
    for lo, hi in DBH_BANDS:
        band = [r["residual"] for r in treed if lo <= r["mean_dbh"] < hi]
        if len(band) >= MIN_SLICE_N:
            label = f"{lo}-{hi}in" if hi < 999 else f">={lo}in"
            print(f"  {label:>10s}  {describe(band)}")

    capped = [r["residual"] for r in treed if r["max_dbh"] >= config.DBH_CAP_IN]
    uncapped = [r["residual"] for r in treed if r["max_dbh"] < config.DBH_CAP_IN]
    print(f"\nCAPPED TREE PRESENT (any attached dbh >= {config.DBH_CAP_IN}in):")
    print(f"  {'present':>10s}  {describe(capped)}")
    print(f"  {'absent':>10s}  {describe(uncapped)}")

    by_genus: dict[str, list] = {}
    for r in treed:
        if r["dominant"]:
            by_genus.setdefault(r["dominant"], []).append(r)
    top = sorted(by_genus.items(), key=lambda kv: -len(kv[1]))[:10]
    print(f"\nBY DOMINANT GENUS (>= {DOMINANT_SHARE:.0%} of attached value), "
          f"top {len(top)} by count:")
    for genus, rs in top:
        med_dbh = statistics.median(r["mean_dbh"] for r in rs)
        print(f"  {genus:>14s}  {describe([r['residual'] for r in rs])}  "
              f"(median mean-dbh {med_dbh:.0f}in)")

    lo, hi = MATCHED_DBH_BAND
    print(f"\nSAME, SIZE-MATCHED (mean dbh {lo}-{hi}in only -- the species "
          f"signal with size held roughly constant):")
    for genus, rs in top:
        matched = [r["residual"] for r in rs if lo <= r["mean_dbh"] < hi]
        if len(matched) >= MIN_SLICE_N:
            print(f"  {genus:>14s}  {describe(matched)}")

    logger.info(f"[residuals] done in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
