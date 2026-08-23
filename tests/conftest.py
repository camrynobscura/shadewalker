"""Shared fixtures for the server test suite.

Both are session-scoped: loading the pilot tile takes single-digit
milliseconds (see server/graph_store.py's own load-time comments), but
there's no reason to repeat it once per test when the underlying tile
data never changes between tests.
"""

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import config
from server.app import app
from server.graph_store import GraphStore

def pytest_addoption(parser):
    parser.addoption(
        "--run-external", action="store_true", default=False,
        help="run the external-engine discovery harness (LIVE third-party "
             "calls at ~1 req/s -- see tests/test_external_validation.py)",
    )
    parser.addoption(
        "--external-pairs", type=int, default=30,
        help="how many random pairs the external discovery batch compares",
    )
    parser.addoption(
        "--external-seed", type=int, default=None,
        help="seed for the external discovery batch (default: fresh entropy, printed)",
    )


def pytest_collection_modifyitems(config, items):
    """`external` tests are opt-in via an explicit flag, NOT just the marker:
    marker expressions compose badly with addopts (a routine
    `-m "not citywide"` would otherwise silently re-enable live third-party
    traffic), so the gate is a flag no invocation passes by accident."""
    if config.getoption("--run-external"):
        return
    skip = pytest.mark.skip(
        reason="live external-engine calls only run with --run-external"
    )
    for item in items:
        if "external" in item.keywords:
            item.add_marker(skip)


# The committed pilot-tile fixture. It lives under tests/, not data/tiles/,
# so the production server (which loads every *.json.gz in data/tiles/) can
# never pick it up by accident -- that actually happened: the fixture's
# older OSM fetch loaded alongside the newer citywide tiles and put 559
# stale duplicate edges (and stale tree scores, which won dedupe ties by
# sorting first) into production routing.
PILOT_FIXTURE = Path(__file__).parent / "fixtures" / "pilot.json.gz"


@pytest.fixture(scope="session")
def _pilot_only_tiles_dir(tmp_path_factory):
    """Redirects config.TILES_DIR at a directory holding only a copy of
    the pilot fixture, for the rest of the test session.

    data/tiles/ holds whatever real tiles the pipeline has produced --
    normally the full citywide set. GraphStore.load() merges every
    *.json.gz file it finds there with no filtering, so without this
    isolation these tests would silently route against whatever real data
    happens to be on disk instead of the small, deterministic tile they're
    actually pinned to (a real thing that happened: shade_fraction and a
    geometry-alignment check both "failed" against real Brooklyn+Manhattan
    data sitting alongside the pilot tile, despite pinning neither to it)."""
    isolated_dir = tmp_path_factory.mktemp("pilot_only_tiles")
    shutil.copy(PILOT_FIXTURE, isolated_dir / "pilot.json.gz")

    original_tiles_dir = config.TILES_DIR
    config.TILES_DIR = isolated_dir
    yield
    config.TILES_DIR = original_tiles_dir


@pytest.fixture(scope="session")
def graph_store(_pilot_only_tiles_dir) -> GraphStore:
    """A GraphStore loaded independently of the FastAPI app, for tests that
    call GraphStore methods directly instead of going through /route."""
    store = GraphStore()
    store.load()
    return store


@pytest.fixture(scope="session")
def client(_pilot_only_tiles_dir):
    """A TestClient for the real app. Used as a context manager so FastAPI's
    lifespan hook runs, loading tile data into the app's own GraphStore."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def citywide_store() -> GraphStore:
    """A GraphStore loaded against the REAL production tiles (data/tiles/),
    for the opt-in-by-default citywide sweep (see the `citywide` marker).

    Skips cleanly when that data isn't present -- CI and a fresh clone only
    ever have the committed pilot fixture, never the gitignored citywide
    tiles. Deliberately independent of _pilot_only_tiles_dir: it points at
    the real production directory itself and restores config.TILES_DIR
    afterward, so it coexists with the pilot-isolated fixtures in one test
    session without either clobbering the other's global TILES_DIR. The
    ~15s load is paid once per session, and only when this fixture is
    actually used (i.e. the citywide test wasn't deselected)."""
    production_tiles = Path(os.environ.get("SHADEWALKER_TILES_DIR", config.DATA_DIR / "tiles"))
    tiles = sorted(production_tiles.glob("*.json.gz"))
    if len(tiles) < 10:
        pytest.skip(
            f"citywide tiles not present in {production_tiles} ({len(tiles)} found) "
            "-- run the pipeline to enable the citywide sweep"
        )
    saved_tiles_dir = config.TILES_DIR
    config.TILES_DIR = production_tiles
    try:
        store = GraphStore()
        store.load()
    finally:
        config.TILES_DIR = saved_tiles_dir
    return store
