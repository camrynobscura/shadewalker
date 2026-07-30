# Shadewalker

Find the shadiest walking route between two points in NYC, powered by the live
[NYC Tree Map](https://data.cityofnewyork.us/Environment/Forestry-Tree-Points/hn5i-inap)
and OpenStreetMap.

**Status:** routing trees-only across **all five boroughs** — a working, WCAG
2.2 AA–accessible app over ~489k walk-graph edges, with shade scored from
both the live tree census and the 2021 NYC canopy raster. Not yet deployed;
run it locally as below.

## Layout

```
pipeline/   Python preprocessing: fetch data → build graph → score trees → export tiles
server/     FastAPI + igraph routing server
web/        Vite + React + TypeScript frontend
data/       gitignored: raw API cache + exported tiles (regenerable)
```

## Running it

Requires [uv](https://docs.astral.sh/uv/) (installs its own Python 3.12) and Node.

**1. Build the pilot tile's data** (raw fetches are cached, so reruns are fast):

```bash
uv sync                                  # like `npm install`
uv run python -m pipeline.run_tile pilot # fetch + process the pilot tile
```

`--refresh-trees` re-downloads tree data only (street network stays cached).

Optionally set `SOCRATA_APP_TOKEN` (a free token from
[data.cityofnewyork.us](https://data.cityofnewyork.us)) to raise the NYC
Open Data rate limit above the anonymous default — helpful once fetching
more than a tile or two at a time.

**2. Start the routing server**, which loads that tile into memory:

```bash
uv run uvicorn server.app:app --port 8000
```

**3. Start the frontend**, in a separate terminal:

```bash
cd web
npm install
npm run dev   # proxies /route and /health to localhost:8000
```

Open the printed local URL — click two points on the map (or type addresses)
to see the shadiest route alongside the plain-fastest one.
