/** The display shade curve: measured canopy coverage -> estimated
 * experienced shade. Display-only -- routing, clamp_shade_monotonic and
 * the API's shade_fraction are untouched; this maps the number at the
 * last moment before a person reads it.
 *
 * Why: plan-view coverage understates what a walk feels like. The
 * measured mechanism is lane choice -- a walker drifts to the shadiest
 * line the pavement offers, so experienced shade tracks the best lane,
 * while shade_fraction is calibrated to the strip average. Derived
 * 2026-08-27 by tools/audit/derive_display_curve_k.py: over 4,000
 * seeded-random sidewalk edges (425.5 km), best 1m walking lane vs the
 * production 2m-strip average gives k = 1.19-1.23, two estimators
 * agreeing within 0.03, and the implied k is near-constant across
 * coverage deciles, which validates the form, not just the value.
 *
 * Why the floor and not more: perceptual calibration points at 1.2-1.4,
 * but judgment on real streets is contaminated by building shade, which
 * has its own layer -- an exponent tuned to street-level feel would
 * double-count it. The same estimator on building samples gives k
 * 1.13-1.87 at July noon (hard-edged wall bands reward lane choice) and
 * 1.04-1.64 at other slots; 1.2 stays because this is display-only and a
 * per-layer exponent would need per-layer fractions in the response.
 * Wide park paths are knowingly under-served (lane choice grows with
 * width: path-kind measures k 1.46-1.72), and inflating the whole city
 * to chase them isn't worth it.
 *
 * The map is strictly monotone with fixed points at 0 and 1: a bare
 * route stays bare, full coverage stays full, and a shadier route
 * always displays higher than a less shady one -- which is why the
 * Shade_priority monotonicity guarantee survives untouched. Anything
 * that compares displayed values (the stat, the LOW_SHADE warning
 * text) must go through displayShade; anything that reasons about
 * measured coverage (thresholds derived on that scale) stays raw. */
export const DISPLAY_SHADE_EXPONENT = 1.2

/** Measured coverage fraction (0-1) -> displayed shade fraction (0-1). */
export function displayShade(fraction: number): number {
  return 1 - (1 - fraction) ** DISPLAY_SHADE_EXPONENT
}

/** Below this shade_fraction, the route is objectively exposed — say so
 * instead of overselling. Reads the same continuous stat displayed as
 * "% shaded" right above it, so the warning and the number can never
 * disagree.
 *
 * Derived 2026-08-26 on the coverage scale over 188 routable random
 * pairs: share of routes warned is July MED 2.7% / NONE 8.5%, April MED
 * 12.8% / NONE 35.1%. That is the editorial judgment about when exposure
 * deserves saying, and on this scale the words mean exactly what they
 * say: below 15% covered, 85% of the walk is in open sun. The one high
 * figure is April NONE ("fastest route, no detours for shade"), where the
 * user has already said shade is not a priority. Seasonal variation is
 * deliberate, not drift: April really is less shaded than July
 * (CANOPY_BY_MONTH), so the same bar firing more in spring is the honest
 * geography.
 *
 * The display curve does not move this bar: the comparison stays on the
 * raw measured fraction, and because displayShade is strictly monotone,
 * exactly the same routes fire. Only the printed numbers go through the
 * curve (both of them, stat and warning, so they can never disagree); at
 * this bar the warning shows itself at ~18% displayed rather than 15%
 * measured, and its words stay true either way. */
export const LOW_SHADE_FRACTION = 0.15

/** When at least this share of the route's tree score is park-canopy area
 * credit (not countable trees), hide the tree count -- the "> N trees
 * along the way" line under the route rows -- because the count can't
 * see area credit, so it undersells exactly the routes with the most real
 * cover ("83% shaded, 3 trees").
 *
 * 0.25: the count is flavor, so it should be accurate or absent. Because
 * canopy credit shares units with per-tree credit, the share is the
 * fraction of shade the count can't see -- so a shown count always covers
 * at least 75% of the route's shade story. Calibrated against 250 seeded
 * citywide routes at the default preset (seed 20260829, weight 15, July,
 * 2026-08-29):
 *   - hides the count on 20.0% of sampled routes;
 *   - a 60/40 street/park route (share ~0.4) hides: the walker can see
 *     the park trees the number ignores, and that visible contradiction
 *     -- not any internal score ratio -- is the harm model here;
 *   - 0.5 ("hide only when the count stops being the majority of the
 *     score") would be wrong: score-majority is invisible to a walker,
 *     uncounted trees in plain view are not;
 *   - share 0.75+ is the absurd case either way (count 3 vs ~117
 *     unseen tree-equivalents; pure-canopy routes counting 0). */
export const CANOPY_SHARE_HIDES_TREE_COUNT = 0.25
