/** Typed client for the routing server + Nominatim geocoding.
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

export interface RouteSegment {
  name: string
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
    segments: RouteSegment[]
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
}

export async function fetchCoverage(): Promise<CoverageFeature> {
  const res = await fetch('/coverage')
  if (!res.ok) throw new Error(`Could not load coverage area (${res.status})`)
  return res.json()
}

export interface GeocodeResult extends Point {
  label: string
}

/** Nominatim = OpenStreetMap's free geocoder. The viewbox + bounded params
 * confine matches to NYC so "Court St" finds Brooklyn, not Buffalo. */
export async function geocode(query: string): Promise<GeocodeResult | null> {
  const params = new URLSearchParams({
    q: query,
    format: 'jsonv2',
    limit: '1',
    viewbox: '-74.26,40.49,-73.68,40.92',
    bounded: '1',
  })
  const res = await fetch(`https://nominatim.openstreetmap.org/search?${params}`)
  if (!res.ok) return null
  const results: { lat: string; lon: string; display_name: string }[] = await res.json()
  if (results.length === 0) return null
  return {
    lat: parseFloat(results[0].lat),
    lon: parseFloat(results[0].lon),
    label: results[0].display_name.split(',').slice(0, 2).join(','),
  }
}

/** The reverse of geocode(): a point the user picked (a map click, a
 * geolocation fix) back to a short human-readable label, so an address
 * field can show real text instead of the point that filled it.
 *
 * Built from the structured `address.house_number`/`address.road` fields,
 * not `display_name` -- Nominatim's reverse lookup happily matches the
 * nearest tagged POI (a restaurant, a brewery, a numbered sports pitch),
 * and `display_name` puts that POI's own name first: reverse-geocoding a
 * point right outside a restaurant returned "Lucali, 575, Henry Street,
 * ..." there, a business name where an address field needs an address.
 * `address.house_number`/`address.road` stay separate from whatever POI
 * tag matched (confirmed against several categories -- amenity, craft,
 * leisure -- each keys its own name under its own category, never under
 * `house_number`/`road`), so reading those two fields directly sidesteps
 * the problem instead of trying to filter business names out after the
 * fact. Falls back to just `road` with no house number (e.g. a path
 * inside a park), or null (→ the caller's own coordinate fallback) if
 * Nominatim has no address-shaped answer at all. */
export async function reverseGeocode(point: Point): Promise<string | null> {
  const params = new URLSearchParams({
    lat: String(point.lat),
    lon: String(point.lon),
    format: 'jsonv2',
  })
  const res = await fetch(`https://nominatim.openstreetmap.org/reverse?${params}`)
  if (!res.ok) return null
  const result: { address?: { house_number?: string; road?: string } } = await res.json()
  const road = result.address?.road
  if (!road) return null
  return result.address?.house_number ? `${result.address.house_number}, ${road}` : road
}
