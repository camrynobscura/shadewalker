const METERS_PER_FOOT = 0.3048
const FEET_PER_MILE = 5280

/** Short routes read better as whole feet, longer ones as miles with one
 * decimal — the convention US map apps use. Shared by Controls (extra-cost
 * line) and RouteStats (route stats + directions). */
export function formatDistance(meters: number): string {
  const feet = meters / METERS_PER_FOOT
  return feet >= FEET_PER_MILE * 0.1
    ? `${(feet / FEET_PER_MILE).toFixed(1)} mi`
    : `${Math.round(feet)} ft`
}
