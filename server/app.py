"""The routing server. Run with:

    uv run uvicorn server.app:app --port 8000

FastAPI ≈ Express for Python: routes are functions, decorated with their
path. Two extras Express doesn't give you for free: every query parameter
is parsed + validated from the type hints (bad input → automatic 422 with
a clear message, no manual checks), and interactive API docs are generated
at /docs.

Endpoints:
    GET /health
    GET /coverage
    GET /route?from_lat=..&from_lon=..&to_lat=..&to_lon=..[&tree_weights=..&tree_weights=..][&month=..]

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
read a half-written tile), then restart the server (~15s + a coverage
recompute when the tile set changed). Building a safe in-flight reload
mechanism costs real threading care and buys nothing at that cadence;
revisit only if the hosting platform's restart story turns out to be
painful or the refresh cadence tightens dramatically.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query

from pipeline import config
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


app = FastAPI(title="Shadewalker", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/coverage")
def coverage() -> dict:
    """The loaded data's actual outline as a GeoJSON MultiPolygon, so the
    frontend can draw it on the map — computed from whatever tiles are
    loaded, no server code change needed as Stage 2 adds more. A
    buffered-streets footprint, not a bounding box: with Brooklyn-sized
    coverage, a box claimed water and Lower Manhattan as clickable area
    that /route would then reject — the drawn line should be one users
    can trust. MultiPolygon (not Polygon) because GraphStore.load() keeps
    every real connected component now, not just the largest — Governors
    Island and eventually Staten Island get their own separate piece
    rather than being silently left off the map while still routable."""
    frame = store.coverage_frame()
    return {
        "type": "Feature",
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [[ring] for ring in store.coverage_rings()],
        },
        # The offshore frame (FIXES item 12): what the frontend actually
        # DRAWS -- one generous dashed boundary through the water plus a
        # two-step feathered dim -- while the geometry above remains the
        # true click-acceptance region. See server/coverage_frame.py.
        "properties": {
            "frame": frame.get("frame", []),
            "frame_feather_350": frame.get("feather_350", []),
            "frame_feather_800": frame.get("feather_800", []),
        },
    }


@app.get("/route")
def route(
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
    # Each weight is a real ~13ms Dijkstra pass on a worker thread; the
    # frontend sends 4. Uncapped, one request with hundreds of weights
    # blocks a worker for seconds (FIXES item 7 / audit §2.1).
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
            detail="Start point is outside our current coverage area — pick a point on land inside the dashed frame shown on the map.",
        )
    if not store.in_coverage(to_lat, to_lon):
        raise HTTPException(
            status_code=422,
            detail="End point is outside our current coverage area — pick a point on land inside the dashed frame shown on the map.",
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


def _describe(segments: list[dict]) -> str:
    """Street-by-street text directions — the accessible (screen-reader)
    twin of the drawn polyline, per the plan's accessibility section."""
    if not segments:
        return "You are already at your destination."
    parts = [f"Head {s['length_m']:.0f} m along {s['name']}" if i == 0
             else f"then {s['length_m']:.0f} m along {s['name']}"
             for i, s in enumerate(segments)]
    return ", ".join(parts) + "."
