"""Build the citywide pedestrian graph and export it for the server.

    uv run python -m pipeline.build

No arguments, and deliberately so: there is exactly one thing to build.
The centerline model's runner took a tile id or a borough name because it
produced 276 files; this produces one, for all five boroughs, every time.

WHERE IT WRITES
---------------
config.EXPORT_DIR, which is data/export/ unless SHADEWALKER_EXPORT_DIR says
otherwise. A practice run must set that variable:

    SHADEWALKER_EXPORT_DIR=/tmp/scratch uv run python -m pipeline.build

Without it this overwrites the file a running server is serving from.
That is not hypothetical -- smoke-test runs of the old runner wrote into
the production directory more than once. The export itself is atomic
(pipeline/export.py), so a reader never sees a half-written file, but
atomic still means the new file replaces the old one.

SHADE, IN TWO INSTRUMENTS
-------------------------
Sidewalks are scored from the Forestry tree data via block faces
(pipeline/scoring/blocks.py); everything kerb-less -- park paths, plazas,
steps, alleys -- is scored from the land-cover raster
(pipeline/scoring/canopy.py, wired 2026-08-26), which fills
`tree_park_canopy` for the first time. One instrument per pavement kind,
so the two can never double-count. Crossings score zero shade by design
(they run ACROSS a roadway) while staying fully routable. A build without
the 1.7GB raster on disk skips the canopy step cleanly.

The export carries real tree scores, and as of 2026-08-24 the server acts
on them: the shade saturation point was re-derived for this model (0.02,
now DENSITY_AT_FULL_COVERAGE = 0.031 since the 2026-08-26 unification) and
the per-edge length floor was deleted outright, so the fail-closed guards
are gone and the four Shade_priority presets produce genuinely different
routes for the first time.
"""

import logging
import sys
import time

from pipeline import config, export, sun
from pipeline.fetch import buildings, planimetrics
from pipeline.fetch.boundaries import fetch_borough_boundaries
from pipeline.fetch.trees import fetch_trees
from pipeline.graph import naming, pedestrian
from pipeline.graph.blockface import BlockFaceIndex
from pipeline.graph.boundary import nyc_boundary
from pipeline.scoring import blocks, canopy, shadows

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

    # One pass yields both: pedestrian ways for the graph, named streets
    # for the naming step. Reading twice would cost another ~110s.
    logger.info(f"[build] reading {config.OSM_EXTRACT_PATH.name}")
    ped_ways, street_ways = pedestrian.read_ways(
        config.OSM_EXTRACT_PATH, nyc_shape)
    logger.info(f"[build] {len(ped_ways):,} pedestrian ways, "
                f"{len(street_ways):,} named streets")

    nodes, edges = pedestrian.build_graph(ped_ways)
    if not edges:
        logger.error("[build] no edges built -- refusing to write an empty "
                     "export over a good one")
        return 1

    # Labels only -- this adds no way, removes none, and connects nothing.
    # See pipeline/graph/naming.py on why that is not an OSM override.
    logger.info("[build] naming")
    naming.assign_parent_names(edges, street_ways)

    # Shade. Fills tree_deciduous / tree_evergreen / tree_count on every
    # edge, and `side` from the block face -- which is what pedestrian.py's
    # "C" placeholder was reserved for. Like naming above, this is labels
    # and grouping: it adds no way, removes none, and connects nothing.
    _score_shade(edges)

    # The second shade LAYER: building shadows by month and hour, combined
    # with trees by the server. Labels again -- no way added, none removed.
    sun_table = _score_building_shade(edges)

    logger.info(f"[build] exporting to {config.EXPORT_DIR}")
    out_path = export.write_citywide(nodes, edges, sun_table=sun_table)

    if not _readback_matches(out_path, len(nodes), len(edges)):
        return 1

    logger.info(f"[build] done in {time.monotonic() - started:.0f}s")
    return 0


def _score_shade(edges: list[dict]) -> None:
    """Give every edge the shade of the block face it lies on.

    Three inputs, each cached on first use so a rebuild pays only the
    scoring cost: NYC's kerb lines and CSCL (pipeline/fetch/planimetrics.py,
    ~3 min cold) and the live Forestry tree points (~8 min cold, 898,643
    living trees citywide).

    Trees attach to a block face by NEAREST KERB, and sidewalk is sampled
    along its own geometry every config.BLOCK_FACE_SAMPLE_STEP_M so that a
    single OSM way running past many blocks credits each block the pavement
    actually beside it. Both rules and their measurements live in
    pipeline/graph/blockface.py; this function only sequences them.

    Mutates `edges` in place, the same way naming.assign_parent_names does.
    """
    logger.info("[build] planimetrics (kerb lines + CSCL)")
    index = BlockFaceIndex(planimetrics.load("pavement_edge"),
                           planimetrics.load("cscl"))

    logger.info("[build] trees")
    trees = fetch_trees(config.CITY_BBOX, "citywide")
    logger.info(f"[build] {len(trees):,} living trees")

    logger.info("[build] scoring")
    blocks.score_edges(edges, trees, index)

    # Kerb-less pavement gets its shade from the raster instead -- park
    # paths, plazas, steps. Selection is by kind inside, so this cannot
    # touch a sidewalk or a crossing.
    logger.info("[build] park canopy")
    canopy.score_park_paths(edges)

    # And the sidewalks Forestry could NOT answer -- no block face found,
    # or a treeless face inside a city park (Central Park's paths beside
    # its drives) -- fall back to the raster too. Ordinary treeless
    # streets keep their honest zero; crossings are untouched. The
    # populations and their 2026-08-27 measurements are in canopy.py's
    # module docstring.
    logger.info("[build] sidewalk fallback")
    canopy.score_sidewalk_fallback(edges)


def _score_building_shade(edges: list[dict]):
    """Fill `building_shade` on every edge; returns the sun table it used.

    Every edge kind, crossings included -- buildings shade roadways, which
    is why this is not gated on the tree hierarchy's kinds. The Building
    Footprints fetch caches itself (pipeline/fetch/buildings.py); the
    rules for which rows cast shade are on the config constants. Engine,
    sample rule and the Gate 1 measurements: pipeline/scoring/shadows.py.
    """
    logger.info("[build] building shade")
    table = sun.sun_table()
    # Convert first, then score: the raw rows (1.08M dicts, ~1.5 GB) are a
    # temporary of this one expression and are gone before the pass runs.
    footprints = shadows.prepare_buildings(buildings.usable(buildings.load()))
    logger.info(f"  [buildings] {len(footprints[0]):,} footprints prepared")
    shadows.score_building_shade(edges, footprints, table)
    return table


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
    # Building shade rode along: the table that explains the rows, and the
    # rows themselves on at least some edges (all-zero rows are omitted, so
    # "none at all" means the step silently produced nothing).
    sun_table = payload["meta"].get("sun_table")
    if not (isinstance(sun_table, list) and len(sun_table) == 12
            and all(len(row) == 24 for row in sun_table)):
        problems.append("meta.sun_table is not 12 months x 24 hours")
    shaded = sum(1 for edge in payload["edges"] if edge.get("building_shade"))
    if shaded == 0:
        problems.append("no edge carries building_shade")

    if problems:
        for problem in problems:
            logger.error(f"[build] readback MISMATCH -- {problem}")
        return False

    logger.info(f"[build] readback ok: {len(payload['nodes']):,} nodes, "
                f"{len(payload['edges']):,} edges, {shaded:,} with building shade")
    return True


if __name__ == "__main__":
    sys.exit(main())
