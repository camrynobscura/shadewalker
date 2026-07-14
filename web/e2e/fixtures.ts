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

/** Intercepts the real Nominatim service so tests never make live network
 * requests against a free, keyless, policy-sensitive public API — the
 * project's own notes already flag that Nominatim's usage policy forbids
 * high-frequency automated querying, and an automated test suite calling
 * it on every run is exactly the pattern to avoid. Returns a canned result
 * keyed by the exact query text a test types in; unrecognized queries
 * resolve to "no match", driving AddressField's NOT_FOUND state. */
export async function mockGeocode(page: Page): Promise<void> {
  await page.route('https://nominatim.openstreetmap.org/**', (route) => {
    const query = new URL(route.request().url()).searchParams.get('q') ?? ''
    const hit = GEOCODE_RESULTS[query]
    const body = hit ? [{ ...hit, display_name: `${query}, Brooklyn, NY` }] : []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
