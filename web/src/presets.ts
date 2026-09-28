/** The Shade_priority presets and the URL snapping built on them. Own
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
/* `spoken` is what assistive tech announces; `label` is what the route
 * row shows. They differ only for MED (VoiceOver pass 2026-08-31): on
 * screen, NONE/LOW/MED/MAX reads as an obvious scale; aloud, with no
 * visual context, "med" is cryptic -- so the row's spoken name starts
 * "Medium". Compliant with label-in-name: "med" is the start of "medium". */
export const TREE_PRESETS = [
  { value: 0, label: 'NONE', spoken: 'None', hint: 'fastest route, no detours for shade' },
  { value: 5, label: 'LOW', spoken: 'Low', hint: 'shadier only when it’s nearly free' },
  { value: 15, label: 'MED', spoken: 'Medium', hint: 'short detours for shadier blocks' },
  // "longest detours for most shade": completes NONE→LOW→MED's
  // detour-size gradient. No "the" — with it, the hint wrapped to a
  // second line by exactly one word while the other three hints fit one
  // line (user call 2026-09-02; the wording itself replaced the older
  // "shadiest route, even if it takes longer" for the same
  // fit-on-one-line reason, 2026-08-28).
  { value: 40, label: 'MAX', spoken: 'Maximum', hint: 'longest detours for most shade' },
] as const

// MAX, the shadiest: it's what the app promises (user, 2026-09-28; MED
// was the default since the presets began). Measured over 180 walks it
// costs under a minute more than MED on a typical walk, 5+ minutes on
// ~7% (data/audits/2026-09-28/default-preset/), and the rows show every
// route's cost, so a long one is a tap from MED or NONE. Looked up by
// label, not array position.
export const DEFAULT_TREE_WEIGHT: number = TREE_PRESETS.find((preset) => preset.label === 'MAX')!.value

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
