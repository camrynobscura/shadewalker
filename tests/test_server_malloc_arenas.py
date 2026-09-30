"""The malloc-arena cap (#111): applied on Linux, a no-op
elsewhere, and applied when server/app.py is imported -- before the heavy
imports and before startup, which a measurement showed is what it takes."""
import sys

from pipeline import config
from server import app as server_app
from server.malloc_arenas import limit_malloc_arenas

ON_LINUX = sys.platform.startswith("linux")


def test_the_cap_applies_on_linux_and_is_a_no_op_elsewhere():
    # CI runs on Linux, so this exercises the real mallopt call there;
    # on the Mac it proves the call degrades to a clean False.
    assert limit_malloc_arenas(config.SERVER_MALLOC_ARENAS) is ON_LINUX


def test_importing_the_app_already_applied_the_cap():
    assert server_app.MALLOC_ARENAS_CAPPED is ON_LINUX


def test_the_cap_sits_above_the_heavy_imports():
    # The ordering is the fix (a later cap left a second 47 MB arena), so
    # pin it: the cap line must come before fastapi and graph_store load.
    source = open(server_app.__file__).read()
    cap = source.index("MALLOC_ARENAS_CAPPED = limit_malloc_arenas(")
    assert cap < source.index("from fastapi import")
    assert cap < source.index("from server.graph_store import")
