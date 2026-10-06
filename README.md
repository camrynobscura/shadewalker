# Shade Walker

**Shaded walking routes in New York City.** Pick two points and how much shade matters to you, and Shade Walker will give you the shadiest route it can find at that exact moment in time. Shade comes from the tree canopy along the sidewalk and from the shadows cast by buildings, computed from each building's roof height and the sun's angle at the current or chosen time, so the same street scores differently at 9am vs 4pm. You can also set a departure or arrival time in the future, and pick from four shade priority levels that show you the distance and walking time trade-offs.

![The Shade Walker app on a route from the Metropolitan Museum of Art to Pier 57: the shaded route (solid green) splits from the fastest (dashed pink) down Manhattan's west side, with the four Shade_priority rows beside the map — the selected MED row reads 1 hr 19 min, 4.1 mi, 81% shaded.](docs/screenshot.png)

Built on four public datasets: the Forestry Tree Points (tree data) and Building Footprints (building height) datasets from NYC Open Data, a 6-inch 2021 land-cover raster (for park canopy) from The Nature Conservancy, and the pedestrian routing data from OpenStreetMap.

## By the numbers

- 488,677 routable sidewalk and path edges across all five boroughs
- ~900,000 city tree records, scored onto the side of the street they actually shade
- One 54 MB export; the whole city routes from ~0.6 GB of RAM (measured on the server, 2026-09-26)
- Every request computes all four shade presets; typical full response ~400 ms
- 445 backend tests, 127 unit, 121 end-to-end in Chromium and WebKit, CI on every PR
- Lighthouse 100/100/100 (accessibility / best practices / SEO); zero axe violations, scanned per app state on every PR

## How it works

```
pipeline/   Python · OSM extract + Forestry Tree Points + land-cover raster
            + building footprints
            → per-sidewalk pedestrian graph, every edge scored for tree
              shade, then shadow-tested against nearby buildings for
              every daylight hour of every month (a 12 × 24 sun table)
            → one citywide export (~54 MB)
server/     FastAPI + igraph · loads the export into memory, serves
            /route (Dijkstra over shade-weighted costs: trees and
            building shadows for the requested time, combined by union),
            /geocode and /geocode/reverse (proxies to Photon, with NYC
            GeoSearch behind it as a backup), /health —
            and the built frontend
web/        Vite + React + TypeScript + react-leaflet
```

The model choices that matter:

- **Per-sidewalk edges, not street centerlines.** Each side of a street is its own routable edge with its own shade score — 37.8% of NYC streets have ≥70% of their canopy on one side, so a single score per street would erase exactly the distinction this app exists for.
- **Coverage comes from OpenStreetMap.** Anything OSM maps as a pedestrian way is routable.
- **Trees attach to the side they shade.** Each recorded tree is assigned to the block face its canopy actually reaches (96.7% of points attach to a clear side). Park paths get area credit from the land-cover raster where individual records don't exist.
- **Leaf cover changes by month.** Leaf-on/leaf-off varies by month; the same street scores shadier in July than April, and `/route` takes a month, day, hour and minute (defaulting to New York's now).
- **Building shadows by time.** Every sidewalk edge is sampled every 5 m in three lanes, and each sample is tested against the shadows of the buildings within 1 km for every daylight hour of every month. The server blends the nearest hours and months for the requested minute and combines the result with tree shade by union, so adding buildings can only ever add shade to an edge, never remove it. Hours when the sun is down count as full shade: the last hour before dark climbs toward 100%, and once it's fully dark `/route` gives every preset the same shortest route and says `night: true`.
- **One request, four routes.** Every call computes the full Shade_priority ladder (weights 0/5/15/40), so switching presets client-side is instant.
- **Departure or arrival time.** A request carries the time of the walk, and every route is scored for that time. If the time is an arrival instead, the server takes the fastest route's duration, subtracts it from the arrival time to get a leave time, and scores all four routes for that leave time so they can be compared.
- **Tree shade or building shade alone.** `layers=trees` or `layers=buildings` scores one kind of shade and ignores the other. After dark, either kind still counts as full shade.

## Running it

```bash
# Python side (needs uv, which installs its own Python)
uv sync
uv run python -m pipeline.build          # full pipeline → data/export (~5 h: ~17 min for
                                         # graph and trees, ~4.5 h for building shadows;
                                         # fetches OSM extract, trees, raster, buildings on
                                         # first run)
uv run uvicorn server.app:app --port 8000

# Frontend
cd web
npm install
npm run dev                              # dev server, proxies API to :8000
npm run build                            # production build; the FastAPI server
                                         # serves web/dist itself when present
```

Open the printed local URL and click two points on the map (or type two addresses) to see the shadiest route alongside the plain-fastest one. Optionally set `SOCRATA_APP_TOKEN` (a free token from [data.cityofnewyork.us](https://data.cityofnewyork.us)) to raise the NYC Open Data rate limit during the first fetch.

Production runs as **one process**: `uvicorn server.app:app --no-access-log --no-server-header` serves API and frontend together (the flags keep visitor IPs and searched locations out of logs — query strings here are location data).

## Tests

Four tiers, all in CI except the data-dependent one:

- `uv run pytest` — pipeline + server (445 tests), including route-regression goldens and a latency canary that run only where the real citywide export exists (they skip cleanly elsewhere)
- `npm run test:unit` — Vitest for pure logic and hooks
- `npx playwright test` — end-to-end against a committed pilot fixture: smoke, keyboard navigation, axe accessibility scans per app state, the shade-monotonicity guarantee, the time and shade pills in both engines, routing by itself on desktop and phone, night routing, and every backend failure the panel has to explain
- `npx tsc -b` / `npm run lint` — tsc clean; lint prints nothing (oxlint with the React Compiler rules on)

## Accessibility

Targets WCAG 2.1/2.2 AA. Last audited 2026-08-30: axe-core across five app states in Chromium, Firefox and WebKit (zero violations), full keyboard operation including the address autocomplete (ARIA combobox), 320px reflow, `prefers-reduced-motion`, and forced-colors. Since then axe runs in CI on every PR, per app state. Automated checks are supplemented by manual VoiceOver passes. Known limitation: map interaction is pointer-first; every routing function is equally available through the labeled address fields.

## Limitations

- Building shadows are computed from a flat-ground model: recorded roof heights, no terrain, no elevated tracks or bridges, no scaffolding or sidewalk sheds, and heights that can lag redevelopment.
- OSM's volunteer-built sidewalk coverage is uneven; routing is thinnest in parts of the Bronx and Staten Island (~70% routable area vs ~83% elsewhere). Mapping a missing sidewalk on OSM fixes the app within a month (data refreshes monthly).
- The land-cover raster is from 2021, so park canopy that uses that raster's data could be somewhat inaccurate.

## Data & attribution

Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL). Tree data: [NYC Open Data](https://opendata.cityofnewyork.us/) / NYC Parks Forestry Tree Points. Building footprints: NYC Open Data / Office of Technology and Innovation. Park and path canopy: The Nature Conservancy. 2024. [New York City Land Cover (2021), Tree Canopy Change (2017-2021), and Estimated Tree Location and Crown Data (2021)](https://doi.org/10.5281/zenodo.14053441). Developed under contract by the University of Vermont Spatial Analysis Laboratory. Used under [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/), as-is and without warranty; we sample it along each path to score its shade. Basemap tiles by [CARTO](https://carto.com/attributions). Geocoding by [Photon](https://photon.komoot.io/) (komoot), with [NYC GeoSearch](https://geosearch.planninglabs.nyc/) (NYC Planning) as the backup when Photon is unavailable. The `data/` directory is fully regenerable from these sources and never committed.

## License

Copyright (c) 2026 camrynobscura. [AGPL-3.0](LICENSE): use it, learn from it, build on it — but anything built on it, including a hosted version, must share its source under the same terms. The data sources above carry their own licenses.
