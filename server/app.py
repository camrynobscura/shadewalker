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
    GET /route?from_lat=..&from_lon=..&to_lat=..&to_lon=..[&tree_weight=..][&month=..]

Every /route response carries BOTH the green route and the plain-shortest
baseline — the frontend's comparison view needs the pair, and computing
the second route costs microseconds.
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException

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


app = FastAPI(title="Shady Stroll", lifespan=lifespan)


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
    tree_weight: float = config.TREE_WEIGHT,  # defaults double as API docs
    month: int | None = None,                 # None → current month (server clock)
) -> dict:
    if month is None:
        month = datetime.now().month
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1-12")

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

    start = store.snap(from_lat, from_lon)
    end = store.snap(to_lat, to_lon)

    green = store.route(start, end, tree_weight=tree_weight, month=month)
    shortest = store.route(start, end, tree_weight=0, month=month)
    if green is None or shortest is None:
        raise HTTPException(status_code=422, detail="No path between these points")

    return {
        "green": _to_feature(green),
        "shortest": _to_feature(shortest),
        "comparison": {
            # Honest-stats inputs for the frontend (plan: absolute numbers
            # alongside percentages, so sparse areas aren't oversold).
            "extra_length_m": round(green["length_m"] - shortest["length_m"], 1),
            "extra_trees": green["tree_count"] - shortest["tree_count"],
            "month": month,
            "tree_weight": tree_weight,
        },
        "description": _describe(green["segments"]),
    }


def _to_feature(route: dict) -> dict:
    """Wrap a GraphStore route as GeoJSON — the lingua franca of web maps;
    Leaflet renders it directly."""
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": route["coords"]},
        "properties": {
            "length_m": route["length_m"],
            "minutes": route["minutes"],
            "tree_count": route["tree_count"],
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
