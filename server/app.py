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
    GET /route?from_lat=..&from_lon=..&to_lat=..&to_lon=..[&tree_weights=..&tree_weights=..]
              [&month=..&day=..&hour=..&minute=..][&arrive=true][&layers=trees|buildings|both]
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
import math
import os
import time
from contextlib import asynccontextmanager
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# FIRST, before the heavy imports below: cap glibc's malloc arenas
# (server/malloc_arenas.py). The cap only limits arenas created after it,
# and glibc hands a finished thread's arena to later threads even past the
# cap. Measured 2026-09-26 (Linux container, 4 requests at a time): capping
# at the start of lifespan left a second arena from a short-lived thread
# earlier in startup, and one worker filled it to 47 MB (+46 MB over 1,000
# requests); capping here, under uvicorn's command line as on the box,
# kept memory flat (607 -> 601 / 612 MB, two runs).
from pipeline import config  # noqa: E402 -- light (os + pathlib), needed for the value
from server.malloc_arenas import limit_malloc_arenas  # noqa: E402

MALLOC_ARENAS_CAPPED = limit_malloc_arenas(config.SERVER_MALLOC_ARENAS)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from server import geocode as geocoder
from server.graph_store import GraphStore, SHADE_ANCHOR_DAY, SHADE_LAYERS, clamp_shade_monotonic

# Shade is computed for New York's clock, whatever the box's timezone is
# (the droplet runs UTC). tzdata is a runtime dep so this never depends on
# the OS having zone files.
NYC_TZ = ZoneInfo("America/New_York")

# Any leap year: /route checks a requested day against it, so Feb 29 is
# valid whatever year it is now (see the check for why).
LEAP_YEAR = 2028


def parse_pinned_now(raw: str | None) -> datetime | None:
    """SHADEWALKER_NOW -> the moment /route treats as "now", or None.

    ISO 8601; without an offset it is New York clock time
    (2026-07-15T12:00), with one it is converted to it. Raises on
    anything else, so a typo stops the server at startup instead of
    answering every request with a 500."""
    if not raw:
        return None
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=NYC_TZ)
    return moment.astimezone(NYC_TZ)


# TEST-ONLY. The Playwright tier (web/playwright.config.ts) pins "now" so
# the suite checks the same thing whatever the wall clock says: since
# `night-shade` a run after dark gets four identical routes at 100%
# shade, and the shade specs would pass there without testing anything.
# A request's own month/day/hour/minute still win over it. NEVER set in
# production -- the live site's time is the real one.
PINNED_NOW = parse_pinned_now(os.environ.get("SHADEWALKER_NOW"))

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
    # The cap itself ran at import (top of this file); say so once here.
    if MALLOC_ARENAS_CAPPED:
        logging.getLogger(__name__).info(
            f"[app] malloc arenas capped at {config.SERVER_MALLOC_ARENAS}")
    if PINNED_NOW is not None:
        logging.getLogger(__name__).warning(
            f"[app] clock pinned at {PINNED_NOW.isoformat()} (SHADEWALKER_NOW) -- tests only")
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
# for advertising its remaining quota. It also gates slowapi's Retry-After
# on the 429 (its _inject_headers is a no-op without it -- this comment
# used to claim the opposite, disproved by test 2026-09-09), which is why
# the handler below sets that one header itself.
limiter = Limiter(key_func=_client_ip)
app.state.limiter = limiter


def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """slowapi's stock handler answers {"error": ...}; every other error
    this server sends is FastAPI's {"detail": ...}, and the frontend reads
    only that key (web/src/api.ts) -- the stock shape surfaced as a bare
    "Routing failed (429)" (2026-09-09). One shape, one client contract.
    The wording is the sentence body after the frontend's "// ERROR:"
    prefix. Retry-After comes from the same window stats slowapi's own
    injector would read -- (reset epoch seconds, remaining) -- floored at
    one second, since the header is whole seconds."""
    response = JSONResponse(
        {"detail": "too many requests — wait a moment and try again"}, status_code=429,
    )
    limit, identifiers = request.state.view_rate_limit
    reset_at, _remaining = request.app.state.limiter.limiter.get_window_stats(limit, *identifiers)
    response.headers["Retry-After"] = str(max(1, math.ceil(reset_at - time.time())))
    return response


app.add_exception_handler(RateLimitExceeded, _rate_limited)

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
    month: int | None = None,      # time of the walk, New York clock; every part defaults to NOW
    day: int | None = None,        # (month given, day not: the 15th -- the table's anchor)
    hour: int | None = None,
    minute: int | None = None,     # (hour given, minute not: 0 -- the anchor)
    layers: str = "both",          # "trees" | "buildings" | "both": which shade the cost sees
    arrive: bool = False,          # the time given is when the walk ENDS (arrive by), not when it starts
) -> dict:
    # Arriving "now" means nothing: an arrival needs its time.
    if arrive and hour is None:
        raise HTTPException(status_code=400, detail="arrive needs the arrival time (at least hour)")
    # No time given means New York's now -- what the frontend sends until
    # someone sets a time, which then sends all four parts. One clock
    # read, so month/day/hour/minute can't straddle a boundary.
    now = PINNED_NOW or datetime.now(NYC_TZ)
    if month is None:
        month, day = now.month, now.day if day is None else day
    elif day is None:
        day = SHADE_ANCHOR_DAY
    if hour is None:
        hour, minute = now.hour, now.minute if minute is None else minute
    elif minute is None:
        minute = 0
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1-12")
    # Checked against a LEAP year, not this one: the shade table has no
    # year (every month is the anchor year's), so Feb 29 is always a real
    # day to ask about -- a date picked in a leap year, or a shared link
    # opened in the next one. _month_blend lands it half-way to March.
    last_day = calendar.monthrange(LEAP_YEAR, month)[1]
    if not 1 <= day <= last_day:
        raise HTTPException(status_code=400, detail=f"day must be 1-{last_day} for month {month}")
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0-23")
    if not 0 <= minute <= 59:
        raise HTTPException(status_code=400, detail="minute must be 0-59")
    if layers not in SHADE_LAYERS:
        raise HTTPException(status_code=400, detail=f"layers must be one of {', '.join(SHADE_LAYERS)}")
    if not tree_weights:
        raise HTTPException(status_code=400, detail="tree_weights must include at least one value")
    # Each weight costs one real Dijkstra run on a worker thread (two
    # until 2026-09-09, when the start edge's second endpoint was folded
    # into the first run -- graph_store._best_plan); the frontend sends
    # 4. Uncapped, one request with hundreds of weights blocks a worker
    # for seconds (FIXES item 7 / audit §2.1) — see pipeline/config.py's
    # note on the cap for the per-route-length numbers.
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

    # Arrive by (PLAN `time-and-layers`): the fastest route prices an edge
    # by its length alone (weight 0 in graph_store.edge_costs), so its
    # minutes are the same at any hour. Leave that many minutes before the
    # arrival, and score EVERY weight for that one moment: a shadier route
    # really leaves a minute or two earlier still, but one moment keeps the
    # batch comparable -- clamp_shade_monotonic compares the weights'
    # shade against each other. The frontend shows each route's own leave
    # time (the arrival minus that route's minutes).
    if arrive:
        fastest = store.route(start, end, tree_weight=0.0, month=month,
                              day=day, hour=hour, minute=minute, layers=layers)
        if fastest is None:
            raise HTTPException(status_code=422, detail="No path between these points")
        month, day, hour, minute = _leave_time(month, day, hour, minute, fastest["minutes"])

    # After dark every edge is fully shaded (graph_store.is_night), so every
    # tree_weight prices an edge by its length alone and would find the
    # plain shortest path. Route it ONCE, at weight 0, and hand that route
    # to every weight: one Dijkstra instead of four, and the presets cannot
    # split on a floating-point tie (PLAN `night-shade`). In every layer:
    # the dark is the sun's, so "Tree shade" after dark is full shade too
    # (PLAN `time-and-layers`, user 2026-09-26; #112 had exempted it).
    night = store.is_night(month, day, hour, minute)
    results = []
    if night:
        result = store.route(start, end, tree_weight=0.0, month=month,
                             day=day, hour=hour, minute=minute, layers=layers)
        if result is None:
            raise HTTPException(status_code=422, detail="No path between these points")
        results = [result for _ in tree_weights]
    else:
        for tree_weight in tree_weights:
            result = store.route(start, end, tree_weight=tree_weight, month=month,
                                 day=day, hour=hour, minute=minute, layers=layers)
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
        # The moment the shade was computed for (New York clock) and which
        # layers the cost saw -- echoed so a client can show or pin them.
        # With arrive, that moment is the fastest route's leave time.
        "month": month,
        "day": day,
        "hour": hour,
        "minute": minute,
        "arrive": arrive,
        "layers": layers,
        # True when the whole moment is dark: every route above is the same
        # fastest route at full shade, and the frontend says so in one line
        # instead of comparing four identical presets.
        "night": night,
        # All routes share the same street-by-street shape whenever there's
        # no real path to describe (start == end) -- doesn't matter which
        # one this is built from in that case, so the first is as good as
        # any. When there IS a real path, the frontend builds directions
        # from whichever route.properties.segments is actually selected,
        # not from this string -- see RouteStats.tsx.
        "description": _describe(routes[0]["properties"]["segments"]),
    }


def _leave_time(month: int, day: int, hour: int, minute: int,
                walk_minutes: float) -> tuple[int, int, int, int]:
    """When to leave to arrive at month/day/hour/minute after a walk of
    walk_minutes. Rounded to the minute the way the frontend rounds a
    route row's minutes (Math.round: .5 goes up, where Python's round()
    goes to even), so the NONE row's leave time is this exact moment.
    Midnight, a month's end and New Year's Eve are crossed as the calendar
    crosses them, in LEAP_YEAR -- the table has no year, and the one day
    that changes is a walk leaving before midnight on Mar 1, which reads
    Feb 29 for Feb 28: a day apart, in a table blended by the day."""
    arrival = datetime(LEAP_YEAR, month, day, hour, minute)
    leave = arrival - timedelta(minutes=math.floor(walk_minutes + 0.5))
    return leave.month, leave.day, leave.hour, leave.minute


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
