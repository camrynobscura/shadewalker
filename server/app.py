"""The routing server — and, when web/dist exists, the whole app. Run with:

    uv run uvicorn server.app:app --port 8000

Production adds two flags (decided 2026-08-30, `abuse-and-privacy`):

    uvicorn server.app:app --port 8000 --no-access-log --no-server-header

--no-access-log because the access log ties each visitor's IP to their
searched text (/geocode?q=...) and exact route coordinates — location
data that must not accumulate in a file by default; --no-server-header
to stop advertising the stack. App-level logs (startup, warnings) stay.

FastAPI ≈ Express for Python: routes are functions, decorated with their
path. Two extras Express doesn't give you for free: every query parameter
is parsed + validated from the type hints (bad input → automatic 422 with
a clear message, no manual checks), and interactive API docs are generated
at /docs.

Endpoints:
    GET /health
    GET /route?from_lat=..&from_lon=..&to_lat=..&to_lon=..[&tree_weights=..&tree_weights=..][&month=..]
    GET /geocode?q=..[&limit=..]
    GET /geocode/reverse?lat=..&lon=..

/route computes a route for EVERY requested tree_weight in one call, not
just one — the frontend's Shade_priority control has four fixed presets
(NONE/LOW/MED/MAX), and a walker comparing them by flipping back and forth
was firing a fresh network request on every click. Each extra Dijkstra run
costs microseconds on this in-memory graph, so computing all four up front
and letting the frontend cache + switch between them locally is strictly
better than re-fetching per click.

Data refresh = restart, by design (FIXES item 8, decided 2026-08-17):
tiles load once at startup and there is deliberately NO live-reload
path. The refresh cadence is monthly (a tree re-score; the source
dataset only updates biweekly), so the refresh story is: re-run the
pipeline (exports are atomic, tmp+rename — a running server can never
read a half-written tile), then restart the server (load measured 5.9s
on the laptop, 2026-09-03 — down from ~15s once the coverage-ring
compute was deleted with the map's coverage outline). Building a safe in-flight reload
mechanism costs real threading care and buys nothing at that cadence;
revisit only if the hosting platform's restart story turns out to be
painful or the refresh cadence tightens dramatically.
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from pipeline import config
from server import geocode as geocoder
from server.graph_store import GraphStore, clamp_shade_monotonic

# Route graph_store's loggers somewhere visible under uvicorn, which
# configures its own loggers but leaves the root logger bare (FIXES item
# 9) -- without this, load()'s startup summary and the dead-gap-entry
# WARNING would silently vanish. basicConfig is a no-op if some outer
# process (tests, a managed host) already configured handlers, so this
# never overrides a real deployment's logging setup.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

store = GraphStore()


# lifespan = FastAPI's startup/shutdown hook (the modern replacement for
# @app.on_event). Everything before `yield` runs once before the first
# request — here, the one-time load of all tiles into memory.
@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load()
    yield


# docs_url/redoc_url/openapi_url=None: the API has exactly one intended
# client (our own frontend), so public interactive docs have no audience
# -- and they'd advertise /geocode, a relay to a fair-use upstream, as a
# documented try-it-out endpoint. Don't volunteer that (decided
# 2026-08-30, same spirit as --no-server-header). We never use /docs in
# dev either -- curl is the house tool; re-enabling is this one line.
app = FastAPI(title="Shade Walker", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# Per-client rate limiting (slowapi, app-level; decided 2026-08-31,
# `hosting`). App-level rather than at the Caddy edge because the edge
# plugin needs a custom Caddy binary with no clean update path -- and a
# 429 here still short-circuits BEFORE the view body runs (no Dijkstra, no
# upstream hop), so it protects the single worker just the same, while
# being testable in-process. Keyed by the real client IP: Caddy passes it
# as X-Real-IP (header_up, so a client can't spoof it); with no proxy in
# front (dev) we fall back to the socket peer. Limits live as decorators on
# the endpoints below -- /health and the static mount stay unlimited,
# since monitoring and page loads must never be throttled.
def _client_ip(request: Request) -> str:
    return request.headers.get("X-Real-IP") or get_remote_address(request)


# No headers_enabled: that makes slowapi inject X-RateLimit-* headers into
# each response, which requires the endpoint to return a Response object
# (ours return plain dicts) -- and a private API with one client has no use
# for advertising its remaining quota. The 429 (with Retry-After) still
# fires from the exception handler regardless.
limiter = Limiter(key_func=_client_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# The Playwright tier (web/playwright.config.ts) drives many /route calls
# from a single localhost IP in seconds, which would trip the production
# limit and make the suite flaky. It sets this env var to turn limiting off,
# the same isolation the pytest conftest does. NEVER set in production.
if os.environ.get("SHADEWALKER_DISABLE_RATE_LIMIT"):
    limiter.enabled = False


# Applies to every response, static files included (middleware wraps the
# mount too). Only the two headers that belong to the APP no matter where
# it runs: Referrer-Policy (strict-origin-when-cross-origin) because route
# URLs carry coordinates in the query string -- this keeps the path+query
# off every outbound Referer (only the bare origin is ever sent), while
# still letting the CARTO basemap key be locked to our domain, which needs
# SOME referer to verify against (decided 2026-08-31, `hosting`); nosniff
# because we serve user-adjacent JSON and static
# files from one origin. The rest of the header story (CSP, HSTS) is
# deliberately NOT here -- it depends on final asset origins and TLS, so
# it lives in the Caddy layer at hosting time (PLAN.md, `hosting`).
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/geocode")
@limiter.shared_limit(config.GEOCODE_RATE_LIMIT, scope="geocode")
def geocode_search(
    request: Request,
    q: str = Query(min_length=1, max_length=config.MAX_GEOCODE_QUERY_CHARS),
    limit: int = Query(default=1, ge=1, le=config.MAX_GEOCODE_RESULTS),
) -> dict:
    """Forward geocoding via the Photon proxy (server/geocode.py — the
    whole why lives on that module's docstring). limit=1 is the address
    field's submit-time resolve; higher limits are the autocomplete
    dropdown's. NYC bbox and language are pinned server-side."""
    q = " ".join(q.split())  # normalize whitespace so cache keys collapse
    if not q:
        raise HTTPException(status_code=422, detail="q must not be blank")
    try:
        results = geocoder.search(q, limit)
    except geocoder.UpstreamError:
        # Deliberately does NOT echo q back: query text is location data
        # and this detail string is the only thing we'd ever emit it in.
        raise HTTPException(status_code=502, detail="Geocoding is temporarily unavailable")
    return {"results": list(results)}


@app.get("/geocode/reverse")
@limiter.shared_limit(config.GEOCODE_RATE_LIMIT, scope="geocode")
def geocode_reverse(
    request: Request,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
) -> dict:
    """Point → short address label, or null when nothing address-shaped
    is nearby (the frontend then keeps showing coordinates). Rounded to
    5dp (~1m) so a re-click of the same spot is a cache hit."""
    try:
        label = geocoder.reverse(round(lat, 5), round(lon, 5))
    except geocoder.UpstreamError:
        raise HTTPException(status_code=502, detail="Geocoding is temporarily unavailable")
    return {"label": label}


@app.get("/route")
@limiter.limit(config.ROUTE_RATE_LIMIT)
def route(
    request: Request,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    tree_weights: list[float] = Query(default=[0.0, 5.0, 15.0, 40.0]),  # defaults double as API docs
    month: int | None = None,                                          # None → current month (server clock)
) -> dict:
    if month is None:
        month = datetime.now().month
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if not tree_weights:
        raise HTTPException(status_code=400, detail="tree_weights must include at least one value")
    # Each weight costs two real Dijkstra runs on a worker thread
    # (~25ms/run on the dev Mac, ~110ms on the droplet, profiled
    # 2026-09-01); the frontend sends 4. Uncapped, one request with
    # hundreds of weights blocks a worker for seconds (FIXES item 7 /
    # audit §2.1) — see pipeline/config.py's note on the cap for the
    # per-route-length numbers.
    if len(tree_weights) > config.MAX_TREE_WEIGHTS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"tree_weights accepts at most {config.MAX_TREE_WEIGHTS_PER_REQUEST} values",
        )
    for tree_weight in tree_weights:
        if not 0 <= tree_weight <= config.MAX_TREE_WEIGHT:
            raise HTTPException(
                status_code=400,
                detail=f"tree_weight must be between 0 and {config.MAX_TREE_WEIGHT}",
            )

    # Snapping alone can't tell "outside our data" from "a real address" —
    # it always returns the nearest node, however far away. Without this,
    # a destination beyond the pilot tile's edge silently snapped to the
    # tile boundary instead of reaching where the user actually asked for.
    if not store.in_coverage(from_lat, from_lon):
        raise HTTPException(
            status_code=422,
            detail="Start point is outside our current coverage area — pick a point on land in New York City.",
        )
    if not store.in_coverage(to_lat, to_lon):
        raise HTTPException(
            status_code=422,
            detail="End point is outside our current coverage area — pick a point on land in New York City.",
        )

    pair = store.snap_pair(from_lat, from_lon, to_lat, to_lon)
    if pair is None:
        raise HTTPException(status_code=422, detail="No path between these points")
    start, end = pair

    results = []
    for tree_weight in tree_weights:
        result = store.route(start, end, tree_weight=tree_weight, month=month)
        if result is None:
            raise HTTPException(status_code=422, detail="No path between these points")
        results.append(result)

    # Guarantee the Shade_priority promise: a higher tree_weight must never
    # come back with less shade than a lower one. route() optimizes a smooth
    # density cost while shade_fraction is a thresholded stat, so the two can
    # disagree -- clamp the batch to non-decreasing shade before returning
    # (only ever falls back to a route already computed here). See
    # clamp_shade_monotonic's docstring for the full why.
    results = clamp_shade_monotonic(results, tree_weights)
    routes = [_to_feature(result, tree_weight) for result, tree_weight in zip(results, tree_weights)]

    return {
        "routes": routes,
        "snapped": {
            # Where the request actually starts/ends once resolved onto the
            # street network — the frontend draws the A/B marker here
            # instead of at the raw clicked/geocoded point, since that
            # point can sit mid-block. One shared pair, not one per
            # feature: the snap itself doesn't depend on tree_weight, only
            # which side of it a given route connects through does.
            "start": {"lat": start.point[1], "lon": start.point[0]},
            "end": {"lat": end.point[1], "lon": end.point[0]},
        },
        "month": month,
        # All routes share the same street-by-street shape whenever there's
        # no real path to describe (start == end) -- doesn't matter which
        # one this is built from in that case, so the first is as good as
        # any. When there IS a real path, the frontend builds directions
        # from whichever route.properties.segments is actually selected,
        # not from this string -- see RouteStats.tsx.
        "description": _describe(routes[0]["properties"]["segments"]),
    }


def _to_feature(route: dict, tree_weight: float) -> dict:
    """Wrap a GraphStore route as GeoJSON — the lingua franca of web maps;
    Leaflet renders it directly."""
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": route["coords"]},
        "properties": {
            "tree_weight": tree_weight,
            "length_m": route["length_m"],
            "minutes": route["minutes"],
            "tree_count": route["tree_count"],
            "shade_fraction": route["shade_fraction"],
            "park_canopy_share": route["park_canopy_share"],
            "segments": route["segments"],
        },
    }


def _fmt_dist(metres: float) -> str:
    return f"{metres / 1000:.1f} km" if metres >= 1000 else f"{metres:.0f} m"


def _describe(segments: list[dict]) -> str:
    """Turn-by-turn text directions — the accessible (screen-reader) twin
    of the drawn polyline. Same steps RouteStats.tsx renders as a list;
    the phrasing here and there must tell the same story."""
    if not segments:
        return "You are already at your destination."
    parts = []
    for step in segments:
        dist = _fmt_dist(step["length_m"])
        side = f" ({step['side']} side)" if step["side"] else ""
        if step["action"] == "depart":
            parts.append(f"Head {step['heading']} on {step['name']}{side} "
                         f"for {dist}")
        elif step["action"] == "continue":
            parts.append(f"continue onto {step['name']}{side} for {dist}")
        elif step["action"] == "cross_side":
            parts.append(f"cross to the {step['side']} side of "
                         f"{step['name']} and continue for {dist}")
        else:  # left / right / sharp_left / sharp_right
            word = step["action"].replace("_", " ")
            parts.append(f"turn {word} onto {step['name']}{side} for {dist}")
    return ", then ".join(parts) + "."


def _mirror_head_on_get(app: FastAPI) -> None:
    """Answer HEAD on every GET endpoint — restoring what plain Starlette
    does by default (its Route.__init__ auto-adds HEAD to any GET route)
    and FastAPI's APIRoute drops. Without this, HEAD to any API endpoint
    missed the router entirely and fell through to the static mount as a
    404 — found live by UptimeRobot's HEAD probes (2026-09-01). No body
    handling needed here: the handler runs and uvicorn drops the body at
    the protocol layer for a HEAD request (h11_impl:
    `data = b"" if method == "HEAD" else body`), so the response carries
    GET's exact headers, Content-Length included, per RFC 9110. A HEAD
    therefore costs the same work as its GET (rate-limited identically);
    fine — the monitors use keyword GETs anyway, this is HTTP
    correctness. The static mount needs no help: StaticFiles answers
    HEAD natively. Must run after every endpoint above is declared."""
    for route in app.router.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            route.methods.add("HEAD")


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built frontend (web/dist) from the same process as the
    API — the serving shape decided 2026-08-30: `uvicorn server.app:app`
    IS the whole application, deployable anywhere that runs one process,
    with the Caddy layer at hosting time purely additive in front.

    Mounted at "/" AFTER every route above, so /route, /geocode etc.
    always win and everything else falls through to static files
    (html=True serves index.html for "/"). Conditional on the build
    existing: dev serves the frontend from vite, CI and fresh checkouts
    have no dist/ — in those the server is simply API-only, same as it
    always was, and says so once at startup instead of failing.

    StaticFiles sends ETag/Last-Modified; long-lived Cache-Control for
    the content-hashed /assets bundle is Caddy-layer polish, not done
    here."""
    if config.WEB_DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=config.WEB_DIST_DIR, html=True), name="frontend")
        logging.info("serving frontend from %s", config.WEB_DIST_DIR)
    else:
        logging.info("no frontend build at %s -- serving API only", config.WEB_DIST_DIR)


_mirror_head_on_get(app)
_mount_frontend(app)
