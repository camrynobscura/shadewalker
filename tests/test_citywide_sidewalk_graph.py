"""Invariants for the sidewalk model's citywide export.

Run against the real data/export/citywide.json.gz when it is present, and
skipped otherwise -- that file is gitignored and ~16MB, so CI and a fresh
clone never have it. Build it with:

    uv run python -m pipeline.build

DELIBERATELY NOT USING conftest's citywide_store FIXTURE. These read the
export file directly rather than loading it through GraphStore, so they
check what the PIPELINE wrote rather than what the server made of it --
a distinction that matters when the question is whether the export itself
is right.

(The original reason was different and is now obsolete: citywide_store
used to require 10+ tile files, a threshold left over from the centerline
era's 276-tile grid, so it always skipped. That gate was fixed on
2026-08-23 to skip only when there is no export at all.)

WHAT THESE ARE FOR
------------------
Every one of them pins something that has already gone wrong, or that
would look like something else when it does:

  - a clip failure produces entirely plausible numbers (2026-08-22: an
    unclipped read reported 37.1% connectivity, which reads as a routing
    catastrophe and was purely Buffalo being measured beside Brooklyn)
  - a dangling edge endpoint is a KeyError at server startup, not a
    routing oddity
  - Staten Island being unreachable is CORRECT, and looks like a bug
"""

import gzip
import json
import os
from pathlib import Path

import pytest

from pipeline import config, export
from pipeline.fetch.boundaries import fetch_borough_boundaries
from pipeline.graph.boundary import nyc_boundary
from server.graph_store import GraphStore

# How far past the five-borough bounding box a node may legitimately sit.
# Not zero, because the clip keeps a border-crossing way WHOLE rather than
# trimming it -- a bridge into New Jersey drags its far end along with it,
# which is the intended behaviour (pipeline/graph/pedestrian.py). 0.1
# degrees is ~11km: comfortably past any real bridge tail, and nowhere
# near the statewide extract's own reach (-79.7 lon, 45.0 lat), which is
# what this is actually guarding against.
BBOX_TOLERANCE_DEG = 0.1

# Two coordinates taken off real nodes in the built graph (2026-08-23):
# one in Staten Island's own component, one in the main component that
# holds Manhattan, Brooklyn, Queens and the Bronx.
STATEN_ISLAND = (40.54723, -74.15724)
MAINLAND = (40.71887, -74.00472)


def _production_export_dir() -> Path:
    return Path(os.environ.get("SHADEWALKER_EXPORT_DIR",
                               config.DATA_DIR / "export"))


@pytest.fixture(scope="session")
def sidewalk_export() -> dict:
    """The parsed citywide export, or a skip if it hasn't been built."""
    path = _production_export_dir() / f"{export.CITYWIDE_NAME}.json.gz"
    if not path.exists():
        pytest.skip(f"no citywide export at {path} -- "
                    "run `uv run python -m pipeline.build`")
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def sidewalk_store(sidewalk_export) -> GraphStore:
    """A GraphStore loaded from the real citywide export.

    Depends on sidewalk_export purely for its skip: if the file is
    missing, skip before paying a load.
    """
    production = _production_export_dir()
    saved = config.EXPORT_DIR
    config.EXPORT_DIR = production
    try:
        store = GraphStore()
        store.load()
    finally:
        config.EXPORT_DIR = saved
    return store


@pytest.mark.citywide
def test_the_export_covers_only_new_york_city(sidewalk_export):
    """The pinned extract is Geofabrik's NEW YORK STATE file, so a read
    that forgets to clip silently includes Buffalo, Albany and the
    Adirondacks.

    That is not a hypothetical: it happened on 2026-08-22 and produced a
    graph of 868,339 nodes whose largest component read 37.1%, a figure
    that looks exactly like a broken pedestrian network and was entirely
    an artifact of the missing clip. Nothing about the numbers themselves
    gave it away -- only their geography did.
    """
    nyc = nyc_boundary(fetch_borough_boundaries())
    min_lon, min_lat, max_lon, max_lat = nyc.bounds

    lons = [lon for lon, _ in sidewalk_export["nodes"].values()]
    lats = [lat for _, lat in sidewalk_export["nodes"].values()]
    assert lons, "export has no nodes"

    assert min(lons) >= min_lon - BBOX_TOLERANCE_DEG, (
        f"westernmost node at {min(lons):.3f} is more than "
        f"{BBOX_TOLERANCE_DEG} deg west of NYC ({min_lon:.3f}) -- the read "
        f"is probably not being clipped to the boroughs"
    )
    assert max(lons) <= max_lon + BBOX_TOLERANCE_DEG, (
        f"easternmost node at {max(lons):.3f} is east of NYC ({max_lon:.3f})"
    )
    assert min(lats) >= min_lat - BBOX_TOLERANCE_DEG, (
        f"southernmost node at {min(lats):.3f} is south of NYC ({min_lat:.3f})"
    )
    assert max(lats) <= max_lat + BBOX_TOLERANCE_DEG, (
        f"northernmost node at {max(lats):.3f} is north of NYC "
        f"({max_lat:.3f}) -- upstate data would blow past this"
    )


@pytest.mark.citywide
def test_every_edge_endpoint_resolves_to_an_exported_node(sidewalk_export):
    """server/graph_store.py's load() indexes edges by looking their u/v
    up in the node table it built in pass 1. An endpoint with no node is
    a bare KeyError at server startup -- before any request, with nothing
    naming the edge that caused it.

    Checked against the FILE rather than a loaded store on purpose: a
    loaded store proves nothing here, since load() would already have
    crashed on the way in.
    """
    nodes = sidewalk_export["nodes"]
    dangling = [
        (edge["u"], edge["v"])
        for edge in sidewalk_export["edges"]
        if edge["u"] not in nodes or edge["v"] not in nodes
    ]
    assert not dangling, (
        f"{len(dangling):,} edge(s) reference a node the export doesn't "
        f"contain, e.g. {dangling[:3]}"
    )


@pytest.mark.citywide
def test_staten_island_does_not_reach_the_mainland(sidewalk_store):
    """This is CORRECT behaviour, pinned so it doesn't get "fixed".

    No pedestrian way crosses the Verrazzano. Staten Island's only walking
    links to anywhere are the Bayonne Bridge and Goethals shared-use path,
    both into New Jersey, which the five-borough clip excludes. So all
    40,041 of its nodes sit in their own components, and a Manhattan ->
    Staten Island request has no route.

    Per the governing rule that is the right answer -- OSM says there is
    no crossing, so we don't invent one. snap_pair() returns None and
    server/app.py:152-154 turns that into a clean 422.
    """
    pair = sidewalk_store.snap_pair(
        STATEN_ISLAND[0], STATEN_ISLAND[1], MAINLAND[0], MAINLAND[1]
    )
    assert pair is None, (
        "Staten Island now reaches the mainland. Either OSM gained a "
        "pedestrian crossing (check it, then update this test), or the "
        "clip has started including New Jersey and routing through it."
    )


@pytest.mark.citywide
def test_staten_island_still_routes_within_itself(sidewalk_store):
    """The flip side of the test above: severed from the mainland is not
    the same as broken. Staten Island's own 31,659-node component has to
    route internally, or "no route to SI" would be hiding "SI doesn't
    work at all"."""
    near_by = (40.54903, -74.15188)   # ~500m from STATEN_ISLAND
    pair = sidewalk_store.snap_pair(
        STATEN_ISLAND[0], STATEN_ISLAND[1], near_by[0], near_by[1]
    )
    assert pair is not None, (
        "two points within Staten Island can't reach each other -- its "
        "internal network is broken, not merely cut off from the mainland"
    )
