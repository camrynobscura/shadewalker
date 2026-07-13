# Shady Stroll

A tree-optimized walking route planner for NYC. Three parts, in order of
data flow:

```
pipeline/   Python: fetch OSM + NYC Tree Map data -> build a walk graph ->
            score edges by tree cover -> export gzipped tile chunks
server/     FastAPI + igraph: loads tile chunks into memory, serves /route
            (Dijkstra over tree-weighted edge costs) and /health
web/        Vite + React + TypeScript + react-leaflet frontend
```

`data/` (raw API cache + exported tiles) is gitignored and fully
regenerable from `pipeline/` — nothing in `server/` or `web/` works until
it's been generated at least once.

## Commands

```bash
uv sync                                     # install Python deps
uv run python -m pipeline.run_tile pilot    # fetch + process the pilot tile
                                             # (--refresh-trees to re-pull tree data only)
uv run uvicorn server.app:app --port 8000   # routing server (loads data/tiles/*.json.gz)

cd web
npm install
npm run dev        # dev server; proxies /route and /health to localhost:8000
npm run build      # tsc -b && vite build
npm run lint        # oxlint
npm run preview     # serves the production build (used for Lighthouse audits)
```

No automated test suite exists yet. Verify changes by actually running the
pipeline/server/frontend and exercising the change; re-run a Lighthouse
accessibility audit (`npx lighthouse <url> --chrome-flags="--headless=new"`
against `npm run preview`, not `npm run dev`) after any non-trivial styling
change.

## Product scope

Core, non-optional target: trees-only routing for **all of NYC** — the
current pilot tile (Carroll Gardens + Gowanus) is a proving ground, not the
final scope. Per-sidewalk edges and building-shadow scoring are optional
stretch goals layered on later; don't assume they're required, and don't
block the trees-only citywide expansion on them.

Tree data **must** come from the live NYC Tree Map / Forestry Tree Points
dataset (Socrata id `hn5i-inap`) — never the static 2015 street tree
census. It's a deliberate, already-settled choice.

## Non-obvious traps

- **Dual CRS, on purpose.** Every geometry exists in two CRSs: EPSG:4326
  (lat/lon degrees) for drawing/exporting, EPSG:32618 (UTM 18N, meters) for
  any buffering/measuring. Mixing them up (e.g. buffering in degrees, or
  exporting projected coordinates) is the #1 geospatial bug class here —
  keep `geometry` vs `geometry_m` explicit rather than reusing one column.
- **NYC's OSM walk network is a trap.** `network_type="walk"` pulls in
  thousands of unnamed sidewalk footways and drops named streets that are
  tagged `"sidewalk:X"="use_sidewalk"` (no drivable-adjacent walk path).
  `pipeline/fetch/streets.py` uses a custom Overpass filter to build a
  centerline graph instead — don't swap back to the default filter.
- **Edge geometry direction isn't reliable from the `(u, v)` tuple.**
  `ox.convert.to_undirected()` can keep an edge's original directed
  geometry even when it doesn't match the `(u, v)` order the edge ends up
  indexed under. `server/graph_store.py`'s route-stitching code decides
  whether to flip a segment by comparing actual coordinates to the current
  node position — not by trusting `u == current` — specifically because
  that assumption once produced backtracking spikes on the drawn route.
  Keep that comparison if you touch this function.

## Code style

- **Python:** plain and explicit — small named functions, no dense
  comprehensions/decorators/metaprogramming where a loop and an `if` will
  do. Constants live in `pipeline/config.py` with units in the name (`_M`
  for meters). Comment the *why* on geospatial steps (reprojection,
  buffering, offset curves) — that's the genuinely non-obvious part.
- **Frontend:** idiomatic modern React + strict TypeScript — function
  components, hooks, typed API responses (`web/src/api.ts` mirrors
  `server/app.py`'s response shape exactly; keep them in sync). **CSS
  Modules with plain modern CSS** — not Tailwind, not Sass. That's a
  deliberate choice, not a placeholder.
- Whoever's coding here is comfortable in Node/Express/PostgreSQL/React
  but newer to Python and TypeScript — prefer clarity over cleverness in
  both.

## Accessibility

WCAG 2.2 AA is a requirement, not polish: labeled non-pointer inputs for
every map interaction, text route descriptions in an `aria-live` region,
native controls with visible focus states, AA color contrast, 44px touch
targets, `prefers-reduced-motion` respected. Lighthouse accessibility /
best-practices / SEO should stay at 100 — treat a drop as a regression to
fix, not a score to shrug off. Performance sits around 82–90 (a lighter
theme's map tiles cost more than a dark one under Lighthouse's simulated
throttling) — that tradeoff has already been made deliberately.

## Design system

Current visual identity is **Greenhouse**: light mint ground, forest-ink
text, jade (`--green`) + neon magenta (`--magenta`) accents, all-monospace
type, zero corner radius, faint green scanlines over the map. Tokens live
in `web/src/index.css`; component-specific rules in each `*.module.css`.
UI copy intentionally uses a terminal voice (`Origin_node`, `Shade_priority`,
`LOCK`, `CLEAR_ROUTE`) — that's the theme, not a typo waiting to be fixed.

## Git

Write commits at natural working checkpoints. **Never add a
`Co-Authored-By` line or any AI-attribution trailer to commit messages.**
