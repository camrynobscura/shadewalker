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
 * contaminated by BUILDING shade, a mechanism this app does not model
 * -- an exponent tuned to match street-level feel would double-count
 * buildings if building shade ever lands as its own layer. 1.2 is the
 * measured, building-independent part, correct with or without that
 * future. Wide park paths are knowingly under-served (lane choice
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
