"""Tests for pipeline/fetch/socrata.py's auth-header logic.

No network calls here on purpose — fetch_all_rows() itself (pagination,
caching, the live request) is deliberately out of pytest's scope (see
PLAN.md); this only pins the one piece of that function's behavior a
silent refactor could break without anyone noticing: whether the app
token actually gets attached to outgoing requests.
"""

from pipeline import config
from pipeline.fetch import socrata


def test_auth_headers_includes_token_when_configured(monkeypatch):
    monkeypatch.setattr(config, "SOCRATA_APP_TOKEN", "fake-token-123")
    assert socrata.auth_headers() == {"X-App-Token": "fake-token-123"}


def test_auth_headers_empty_when_token_unset(monkeypatch):
    monkeypatch.setattr(config, "SOCRATA_APP_TOKEN", None)
    assert socrata.auth_headers() == {}
