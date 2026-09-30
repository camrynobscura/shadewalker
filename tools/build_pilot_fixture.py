"""Cut the citywide export down to the pilot area, for the Playwright suite.

    uv run python tools/build_pilot_fixture.py

WHY THIS EXISTS
---------------
web/playwright.config.ts boots a real backend against
tests/fixtures/pilot.json.gz before running any spec; without that file the
config's `cp` step fails, uvicorn never starts, and the whole e2e tier
aborts before running a single test.

pytest does not need this file: tests that need a graph build their own
synthetic export with tmp_path. This is for the browser tier alone.

WHERE IT MUST LAND
------------------
tests/fixtures/, and never data/export/. The server globs *.json.gz in
EXPORT_DIR and loads everything it finds, so a fixture sitting beside real
data would inject its edges into the citywide graph. This script hard-fails
rather than trust the caller to remember.

WHAT THE SPECS REQUIRE OF IT
----------------------------
web/e2e/fixtures.ts pins three points, and the fixture is only valid if all
three still behave:

    POINT_A                 40.6800,-73.9980   routable
    POINT_B                 40.6720,-73.9880   routable, and reachable from A
    POINT_OUTSIDE_COVERAGE  40.7580,-73.9855   rejected as out of coverage

The first two are Carroll Gardens/Gowanus and sit inside config.PILOT_BBOX;
the third is midtown Manhattan and must stay outside it. All three are
asserted below, because a fixture that silently stops satisfying one of them
turns a real regression into a confusing spec failure.

EDGE SELECTION
--------------
An edge is kept when both endpoints are inside the bbox, and the node set is
then exactly the endpoints of the kept edges. Keeping an edge with one
endpoint outside would leave a dangling node reference, which GraphStore
would either drop or choke on -- neither of which should be discovered from
a browser test.
"""

import gzip
import json
import logging
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline import config  # noqa: E402
from pipeline.export import CITYWIDE_NAME  # noqa: E402

logger = logging.getLogger(__name__)

OUT_PATH = REPO / "tests" / "fixtures" / "pilot.json.gz"

# Straight from web/e2e/fixtures.ts. (lat, lon, must_be_covered)
E2E_POINTS = [
    ("POINT_A (250 Court St)", 40.6800, -73.9980, True),
    ("POINT_B (3rd St & 3rd Ave)", 40.6720, -73.9880, True),
    ("POINT_OUTSIDE_COVERAGE", 40.7580, -73.9855, False),
]


def inside(lon: float, lat: float) -> bool:
    b = config.PILOT_BBOX
    return (b.lon_min <= lon <= b.lon_max) and (b.lat_min <= lat <= b.lat_max)


def nearest_edge_distance_m(edges, lat, lon):
    """Crude great-circle distance to the nearest edge vertex, in metres.

    Deliberately not GraphStore's own snapping: this is a sanity check on the
    fixture's extent, and reusing the thing under test to validate the thing
    under test is how a check ends up confirming its own assumption.
    """
    best = float("inf")
    for edge in edges:
        for elon, elat in edge["coords"]:
            dlat = math.radians(elat - lat)
            dlon = math.radians(elon - lon) * math.cos(math.radians(lat))
            d = math.hypot(dlat, dlon) * 6_371_000
            if d < best:
                best = d
    return best


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    source = config.EXPORT_DIR / f"{CITYWIDE_NAME}.json.gz"
    if not source.exists():
        logger.error(f"No citywide export at {source}. Run "
                     f"`uv run python -m pipeline.build` first.")
        return 1

    if OUT_PATH.resolve().parent == config.EXPORT_DIR.resolve():
        logger.error("Refusing to write a fixture into EXPORT_DIR -- the "
                     "server loads every *.json.gz it finds there.")
        return 1

    logger.info(f"reading {source}")
    with gzip.open(source, "rt") as fh:
        payload = json.load(fh)
    nodes, edges = payload["nodes"], payload["edges"]
    logger.info(f"  {len(nodes):,} nodes, {len(edges):,} edges citywide")

    kept_edges = []
    for edge in edges:
        u, v = nodes.get(edge["u"]), nodes.get(edge["v"])
        if u is None or v is None:
            continue
        if inside(*u) and inside(*v):
            kept_edges.append(edge)

    kept_nodes = {}
    for edge in kept_edges:
        for node_id in (edge["u"], edge["v"]):
            kept_nodes[node_id] = nodes[node_id]

    logger.info(f"  {len(kept_nodes):,} nodes, {len(kept_edges):,} edges in "
                f"the pilot bbox")
    if not kept_edges:
        logger.error("empty selection -- refusing to write")
        return 1

    # A non-empty side means the edge matched a block face and went through
    # scoring (some scored edges legitimately read "" on diagonals/curves,
    # so this undercounts a little -- fine for a sanity statistic that
    # exists to catch "all zeroed").
    scored = [e for e in kept_edges if e["side"]]
    trees = sum(e["tree_count"] for e in kept_edges)
    logger.info(f"  {len(scored):,} scored pavement edges, "
                f"{trees:,.0f} trees -- the fixture carries real shade, so "
                f"e2e exercises the shade path rather than a zeroed one")

    problems = []
    for label, lat, lon, must_be_covered in E2E_POINTS:
        distance = nearest_edge_distance_m(kept_edges, lat, lon)
        covered = distance <= config.MAX_SNAP_DISTANCE_M
        ok = covered == must_be_covered
        logger.info(f"  {'OK ' if ok else 'BAD'} {label}: nearest edge "
                    f"{distance:,.0f}m, covered={covered} "
                    f"(spec needs {must_be_covered})")
        if not ok:
            problems.append(label)
    if problems:
        logger.error(f"fixture does not satisfy web/e2e/fixtures.ts: "
                     f"{', '.join(problems)} -- refusing to write")
        return 1

    payload = {
        "meta": {**payload["meta"], "tile_id": "pilot",
                 "node_count": len(kept_nodes), "edge_count": len(kept_edges)},
        "nodes": kept_nodes,
        "edges": kept_edges,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    try:
        with gzip.open(tmp, "wt") as fh:
            json.dump(payload, fh)
        tmp.replace(OUT_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    size_kb = OUT_PATH.stat().st_size / 1024
    logger.info(f"wrote {OUT_PATH.relative_to(REPO)} ({size_kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
