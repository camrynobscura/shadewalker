const METERS_PER_FOOT = 0.3048
const FEET_PER_MILE = 5280

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
