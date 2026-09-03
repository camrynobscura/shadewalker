"""Shared fixtures for the test suite.

Only one fixture survives here: `citywide_store`, which loads the real
production tiles for the opt-in citywide sweep.

The pilot-tile fixtures (`PILOT_FIXTURE`, `_pilot_only_export_dir`,
`client`, `graph_store`, `self_loop_store`) were deleted on 2026-08-23
along with `tests/fixtures/pilot.json.gz` itself. That fixture was
centerline data, and every test routing against it was pinned to
measurements of a graph the project no longer builds. Route-level testing
comes back once, against real citywide data, when sidewalk scoring has
settled -- rather than being ported piecemeal onto a fixture that kept
mixing the two models together.

Tests that need a graph now build their own tiny synthetic tile with
`tmp_path` (see test_graph_store_pruning.py, test_graph_store_components.py
and test_pipeline_export.py for the pattern), which
keeps each one's assumptions visible in the test itself.
"""

import os
from pathlib import Path

import pytest

from pipeline import config
from server.graph_store import GraphStore


@pytest.fixture(autouse=True)
def _rate_limiter_off_by_default():
    """The slowapi limiter's counters are per-process and in-memory, so
    cumulative endpoint calls across unrelated tests could otherwise trip a
    429 and fail a test that has nothing to do with rate limiting. Off by
    default for the whole suite; test_server_ratelimit.py opts back in
    (with its own fresh per-IP buckets) for its own assertions."""
    from server.app import app
    app.state.limiter.enabled = False
    yield


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


@pytest.fixture(scope="session")
def citywide_store() -> GraphStore:
    """A GraphStore loaded against the REAL production export
    (data/export/), for the opt-in-by-default citywide sweep (see the
    `citywide` marker).

    Skips cleanly when that data isn't present, so CI and a fresh clone
    never fail for lack of the gitignored citywide export. Saves and
    restores config.EXPORT_DIR around the load rather than leaving it
    pointed at production for the rest of the session. The ~15s load is
    paid once per session, and only when this fixture is actually used
    (i.e. the citywide test wasn't deselected)."""
    production_export = Path(os.environ.get("SHADEWALKER_EXPORT_DIR", config.DATA_DIR / "export"))
    exports = sorted(production_export.glob("*.json.gz"))
    # Was `< 10`, from the tiled centerline pipeline that emitted one file
    # per tile. The sidewalk pipeline emits exactly ONE citywide export, so
    # that condition could never be satisfied and every citywide test
    # skipped permanently -- present data, zero coverage, green suite.
    if not exports:
        pytest.skip(
            f"no citywide export in {production_export} "
            "-- run `uv run python -m pipeline.build` to enable the citywide sweep"
        )
    saved_export_dir = config.EXPORT_DIR
    config.EXPORT_DIR = production_export
    try:
        store = GraphStore()
        store.load()
    finally:
        config.EXPORT_DIR = saved_export_dir
    return store
