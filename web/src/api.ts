/** Typed client for the routing server, geocoding included.
 *
 * These interfaces mirror server/app.py's response shape exactly. That makes
 * this file the single source of truth for "what the API returns" — if the
 * server changes shape, updating these types makes every affected component
 * a compile error instead of a runtime surprise.
 */

import type { WalkTime } from './walkTime'

export interface Point {
  lat: number
  lon: number
}

/** One turn-by-turn step. The server folds crossings and nameless scraps
 * into the street runs they interrupt (evidence-based, no length
 * thresholds -- server/graph_store.py's build_steps), so each step is a
 * stretch a walker experiences as one instruction. */
export interface RouteStep {
  /** What to do at the start of this stretch. */
  action: 'depart' | 'continue' | 'left' | 'right' | 'sharp_left' | 'sharp_right' | 'cross_side'
  /** Street name, or "unnamed path". */
  name: string
  /** Which side of the street this stretch walks — 'north'/'south'/
   * 'east'/'west', or '' when no plain word is honest (diagonal streets,
   * curves, kerbless paths). Always set on 'cross_side' steps. */
  side: string
  /** 8-way compass word on 'depart' steps, '' otherwise. */
  heading: string
  length_m: number
}

/** GeoJSON Feature for one route at one tree_weight. Coordinates are
 * [lon, lat] pairs (GeoJSON order) — Leaflet wants [lat, lon], so flip
 * when drawing. */
export interface RouteFeature {
  type: 'Feature'
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
  properties: {
    /** Which Shade_priority weight this particular route was computed
     * for -- /route returns one Feature per requested weight, so this is
     * what tells them apart. */
    tree_weight: number
    length_m: number
    minutes: number
    tree_count: number
    /** Fraction (0-1) of the route that is shaded: tree canopy and, since
     * the building-shadows work, building shadows for the moment the
     * server computed it (see `hour`/`minute` on the response), combined
     * by union. Continuous since 2026-08-17: each block contributes
     * min(density / saturation, 1) of its length, no per-edge cliff. */
    shade_fraction: number
    /** Fraction (0-1) of the route's tree score that is park-canopy AREA
     * credit rather than countable trees -- when it dominates, the raw
     * tree_count undersells the real cover (a Central Park loop can be
     * "83% shaded, 3 trees"), so RouteStats hides the count. */
    park_canopy_share: number
    segments: RouteStep[]
  }
}

export interface RouteResponse {
  /** One Feature per requested tree_weight, in the same order they were
   * requested in -- /route computes every Shade_priority preset in one
   * call so switching between them client-side never needs a re-fetch. */
  routes: RouteFeature[]
  /** Where the request actually starts/ends once resolved onto the street
   * network — can differ from what was clicked/geocoded, since that point
   * may sit mid-block. One shared pair (not per-route): the snap itself
   * doesn't depend on tree_weight. */
  snapped: { start: Point; end: Point }
  /** The moment the shade was computed for, New York clock: the set
   * departure time, or the server's "now" when none was (or
   * the anchor day, the 15th, when a request pins only the month). */
  month: number
  day: number
  hour: number
  minute: number
  /** Which shade the routing cost saw: "trees" | "buildings" | "both".
   * Always "both" from this frontend; the switch exists server-side for a
   * possible layer selector. */
  layers: 'trees' | 'buildings' | 'both'
  /** True when the whole moment is dark (server/graph_store.py's
   * is_night): with no sun every street is shade, so every route here is
   * the same fastest route at 100%. The Shade_priority box says so in one
   * line instead of comparing four identical presets. */
  night: boolean
  description: string
}

/** Distinguishes "the server responded but rejected the request" (has a
 * specific, useful reason worth showing) from a plain failed fetch — a
 * dead server or no network throws a generic browser Error instead. */
export class RouteError extends Error {}

/** The one error shape our server speaks: FastAPI's `{"detail": "..."}`
 * (the 429 handler in server/app.py matches it on purpose). Anything
 * else in an error body — Caddy's or Vite's proxy page when uvicorn is
 * down — is not a message for the user. */
async function errorDetail(res: Response): Promise<string | null> {
  const body: unknown = await res.json().catch(() => null)
  if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string')
    return body.detail
  return null
}

/** `time` null = now: no time goes out, and the server uses New York's
 * clock. A picked time sends all four parts -- never the year, which the
 * server's shade table doesn't have (see WalkTime). */
export async function fetchRoute(
  from: Point,
  to: Point,
  treeWeights: number[],
  signal: AbortSignal,
  time: WalkTime | null = null,
): Promise<RouteResponse> {
  const params = new URLSearchParams({
    from_lat: String(from.lat),
    from_lon: String(from.lon),
    to_lat: String(to.lat),
    to_lon: String(to.lon),
  })
  for (const weight of treeWeights) params.append('tree_weights', String(weight))
  if (time) {
    params.set('month', String(time.month))
    params.set('day', String(time.day))
    params.set('hour', String(time.hour))
    params.set('minute', String(time.minute))
  }
  const res = await fetch(`/route?${params}`, { signal })
  if (!res.ok) {
    const detail = await errorDetail(res)
    // A 4xx with our detail is the server explaining itself (outside
    // coverage, no path, rate limited). A 5xx, or a body that isn't ours,
    // is the server being broken -- a plain Error, so the caller shows
    // its generic wording rather than "Routing failed (502)" (2026-09-09).
    if (res.status < 500 && detail) throw new RouteError(detail)
    // slowapi's stock 429 body has no detail; ours does, but keep the
    // wait message even if that handler ever goes missing.
    if (res.status === 429) throw new RouteError('too many routes at once — wait a moment and try again')
    throw new Error(`route request failed (${res.status})`)
  }
  return res.json()
}

/** The address search itself couldn't answer — the proxy or Photon is
 * down, or there's no network — as opposed to answering "no match".
 * Thrown so a field can show SEARCH_DOWN instead of NOT_FOUND, which
 * used to send people retyping an address that was fine (2026-09-09). */
export class GeocodeUnavailableError extends Error {}

export interface GeocodeResult extends Point {
  label: string
}

/** Forward geocoding through our own server (`/geocode`, a proxy in
 * front of Photon — server/geocode.py carries the whole why). Same
 * relative-URL pattern as /route: no CORS, no third-party call from the
 * visitor's browser, and the NYC bounding + label building live
 * server-side, so this stays a thin fetch. limit=1: an address field's
 * submit resolves to its single best match. Null means "no match";
 * a failed request (any non-OK status, or fetch itself throwing with the
 * network down) is GeocodeUnavailableError, never null. */
export async function geocode(query: string): Promise<GeocodeResult | null> {
  const params = new URLSearchParams({ q: query, limit: '1' })
  let res: Response
  try {
    res = await fetch(`/geocode?${params}`)
  } catch (err) {
    throw new GeocodeUnavailableError(err instanceof Error ? err.message : 'network failure')
  }
  if (!res.ok) throw new GeocodeUnavailableError(`geocode answered ${res.status}`)
  const body: { results: GeocodeResult[] } = await res.json()
  return body.results[0] ?? null
}

/** Autocomplete suggestions: the same /geocode proxy, more results.
 * Separate from geocode() because the failure semantics differ — a
 * failed suggestion lookup means "no dropdown" (silently empty), while a
 * failed submit-resolve means NOT_FOUND. The AbortSignal is the caller's
 * rate control: every newer keystroke cancels the in-flight lookup. */
export async function suggest(query: string, signal: AbortSignal): Promise<GeocodeResult[]> {
  const params = new URLSearchParams({ q: query, limit: '5' })
  const res = await fetch(`/geocode?${params}`, { signal })
  if (!res.ok) return []
  const body: { results: GeocodeResult[] } = await res.json()
  return body.results
}

/** The reverse of geocode(): a point the user picked (a map click, a
 * geolocation fix) back to a short address label, so an address field
 * can show real text instead of the point that filled it. Null when
 * nothing address-shaped is nearby OR the lookup failed — either way
 * the caller keeps its own coordinate fallback. The address-not-POI
 * rule (an address field must never read "Lucali") moved server-side
 * with the proxy: server/geocode.py's _reverse_label. */
export async function reverseGeocode(point: Point): Promise<string | null> {
  const params = new URLSearchParams({ lat: String(point.lat), lon: String(point.lon) })
  let res: Response
  try {
    res = await fetch(`/geocode/reverse?${params}`)
  } catch {
    return null // no network: the caller's coordinate fallback is the honest label
  }
  if (!res.ok) return null
  const body: { label: string | null } = await res.json()
  return body.label
}
