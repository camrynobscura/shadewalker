"""App-level rate limiting (slowapi).

Chosen over Caddy-edge limiting because the edge plugin needs a custom
Caddy binary with no clean update path, while this rejects an over-limit
request before the view body runs (no Dijkstra, no upstream hop) and is
testable in-process -- which is what this file does.

Pinned here:
- the client-IP key prefers X-Real-IP (set by Caddy via header_up, so it
  can't be spoofed) and falls back to the socket peer in dev;
- /geocode and /geocode/reverse share one budget (`scope="geocode"`), so a
  single client can't hammer the fair-use Photon upstream through either
  door; over budget returns 429;
- limits are per client IP -- one abuser can't throttle everyone;
- /health is never limited (monitoring must not be throttled).

The limiter is disabled for the rest of the suite (tests/conftest.py's
autouse fixture), since its counters are per-process; `rate_limiter_on`
re-enables it here, and every test uses a unique X-Real-IP so its bucket
is independent of the others.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import config
from server import app as server_app
from server import geocode
from server.app import _client_ip

# No context manager on purpose: lifespan (and the graph load) only runs
# inside `with`, and /geocode + /health don't need the graph. The one test
# that hits /route loads the module store itself (loaded_store).
client = TestClient(server_app.app)


def _limit_count(limit_str: str) -> int:
    """'60/minute' -> 60 -- so the loops track the config, not a literal."""
    return int(limit_str.split("/")[0])


GEOCODE_N = _limit_count(config.GEOCODE_RATE_LIMIT)


@pytest.fixture
def rate_limiter_on():
    limiter = server_app.app.state.limiter
    limiter.enabled = True
    yield
    limiter.enabled = False


@pytest.fixture(autouse=True)
def _clear_geocode_caches():
    # A cache hit would skip the upstream but still counts against the
    # limiter; clearing keeps each test's mocking clean regardless.
    geocode.clear_caches()
    yield
    geocode.clear_caches()


class _FakePhoton:
    status_code = 200

    def json(self) -> dict:
        return {"type": "FeatureCollection", "features": []}


def _mock_photon(monkeypatch):
    monkeypatch.setattr(
        geocode._session, "get",
        lambda url, params=None, timeout=None: _FakePhoton(),
    )


# --- the client-IP key -------------------------------------------------------

class _Req:
    def __init__(self, headers: dict, host: str):
        self.headers = headers
        self.client = type("Client", (), {"host": host})()


def test_client_ip_prefers_x_real_ip():
    # Behind Caddy the socket peer is localhost; the real visitor is in
    # X-Real-IP, and that must win.
    req = _Req({"X-Real-IP": "203.0.113.9"}, host="127.0.0.1")
    assert _client_ip(req) == "203.0.113.9"


def test_client_ip_falls_back_to_peer_without_the_header():
    # Dev / no proxy: no X-Real-IP, so the socket peer is the honest key.
    req = _Req({}, host="198.51.100.4")
    assert _client_ip(req) == "198.51.100.4"


# --- the 429 behavior --------------------------------------------------------

def test_geocode_over_its_limit_returns_429(monkeypatch, rate_limiter_on):
    _mock_photon(monkeypatch)
    ip = {"X-Real-IP": "203.0.113.20"}
    for i in range(GEOCODE_N):
        r = client.get("/geocode", params={"q": f"court st {i}"}, headers=ip)
        assert r.status_code == 200, f"request {i} was {r.status_code}, expected 200"
    over = client.get("/geocode", params={"q": "one too many"}, headers=ip)
    assert over.status_code == 429
    # Our handler, not slowapi's: the same {"detail": ...} shape as every
    # other error, which is the only key the frontend reads (api.ts), plus
    # slowapi's own Retry-After.
    assert over.json() == {"detail": "too many requests — wait a moment and try again"}
    # Whole seconds until the window resets; never 0 (a client would retry
    # instantly and hit the limit again).
    assert int(over.headers["retry-after"]) >= 1


def test_geocode_forward_and_reverse_share_one_budget(monkeypatch, rate_limiter_on):
    # The budget protects the Photon upstream, so it must be pooled: half
    # the calls on each endpoint together exhaust the single scope.
    _mock_photon(monkeypatch)
    ip = {"X-Real-IP": "203.0.113.21"}
    half = GEOCODE_N // 2
    for i in range(half):
        assert client.get("/geocode", params={"q": f"a{i}"}, headers=ip).status_code == 200
    for i in range(GEOCODE_N - half):
        r = client.get("/geocode/reverse",
                       params={"lat": 40.70, "lon": -73.90 - i * 0.001}, headers=ip)
        assert r.status_code == 200
    # One more on either endpoint is over the shared budget.
    assert client.get("/geocode/reverse",
                      params={"lat": 40.70, "lon": -74.50}, headers=ip).status_code == 429


def test_the_limit_is_per_client_ip(monkeypatch, rate_limiter_on):
    _mock_photon(monkeypatch)
    a = {"X-Real-IP": "203.0.113.22"}
    b = {"X-Real-IP": "203.0.113.23"}
    for i in range(GEOCODE_N):
        client.get("/geocode", params={"q": f"x{i}"}, headers=a)
    assert client.get("/geocode", params={"q": "over"}, headers=a).status_code == 429  # A spent
    assert client.get("/geocode", params={"q": "fresh"}, headers=b).status_code == 200  # B untouched


def test_health_is_never_rate_limited(rate_limiter_on):
    ip = {"X-Real-IP": "203.0.113.24"}
    for _ in range(GEOCODE_N + 5):
        assert client.get("/health", headers=ip).status_code == 200


# --- /route is actually protected too (needs the graph) ----------------------

@pytest.fixture
def loaded_store():
    """/route needs the module-global store. Load it against the real
    export; skip cleanly when that data isn't present (same gate as
    citywide_store)."""
    production = Path(os.environ.get("SHADEWALKER_EXPORT_DIR", config.DATA_DIR / "export"))
    if not sorted(production.glob("*.json.gz")):
        pytest.skip("no citywide export -- run `uv run python -m pipeline.build`")
    saved = config.EXPORT_DIR
    config.EXPORT_DIR = production
    try:
        server_app.store.load()
    finally:
        config.EXPORT_DIR = saved
    yield


@pytest.mark.citywide
def test_route_is_rate_limited(rate_limiter_on, loaded_store):
    ip = {"X-Real-IP": "203.0.113.30"}
    # A known-routable Manhattan pair (the John St anchor site).
    params = {"from_lat": 40.717267, "from_lon": -73.977333,
              "to_lat": 40.704456, "to_lon": -73.986651}
    n = _limit_count(config.ROUTE_RATE_LIMIT)
    for i in range(n):
        r = client.get("/route", params=params, headers=ip)
        assert r.status_code == 200, f"request {i} was {r.status_code}: {r.text[:160]}"
    assert client.get("/route", params=params, headers=ip).status_code == 429
