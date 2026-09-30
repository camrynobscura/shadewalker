"""Tests for the serving shape: one process serves the API and, when a
frontend build exists, the static frontend too -- plus the two security
headers the app owns no matter where it runs.

The mount is conditional on web/dist existing, and this suite must pass
both with and without a real build in the repo (CI has none; a dev
machine that ran Playwright does). So the mount tests never touch the
real `app` or the real web/dist -- they build a fresh FastAPI against a
tmp_path via the same _mount_frontend the real module calls, and only
things independent of dist state (headers, API routes' precedence) are
asserted on the real app.
"""

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pipeline import config
from server import app as server_app


def make_app(monkeypatch, dist_dir) -> TestClient:
    monkeypatch.setattr(config, "WEB_DIST_DIR", dist_dir)
    fresh = FastAPI()

    @fresh.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    server_app._mount_frontend(fresh)
    return TestClient(fresh)


def test_serves_index_and_assets_when_build_exists(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>Shady Stroll</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log('hi')")
    client = make_app(monkeypatch, tmp_path)

    root = client.get("/")
    assert root.status_code == 200
    assert "Shady Stroll" in root.text

    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert "console" in asset.text

    # StaticFiles answers HEAD natively -- only the API routes needed
    # _mirror_head_on_get (tested below on the real app).
    head = client.head("/")
    assert head.status_code == 200
    assert head.content == b""


def test_api_routes_win_over_the_mount(tmp_path, monkeypatch):
    # The mount at "/" catches everything not matched by a route declared
    # before it -- /health must keep answering as the API, not 404 into
    # the static directory.
    (tmp_path / "index.html").write_text("x")
    client = make_app(monkeypatch, tmp_path)
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_build_serves_api_only(tmp_path, monkeypatch):
    client = make_app(monkeypatch, tmp_path / "never-built")
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 404  # no frontend, no pretending


def test_security_headers_on_every_response():
    # On the real app: the middleware wraps API routes and the mount
    # alike. /health is dist-independent, so this asserts safely on any
    # machine. Referrer-Policy strict-origin-when-cross-origin: route URLs
    # carry coordinates in the query string, so only the bare origin ever
    # rides an outbound Referer -- never the path+query (which still lets
    # the CARTO key be domain-locked, since that needs some referer).
    client = TestClient(server_app.app)
    resp = client.get("/health")
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # 404s carry them too -- error responses leak referers just as well.
    missing = client.get("/no-such-path-anywhere")
    assert missing.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_head_mirrors_get_on_every_api_route():
    # FastAPI's @app.get registers only GET -- plain Starlette auto-adds
    # HEAD to GET routes, APIRoute drops that -- so without
    # _mirror_head_on_get a HEAD to any API endpoint misses the router,
    # falls through to the static mount, and 404s, which is what an
    # uptime monitor's probe sends.
    for route in server_app.app.router.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            assert "HEAD" in route.methods, f"{route.path} answers GET but not HEAD"

    # Behavioral check on the one dist- and store-independent endpoint:
    # HEAD returns GET's exact headers (Content-Length included) with no
    # body -- uvicorn and TestClient both drop the body for a HEAD at
    # the protocol layer, so the route needs no body handling of its own.
    client = TestClient(server_app.app)
    get = client.get("/health")
    head = client.head("/health")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert head.headers["content-type"] == get.headers["content-type"]


def test_api_docs_are_disabled():
    # No audience: the API's one client is our own frontend, and public
    # docs would advertise /geocode -- a relay to a fair-use upstream --
    # as a try-it-out endpoint.
    client = TestClient(server_app.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404
