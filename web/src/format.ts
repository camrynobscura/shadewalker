const METERS_PER_FOOT = 0.3048
const FEET_PER_MILE = 5280

/** A point as plain display text ("40.6795, -73.9962") -- shown in an
 * address field the instant a map click sets it, before reverse-geocoding
 * has had a chance to resolve a real address (or if it fails outright). 4
 * decimal places is ~11m at NYC's latitude, plenty for "is this roughly
 * where I clicked." Distinct from App.tsx's own point formatter, which
 * encodes URL query-string state (5 decimals, no space) rather than
 * display text -- different job, not a duplicate. */
export function formatCoords(point: { lat: number; lon: number }): string {
  return `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`
}

/** Value and unit separated, for markup that styles the unit differently —
 * RouteStats' dist box renders its unit in the same lighter <small> the eta
 * box's "min" already uses, which needs them as two elements. */
export function formatDistanceParts(meters: number): { value: string; unit: string } {
  const feet = meters / METERS_PER_FOOT
  return feet >= FEET_PER_MILE * 0.1
    ? { value: (feet / FEET_PER_MILE).toFixed(1), unit: 'mi' }
    : { value: `${Math.round(feet)}`, unit: 'ft' }
}

/** Short routes read better as whole feet, longer ones as miles with one
 * decimal — the convention US map apps use. Shared by Controls (extra-cost
 * line) and RouteStats (route stats + directions). */
export function formatDistance(meters: number): string {
  const { value, unit } = formatDistanceParts(meters)
  return `${value} ${unit}`
}

/** ETA as value/unit pairs, for the same <small>-styled markup as
 * formatDistanceParts. Under an hour it reads "42 min"; from an hour up,
 * "2 hr 19 min", with a zero remainder dropped ("2 hr", never
 * "2 hr 0 min"). These units only fit on one stat-box line because the
 * stat values render in a PROPORTIONAL face (RouteStats.module.css's
 * .statVal) — in the app's monospace they were a third spaces and
 * wrapped; short "2h 19m" units were tried in between (2026-08-28).
 * Rounds the raw minutes ONCE, up front, then splits — so 59.6 rolls
 * over to "1 hr" and can never render as "60 min". */
export function formatEtaParts(minutes: number): Array<{ value: string; unit: string }> {
  const total = Math.round(minutes)
  if (total < 60) return [{ value: `${total}`, unit: 'min' }]
  const hours = Math.floor(total / 60)
  const rest = total % 60
  const parts = [{ value: `${hours}`, unit: 'hr' }]
  if (rest > 0) parts.push({ value: `${rest}`, unit: 'min' })
  return parts
}
