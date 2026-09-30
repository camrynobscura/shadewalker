/** Which shade the routes are scored by: the shade pill's pick (#121)
 * and /route's `layers`. All shade
 * -- trees and buildings -- is the default; the other two are for
 * curiosity, comparing paths by where their shade comes from, and
 * choosing tree shade on purpose. The route rows then count only the
 * picked kind; after dark every kind is full shade. */
export type ShadeLayers = 'both' | 'trees' | 'buildings'

/** The pill's words and the menu's lines, the same: only the words change
 * when it's set, like the time pill's (no colour, no bold). */
export const SHADE_CHOICES = [
  { value: 'both', label: 'All shade' },
  { value: 'trees', label: 'Tree shade' },
  { value: 'buildings', label: 'Building shade' },
] as const satisfies readonly { value: ShadeLayers; label: string }[]

/** "Tree shade" -- the pill's words for a pick. */
export function describeShadeLayers(layers: ShadeLayers): string {
  return SHADE_CHOICES.find((choice) => choice.value === layers)!.label
}

/** A link's `layers` back to a pick. Only a set one rides the link, so
 * anything else -- absent, malformed, "both" -- is all shade. */
export function shadeLayersFromLink(params: URLSearchParams): ShadeLayers {
  const value = params.get('layers')
  return value === 'trees' || value === 'buildings' ? value : 'both'
}
