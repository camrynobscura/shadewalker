import type { RouteFeature } from './api'
import { displayShade } from './shade'

/** The Shade_priority presets and the route math built on them. Own
 * module, not Controls.tsx, since 2026-08-30: exporting non-components
 * from a component file breaks React Fast Refresh (it was the source of
 * the suite's only three lint warnings), and three consumers that never
 * render anything (App's URL parsing, useRouteQuery's fetch ladder, the
 * unit tests) were importing a component file just to reach these. */

/* Four routing intensities, calibrated against real routes: 0 is the plain
 * shortest path (no tree preference — the baseline the other three are
 * measured against), 5 only takes near-free detours, 15 sits mid-plateau
 * where short detours appear, 40 is where the router trades serious
 * distance for trees (+523 m for +115 trees on one Gowanus test walk).
 *
 * `as const` freezes the array into a readonly tuple of literal types —
 * TypeScript then knows each value is exactly 0 | 5 | 15 | 40, not just
 * `number`, and will reject a typo like TREE_PRESETS[0].value = 6. */
export const TREE_PRESETS = [
  { value: 0, label: 'NONE', hint: 'fastest route, no detours for shade' },
  { value: 5, label: 'LOW', hint: 'shadier only when it’s nearly free' },
  { value: 15, label: 'MED', hint: 'short detours for shadier blocks' },
  // "longest detours for the most shade": completes NONE→LOW→MED's
  // detour-size gradient, and — unlike the older "shadiest route, even if
  // it takes longer" — keeps the whole "> mode:" line under the ~52
  // monospace cells that fit one line in the panel (user call 2026-08-28).
  { value: 40, label: 'MAX', hint: 'longest detours for the most shade' },
] as const

// Looked up by label rather than array position — a moderate middle
// ground, not the first or last entry, so it shouldn't depend on where
// MED happens to sit in the list above.
export const DEFAULT_TREE_WEIGHT: number = TREE_PRESETS.find((preset) => preset.label === 'MED')!.value

/** Old bookmarked URLs carry any 0–40 slider value; snap it to the nearest
 * preset. `<=` makes ties go to the later (shadier) option, so the old
 * default of 10 — equidistant from 5 and 15 — lands on Medium. */
export function snapToPreset(weight: number): number {
  let nearest: number = TREE_PRESETS[0].value
  for (const preset of TREE_PRESETS) {
    if (Math.abs(preset.value - weight) <= Math.abs(nearest - weight)) {
      nearest = preset.value
    }
  }
  return nearest
}

export interface RouteComparison {
  extraMinutes: number
  extraShadePct: number
  extraLengthM: number
}

/** How much more shade the selected preset buys, and what it costs in time
 * and distance, relative to the plain-shortest (NONE) baseline. Computed
 * client-side -- /route returns every preset's full properties in one
 * response, so this is a pure subtraction over data the client already has.
 *
 * Deliberately no tree-count delta: the server guarantees shade_fraction is
 * monotonic in Shade_priority (see clamp_shade_monotonic), but tree_count
 * isn't -- a genuinely shadier route can pass fewer individual trees -- so a
 * "-3 trees" beside "+5% shade" would muddy the very thing this line is for.
 * Absolute tree_count still shows in RouteStats. After the clamp, all three
 * deltas here are guaranteed >= 0, which is why the template can hardcode a
 * leading "+".
 *
 * The shade delta subtracts DISPLAYED values (shade.ts), not raw
 * fractions: RouteStats shows curved numbers, and "+5% shade" must equal
 * the difference a user can check between two presets on screen.
 * displayShade is strictly monotone, so the clamp's >= 0 guarantee
 * carries through to the curved delta unchanged. */
export function compareRoutes(selected: RouteFeature, baseline: RouteFeature): RouteComparison {
  return {
    extraMinutes: Math.round(selected.properties.minutes - baseline.properties.minutes),
    extraShadePct: Math.round(
      (displayShade(selected.properties.shade_fraction) - displayShade(baseline.properties.shade_fraction)) * 100,
    ),
    extraLengthM: Math.round((selected.properties.length_m - baseline.properties.length_m) * 10) / 10,
  }
}
