# Shady Stroll

Find the shadiest walking route between two points in NYC, powered by the live
[NYC Tree Map](https://data.cityofnewyork.us/Environment/Forestry-Tree-Points/hn5i-inap)
and OpenStreetMap.

**Status:** Stage 1 is done — a working, WCAG 2.2 AA–accessible app routing
trees-only on the Carroll Gardens + Gowanus pilot tile. Stage 2 (expand the
same trees-only routing citywide) hasn't started yet. See the full plan in
`~/.claude/plans/jiggly-stirring-milner.md`.

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
