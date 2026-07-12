# Shady Stroll

Find the greenest walking route between two points in NYC, powered by the live
[NYC Tree Map](https://data.cityofnewyork.us/Environment/Forestry-Tree-Points/hn5i-inap)
and OpenStreetMap.

**Status:** Stage 1 (trees-only routing on the Carroll Gardens + Gowanus pilot tile) — in progress.
See the full plan in `~/.claude/plans/jiggly-stirring-milner.md`.

## Layout

```
pipeline/   Python preprocessing: fetch data → build graph → score trees → export tiles
server/     FastAPI + igraph routing server (M3)
web/        Vite + React + TypeScript frontend (M4)
data/       gitignored: raw API cache + exported tiles (regenerable)
```

## Running the pipeline

Requires [uv](https://docs.astral.sh/uv/) (installs its own Python 3.12):

```bash
uv sync                                  # like `npm install`
uv run python -m pipeline.run_tile pilot # fetch + process the pilot tile
```

`--refresh-trees` re-downloads tree data (street network stays cached).
