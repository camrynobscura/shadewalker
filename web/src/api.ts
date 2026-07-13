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

/** GeoJSON Feature for one route. Coordinates are [lon, lat] pairs
 * (GeoJSON order) — Leaflet wants [lat, lon], so flip when drawing. */
export interface RouteFeature {
  type: 'Feature'
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
  properties: {
    length_m: number
    minutes: number
    tree_count: number
    segments: RouteSegment[]
  }
}

export interface RouteResponse {
  green: RouteFeature
  shortest: RouteFeature
  /** Where the request actually starts/ends once resolved onto the street
   * network — can differ from what was clicked/geocoded, since that point
   * may sit mid-block. One shared pair (not per-route): the snap itself
   * doesn't depend on tree_weight. */
  snapped: { start: Point; end: Point }
  comparison: {
    extra_length_m: number
    extra_trees: number
    month: number
    tree_weight: number
  }
  description: string
}

/** Distinguishes "the server responded but rejected the request" (has a
 * specific, useful reason worth showing) from a plain failed fetch — a
 * dead server or no network throws a generic browser Error instead. */
export class RouteError extends Error {}

export async function fetchRoute(
  from: Point,
  to: Point,
  treeWeight: number,
  signal: AbortSignal,
): Promise<RouteResponse> {
  const params = new URLSearchParams({
    from_lat: String(from.lat),
    from_lon: String(from.lon),
    to_lat: String(to.lat),
    to_lon: String(to.lon),
    tree_weight: String(treeWeight),
  })
  const res = await fetch(`/route?${params}`, { signal })
  if (!res.ok) {
    const body: { detail?: string } = await res.json().catch(() => ({}))
    throw new RouteError(body.detail ?? `Routing failed (${res.status})`)
  }
  return res.json()
}

/** GeoJSON polygon of the area we actually have street + tree data for —
 * drawn on the map so people can see where a route can start/end before
 * they try one. */
export interface CoverageFeature {
  type: 'Feature'
  geometry: {
    type: 'Polygon'
    coordinates: [number, number][][]
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
