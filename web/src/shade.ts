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
 * geography. */
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
