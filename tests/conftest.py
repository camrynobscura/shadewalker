"""Shared fixtures for the server test suite.

Both are session-scoped: loading the pilot tile takes single-digit
milliseconds (see server/graph_store.py's own load-time comments), but
there's no reason to repeat it once per test when the underlying tile
data never changes between tests.
"""

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.graph_store import GraphStore


@pytest.fixture(scope="session")
def graph_store() -> GraphStore:
    """A GraphStore loaded independently of the FastAPI app, for tests that
    call GraphStore methods directly instead of going through /route."""
    store = GraphStore()
    store.load()
    return store


@pytest.fixture(scope="session")
def client():
    """A TestClient for the real app. Used as a context manager so FastAPI's
    lifespan hook runs, loading tile data into the app's own GraphStore."""
    with TestClient(app) as c:
        yield c
