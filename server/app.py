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
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query

from pipeline import config
from server.graph_store import GraphStore

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
    """The loaded data's extent as a GeoJSON polygon, so the frontend can
    draw it on the map — pilot tile today, whatever's loaded once Stage 2
    adds more tiles, with no server code change needed either time."""
    lon_min, lat_min, lon_max, lat_max = store.coverage_bounds()
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon_min, lat_min],
                [lon_max, lat_min],
                [lon_max, lat_max],
                [lon_min, lat_max],
                [lon_min, lat_min],
            ]],
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
            detail="Start point is outside our current coverage area — pick a point inside the shaded area on the map.",
        )
    if not store.in_coverage(to_lat, to_lon):
        raise HTTPException(
            status_code=422,
            detail="End point is outside our current coverage area — pick a point inside the shaded area on the map.",
        )

    start = store.snap_to_edge(from_lat, from_lon)
    end = store.snap_to_edge(to_lat, to_lon)

    routes = []
    for tree_weight in tree_weights:
        result = store.route(start, end, tree_weight=tree_weight, month=month)
        if result is None:
            raise HTTPException(status_code=422, detail="No path between these points")
        routes.append(_to_feature(result, tree_weight))

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
