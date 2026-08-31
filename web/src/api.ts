/** Typed client for the routing server, geocoding included.
 *
 * These interfaces mirror server/app.py's response shape exactly. That makes
 * this file the single source of truth for "what the API returns" — if the
 * server changes shape, updating these types makes every affected component
 * a compile error instead of a runtime surprise.
 */

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
  action:
    | 'depart'
    | 'continue'
    | 'left'
    | 'right'
    | 'sharp_left'
    | 'sharp_right'
    | 'cross_side'
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
    /** Fraction (0-1) of the route classified as shaded -- tree canopy
     * only today, but a general "shade" field so building-shadow scoring
     * (an optional future stretch goal) can feed the same one later.
     * Continuous since 2026-08-17: each block contributes
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
  month: number
  description: string
}

/** Distinguishes "the server responded but rejected the request" (has a
 * specific, useful reason worth showing) from a plain failed fetch — a
 * dead server or no network throws a generic browser Error instead. */
export class RouteError extends Error {}

export async function fetchRoute(
  from: Point,
  to: Point,
  treeWeights: number[],
  signal: AbortSignal,
): Promise<RouteResponse> {
  const params = new URLSearchParams({
    from_lat: String(from.lat),
    from_lon: String(from.lon),
    to_lat: String(to.lat),
    to_lon: String(to.lon),
  })
  for (const weight of treeWeights) params.append('tree_weights', String(weight))
  const res = await fetch(`/route?${params}`, { signal })
  if (!res.ok) {
    const body: { detail?: string } = await res.json().catch(() => ({}))
    throw new RouteError(body.detail ?? `Routing failed (${res.status})`)
  }
  return res.json()
}

/** GeoJSON multipolygon of the area(s) we actually have street + tree data
 * for — drawn on the map so people can see where a route can start/end
 * before they try one. MultiPolygon, not Polygon: disjoint routable areas
 * (mainland NYC, Governors Island, eventually Staten Island) each get
 * their own piece rather than being merged or dropped. */
export interface CoverageFeature {
  type: 'Feature'
  geometry: {
    type: 'MultiPolygon'
    coordinates: [number, number][][][]
  }
  /** The offshore frame (FIXES 12): what MapView actually draws — one
   * generous dashed boundary through the water + a two-step feathered
   * dim — while `geometry` above remains the true click-acceptance
   * region the server checks. Rings are closed [lon, lat] lists. */
  properties: {
    frame: [number, number][][]
    frame_feather_350: [number, number][][]
    frame_feather_800: [number, number][][]
  }
}

export async function fetchCoverage(): Promise<CoverageFeature> {
  const res = await fetch('/coverage')
  if (!res.ok) throw new Error(`Could not load coverage area (${res.status})`)
  return res.json()
}

export interface GeocodeResult extends Point {
  label: string
}

/** Forward geocoding through our own server (`/geocode`, a proxy in
 * front of Photon — server/geocode.py carries the whole why). Same
 * relative-URL pattern as /route: no CORS, no third-party call from the
 * visitor's browser, and the NYC bounding + label building live
 * server-side, so this stays a thin fetch. limit=1: an address field's
 * submit resolves to its single best match. */
export async function geocode(query: string): Promise<GeocodeResult | null> {
  const params = new URLSearchParams({ q: query, limit: '1' })
  const res = await fetch(`/geocode?${params}`)
  if (!res.ok) return null
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
  const res = await fetch(`/geocode/reverse?${params}`)
  if (!res.ok) return null
  const body: { label: string | null } = await res.json()
  return body.label
}
