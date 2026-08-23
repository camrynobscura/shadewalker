"""Build the citywide pedestrian graph and export it for the server.

    uv run python -m pipeline.build

No arguments, and deliberately so: there is exactly one thing to build.
The centerline model's runner took a tile id or a borough name because it
produced 276 files; this produces one, for all five boroughs, every time.

WHERE IT WRITES
---------------
config.TILES_DIR, which is data/tiles/ unless SHADEWALKER_TILES_DIR says
otherwise. A practice run must set that variable:

    SHADEWALKER_TILES_DIR=/tmp/scratch uv run python -m pipeline.build

Without it this overwrites the file a running server is serving from.
That is not hypothetical -- smoke-test runs of the old runner wrote into
the production directory more than once. The export itself is atomic
(pipeline/export.py), so a reader never sees a half-written file, but
atomic still means the new file replaces the old one.

WHAT IT DOES NOT DO
-------------------
No tree scoring: every tree field in the export is zero, so routes over
this graph are the SHORTEST walk, never the shadiest. No parent-street
names beyond the 2.5% of ways that carry one in OSM. Both are later
steps; this is the spine, and its job is to prove the network routes at
all.
"""

import logging
import sys
import time

from pipeline import config, export
from pipeline.fetch.boundaries import fetch_borough_boundaries
from pipeline.graph import pedestrian
from pipeline.graph.boundary import nyc_boundary

logger = logging.getLogger(__name__)


def main() -> int:
    # Same setup as server/app.py: without it a __main__ run has a bare
    # root logger and every logger.info() in the pipeline vanishes,
    # leaving a two-minute build completely silent.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not config.OSM_EXTRACT_PATH.exists():
        logger.error(
            f"No OSM extract at {config.OSM_EXTRACT_PATH}. It is gitignored "
            "and ~495MB -- download New York State's .osm.pbf from Geofabrik "
            "and record what you pinned in "
            "data/oracle/new-york-latest.timestamp.txt."
        )
        return 1

    started = time.monotonic()

    # The extract is statewide, so the graph has to be clipped to NYC's
    # real boundary. Water-included on purpose: a bridge's midspan sits
    # over water, and the water-excluded dataset once severed every
    # inter-borough crossing (pipeline/fetch/boundaries.py).
    logger.info("[build] boundary")
    nyc_shape = nyc_boundary(fetch_borough_boundaries())

    logger.info(f"[build] reading {config.OSM_EXTRACT_PATH.name}")
    nodes, edges = pedestrian.build(config.OSM_EXTRACT_PATH, nyc_shape)
    if not edges:
        logger.error("[build] no edges built -- refusing to write an empty "
                     "export over a good one")
        return 1

    logger.info(f"[build] exporting to {config.TILES_DIR}")
    out_path = export.write_citywide(nodes, edges)

    if not _readback_matches(out_path, len(nodes), len(edges)):
        return 1

    logger.info(f"[build] done in {time.monotonic() - started:.0f}s")
    return 0


def _readback_matches(out_path, expected_nodes: int, expected_edges: int) -> bool:
    """Re-open the written file and check it holds what we just built.

    Cheap, and it closes the one gap the export's own atomicity does not:
    atomicity guarantees the file is whole, not that it is the graph we
    meant to write. This file is the entire contract with the routing
    server, and a mismatch here is far easier to understand now than as a
    strange route days later.
    """
    import gzip
    import json

    with gzip.open(out_path, "rt") as fh:
        payload = json.load(fh)

    problems = []
    if len(payload["nodes"]) != expected_nodes:
        problems.append(f"nodes: built {expected_nodes:,}, "
                        f"file has {len(payload['nodes']):,}")
    if len(payload["edges"]) != expected_edges:
        problems.append(f"edges: built {expected_edges:,}, "
                        f"file has {len(payload['edges']):,}")
    if payload["meta"]["node_count"] != len(payload["nodes"]):
        problems.append("meta.node_count disagrees with the nodes it holds")
    if payload["meta"]["edge_count"] != len(payload["edges"]):
        problems.append("meta.edge_count disagrees with the edges it holds")

    if problems:
        for problem in problems:
            logger.error(f"[build] readback MISMATCH -- {problem}")
        return False

    logger.info(f"[build] readback ok: {len(payload['nodes']):,} nodes, "
                f"{len(payload['edges']):,} edges")
    return True


if __name__ == "__main__":
    sys.exit(main())
