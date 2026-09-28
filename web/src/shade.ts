/** The display shade curve: measured canopy coverage -> estimated
 * experienced shade. DISPLAY-ONLY -- routing, clamp_shade_monotonic and
 * the API's shade_fraction are untouched; this maps the number at the
 * last moment before a person reads it.
 *
 * WHY: plan-view coverage understates what a walk feels like. The
 * measured mechanism is lane choice -- a walker drifts to the shadiest
 * line the pavement offers, so experienced shade tracks the best lane,
 * while shade_fraction is calibrated to the strip AVERAGE. Derived
 * 2026-08-27 by tools/audit/derive_display_curve_k.py: over 4,000
 * seeded-random sidewalk edges (425.5 km), best 1m walking lane vs the
 * production 2m-strip average gives k = 1.19 (+/-1m band) / 1.23
 * (+/-1.5m), least-squares and length-weighted-median estimators
 * agreeing within 0.03 -- and the implied k is near-constant across
 * coverage deciles (1.15-1.25 above 20% cover), which validates the
 * FORM, not just the value: one exponent fits the whole range without
 * distorting any part of it.
 *
 * WHY THE FLOOR AND NOT MORE (user decision, 2026-08-27): perceptual
 * calibration pointed at 1.2-1.4, but judgment on real streets is
 * contaminated by BUILDING shade, which the app did not model then --
 * an exponent tuned to match street-level feel would have double-counted
 * buildings once building shade landed as its own layer. It did (PLAN
 * `building-shadows`, 2026-09): the same estimator on the three-across
 * building samples gives k 1.13-1.87 at July noon (hard-edged wall
 * bands reward lane choice) and 1.04-1.64 at other slots. KEPT at 1.2
 * (user, 2026-09-25): display-only, and a per-layer exponent would need
 * per-layer fractions in the response, which the design rejects.
 * Wide park paths are knowingly under-served (lane choice
 * grows with width: path-kind measures k 1.46-1.72, the Mall ~1.8);
 * inflating the whole city to chase them was declined.
 *
 * The map is strictly monotone with fixed points at 0 and 1: a bare
 * route stays bare, full coverage stays full, and a shadier route
 * always displays higher than a less shady one -- which is why the
 * Shade_priority monotonicity guarantee survives untouched. Anything
 * that compares DISPLAYED values (the stat, the LOW_SHADE warning
 * text, Controls' +N% delta) must go through displayShade; anything
 * that reasons about MEASURED coverage (thresholds derived on that
 * scale) stays raw. */
export const DISPLAY_SHADE_EXPONENT = 1.2

/** Measured coverage fraction (0-1) -> displayed shade fraction (0-1). */
export function displayShade(fraction: number): number {
  return 1 - (1 - fraction) ** DISPLAY_SHADE_EXPONENT
}

/** (Moved here from RouteStats.tsx 2026-08-31, when the warning
 * itself moved into the Shade_priority box -- the threshold belongs
 * beside the display curve it is calibrated against.)
 *
 * Below this shade_fraction, the route is objectively exposed — say so
 * instead of overselling (honest stats). Reads the same continuous stat
 * displayed as "% shaded" right above it, so the warning and the number
 * can never disagree (the old rule read tree_count/length instead, a
 * different signal entirely — a Central Park route could show "83%
 * shaded" AND this warning).
 *
 * RE-DERIVED 2026-08-26 for the coverage scale, 0.20 -> 0.15. The 0.20
 * bar was chosen against the display that saturated at density 0.02 —
 * which the leaf-cover exchange rate later revealed to be ~65% real
 * coverage, i.e. an inflated scale. When shade_fraction became measured
 * coverage (DENSITY_AT_FULL_COVERAGE, 2026-08-26) every displayed number
 * dropped ~12-15 points and 0.20 began firing on 29% of default-preset
 * April routes and 59% of April no-priority ones — worse than the 0.25
 * value the previous derivation explicitly REJECTED for firing on 26%
 * and 57%. Same failure, so same treatment: re-measure, don't re-tune.
 *
 * Measured on 188 routable random pairs (the routing harness's seeded
 * draw; the 2026-08-24 derivation found borough reweighting moved every
 * figure <1pt, so unweighted, with ±2-3pt sampling noise per cell).
 * Share of routes warned on the COVERAGE scale:
 *
 *             July MED  July NONE  April MED  April NONE
 *     0.125       1.1%       5.3%       9.6%      21.3%
 *     0.15        2.7%       8.5%      12.8%      35.1%   <- chosen
 *     0.20        4.8%      21.3%      29.3%      58.5%
 *
 * 0.15 reproduces the firing profile 0.20 was originally PICKED to
 * deliver (July MED ~5%/NONE ~15%, April MED ~15%/NONE ~38%) — the same
 * editorial judgment about when exposure deserves saying, re-expressed on
 * the truthful scale. And on this scale the words finally mean exactly
 * what they say: below 15% covered, 85% of the walk is in open sun. The
 * one high figure, 35%, is on April NONE ("fastest route, no detours for
 * shade") where the user has already said shade is not a priority.
 *
 * Seasonal variation is deliberate, not drift: April really is less shaded
 * than July (CANOPY_BY_MONTH), so the same bar firing more in spring is
 * the honest geography.
 *
 * The 2026-08-27 display curve (shade.ts) does NOT move this bar: the
 * comparison stays on the raw measured fraction, and because
 * displayShade is strictly monotone, exactly the same routes fire as
 * before -- the firing profile above is preserved without re-derivation.
 * Only the PRINTED numbers go through the curve (both of them, stat and
 * warning, so they can never disagree); at this bar the warning shows
 * itself at ~18% displayed rather than 15% measured, and its words stay
 * true either way. */
export const LOW_SHADE_FRACTION = 0.15

/** When at least this share of the route's tree score is park-canopy AREA
 * credit (not countable trees), hide the tree count -- the "> N trees
 * along the way" line under the route rows (the "trees: N" stat before
 * 2026-09-27) -- the count
 * can't see area credit, so it undersells exactly the routes with the
 * most real cover ("83% shaded, 3 trees").
 *
 * 0.25, user decision 2026-08-28: the count is flavor, so it should be
 * accurate or absent. Because canopy credit shares units with per-tree
 * credit, the share IS the fraction of shade the count can't see -- so
 * a shown count always covers at least 75% of the route's shade story.
 * Calibrated against 250 seeded citywide routes at the default preset
 * (seed 20260829, weight 15, July; method in history/quick-fixes.md):
 *   - hides the stat on 20.0% of sampled routes (the 2026-08-17 guess
 *     of 1/3 hid 13.2%);
 *   - a 60/40 street/park route (share ~0.4) HIDES: the walker can SEE
 *     the park trees the number ignores, and that visible contradiction
 *     -- not any internal score ratio -- is the harm model here;
 *   - 0.5 ("hide only when the count stops being the majority of the
 *     score") was derived first and REJECTED: score-majority is
 *     invisible to a walker, uncounted trees in plain view are not.
 *     Don't re-raise it without evidence about perception, not scores;
 *   - share 0.75+ is the absurd case either way (count 3 vs ~117
 *     unseen tree-equivalents; pure-canopy routes counting 0). */
export const CANOPY_SHARE_HIDES_TREE_COUNT = 0.25
