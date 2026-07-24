"""Shared fixtures for the server test suite.

Both are session-scoped: loading the pilot tile takes single-digit
milliseconds (see server/graph_store.py's own load-time comments), but
there's no reason to repeat it once per test when the underlying tile
data never changes between tests.
"""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import config
from server.app import app
from server.graph_store import GraphStore

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
