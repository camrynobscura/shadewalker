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

const GEOCODE_RESULTS: Record<string, { lat: number; lon: number }> = {
  '250 Court St': { lat: 40.68, lon: -73.998 },
  '3rd St & 3rd Ave': { lat: 40.672, lon: -73.988 },
}

// api.ts's reverseGeocode() sends `String(point.lat)` -- e.g. `40.68`, not
// `40.6800` -- so the lookup key has to go through the same Number()
// round-trip POINT_A/POINT_B/POINT_OUTSIDE_COVERAGE's own "lat,lon" text
// would, rather than matching those literals verbatim.
function reverseKey(pointStr: string): string {
  const [lat, lon] = pointStr.split(',').map(Number)
  return `${lat},${lon}`
}

const REVERSE_GEOCODE_RESULTS: Record<string, string> = {
  [reverseKey(POINT_A)]: '250 Court St',
  [reverseKey(POINT_B)]: '3rd Ave',
  [reverseKey(POINT_OUTSIDE_COVERAGE)]: 'Somewhere Ave',
}

/** Intercepts OUR OWN /geocode proxy (in the browser, before any request
 * leaves the page) so tests never reach the real backend — which since
 * 2026-08-30 would itself call the public Photon instance upstream, a
 * free fair-use service an automated suite must not hammer. Same
 * hermeticity rule as the Nominatim days, one hop earlier. Covers both
 * directions -- forward search (typed address text -> a point, keyed by
 * the `q` param) and reverse (a point that landed in start/end from
 * anywhere -- a URL param via routeUrl(), a map click, a geolocation fix
 * -- back to address text, keyed by `lat`/`lon`). Every spec driving the
 * app via routeUrl() needs this called too, not just the ones that type
 * into an address field -- Controls.tsx's useAddressField
 * reverse-geocodes whatever's in start/end regardless of how it got
 * there, so a bare `page.goto(routeUrl(...))` without this active would
 * quietly hit the live upstream instead of failing loudly. Unrecognized
 * queries/points resolve to "no match" -- AddressField's NOT_FOUND state
 * for a forward miss, api.ts's own coordinate fallback for a reverse one.
 * Response bodies mirror server/app.py's shapes exactly:
 * {results: [{lat, lon, label}]} and {label: string | null}. */
export async function mockGeocode(page: Page): Promise<void> {
  await page.route('**/geocode/reverse?*', (route) => {
    const url = new URL(route.request().url())
    const key = `${url.searchParams.get('lat')},${url.searchParams.get('lon')}`
    const body = { label: REVERSE_GEOCODE_RESULTS[key] ?? null }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route('**/geocode?*', (route) => {
    const url = new URL(route.request().url())
    const query = url.searchParams.get('q') ?? ''
    const hit = GEOCODE_RESULTS[query]
    const body = { results: hit ? [{ ...hit, label: `${query}, Brooklyn` }] : [] }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
