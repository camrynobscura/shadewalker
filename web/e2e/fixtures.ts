import type { Page } from '@playwright/test'

/** Builds the query string App.tsx's URL-state effect reads on load
 * (`from`/`to`/`w`) — drives the app straight to a given state without
 * simulating pointer clicks on the Leaflet map, which is brittle in a
 * headless browser. */
export function routeUrl(from: string, to: string, w = 15): string {
  return `/?from=${from}&to=${to}&w=${w}`
}

// Two real pilot-tile points a few blocks apart in Carroll Gardens —
// close enough that every tree_weight preset resolves quickly.
export const POINT_A = '40.6800,-73.9980'
export const POINT_B = '40.6720,-73.9880'

// Well outside the pilot tile's coverage polygon — triggers the server's
// 422 rejection and RouteStats's error branch.
export const POINT_OUTSIDE_COVERAGE = '40.7580,-73.9855'

const GEOCODE_RESULTS: Record<string, { lat: string; lon: string }> = {
  '250 Court St': { lat: '40.6800', lon: '-73.9980' },
  '3rd St & 3rd Ave': { lat: '40.6720', lon: '-73.9880' },
}

// api.ts's reverseGeocode() sends `String(point.lat)` -- e.g. `40.68`, not
// `40.6800` -- so the lookup key has to go through the same Number()
// round-trip POINT_A/POINT_B/POINT_OUTSIDE_COVERAGE's own "lat,lon" text
// would, rather than matching those literals verbatim.
function reverseKey(pointStr: string): string {
  const [lat, lon] = pointStr.split(',').map(Number)
  return `${lat},${lon}`
}

const REVERSE_GEOCODE_RESULTS: Record<string, { house_number?: string; road: string }> = {
  [reverseKey(POINT_A)]: { house_number: '250', road: 'Court St' },
  [reverseKey(POINT_B)]: { road: '3rd Ave' },
  [reverseKey(POINT_OUTSIDE_COVERAGE)]: { road: 'Somewhere Ave' },
}

/** Intercepts the real Nominatim service so tests never make live network
 * requests against a free, keyless, policy-sensitive public API — the
 * project's own notes already flag that Nominatim's usage policy forbids
 * high-frequency automated querying, and an automated test suite calling
 * it on every run is exactly the pattern to avoid. Covers both directions
 * Nominatim is called for -- forward search (typed address text -> a
 * point, keyed by the `q` param) and reverse (a point that landed in
 * start/end from anywhere -- a URL param via routeUrl(), a map click, a
 * geolocation fix -- back to address text, keyed by `lat`/`lon`). Every
 * spec driving the app via routeUrl() needs this called too, not just the
 * ones that type into an address field -- Controls.tsx's useAddressField
 * reverse-geocodes whatever's in start/end regardless of how it got
 * there, so a bare `page.goto(routeUrl(...))` without this active would
 * quietly hit the real service instead of failing loudly. Unrecognized
 * queries/points resolve to "no match" -- AddressField's NOT_FOUND state
 * for a forward miss, api.ts's own coordinate fallback for a reverse one. */
export async function mockGeocode(page: Page): Promise<void> {
  await page.route('https://nominatim.openstreetmap.org/**', (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/reverse') {
      const key = `${url.searchParams.get('lat')},${url.searchParams.get('lon')}`
      const hit = REVERSE_GEOCODE_RESULTS[key]
      const body = hit ? { address: hit } : {}
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    }
    const query = url.searchParams.get('q') ?? ''
    const hit = GEOCODE_RESULTS[query]
    const body = hit ? [{ ...hit, display_name: `${query}, Brooklyn, NY` }] : []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
