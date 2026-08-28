import type { RouteFeature, RouteStep } from '../api'
import { formatDistance, formatDistanceParts } from '../format'
import { displayShade } from '../shade'
import styles from './RouteStats.module.css'

/** One step's instruction text, minus the distance (the <li> appends
 * that uniformly). Must tell the same story as server/app.py's
 * _describe — the aria description and this list are twins. */
function stepText(step: RouteStep): string {
  const side = step.side ? ` (${step.side} side)` : ''
  switch (step.action) {
    case 'depart':
      return `Head ${step.heading} on ${step.name}${side}`
    case 'continue':
      return `Continue onto ${step.name}${side}`
    case 'cross_side':
      return `Cross to the ${step.side} side of ${step.name}`
    case 'sharp_left':
      return `Turn sharply left onto ${step.name}${side}`
    case 'sharp_right':
      return `Turn sharply right onto ${step.name}${side}`
    default:
      return `Turn ${step.action} onto ${step.name}${side}`
  }
}

/** Below this shade_fraction, the route is objectively exposed — say so
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
const LOW_SHADE_FRACTION = 0.15

/** When at least this share of the route's tree score is park-canopy AREA
 * credit (not countable trees), hide the "trees: N" stat -- the count
 * can't see area credit, so it undersells exactly the routes with the
 * most real cover ("83% shaded, 3 trees", FIXES item 4). Measured
 * 2026-08-17: park loops (Central/Prospect/Riverside) read 0.40-0.51,
 * ordinary street routes 0.000, a park-adjacent street 0.297 -- 1/3
 * hides the count only where canopy genuinely dominates.
 *
 * LIVE as of 2026-08-26: the park-canopy pipeline step fills
 * tree_park_canopy on 57,507 kerb-less edges, so this threshold fires for
 * the first time (a walk across Central Park reads canopy share 0.96 and
 * correctly hides its near-meaningless tree count). The 1/3 value itself
 * is still the 2026-08-17 guess, never derived against the live signal —
 * calibrating it deliberately is in PLAN's Unresolved list. */
const CANOPY_SHARE_HIDES_TREE_COUNT = 1 / 3

interface RouteStatsProps {
  /** The currently selected Shade_priority preset's route -- /route
   * computes all four presets in one request, App.tsx picks this one out
   * by treeWeight. */
  route: RouteFeature | null
  /** Only actually used when `route.properties.segments` is empty (start
   * == end) -- see the fallback below. */
  description: string
  loading: boolean
}

export function RouteStats({ route, description, loading }: RouteStatsProps) {
  // This div must stay mounted unconditionally — aria-live only announces
  // *changes* to an already-present node, so swapping it in and out of the
  // DOM (rather than just its content) risks the first update going
  // unannounced. But an empty live region shouldn't claim the --space-lg
  // gap above it the way a populated one does, so the margin that
  // separates it from Controls is conditional on there being anything to
  // show — not the div's own presence.
  const hasContent = loading || Boolean(route)
  return (
    // Third of the panel's three top-level sections -- a plain div, not a
    // <section> (see Controls.module.css's .sectionDivider comment for
    // why), kept as the aria-live host since that attribute works on any
    // element and needs to stay mounted for screen readers to catch the
    // first update.
    <div aria-live="polite" className={hasContent ? styles.sectionDivider : undefined}>
      {loading && <p className={styles.quiet}>Finding your route…</p>}
      {/* A rejected route's error message lives in App.tsx's header now,
          not here -- see the comment there. This component only ever
          rendered it when a route successfully loaded anyway, so there's
          nothing route-specific left for this component to say about it. */}
      {route && !loading && <StatsBody route={route} description={description} />}
    </div>
  )
}

function StatsBody({ route, description }: { route: RouteFeature; description: string }) {
  const stats = route.properties
  const isLowShade = stats.shade_fraction < LOW_SHADE_FRACTION
  // Computed once and used by BOTH the stat and the warning below --
  // the invariant that the warning can never disagree with the number
  // now includes agreeing about the display curve.
  const shownShadePct = Math.round(displayShade(stats.shade_fraction) * 100)
  const dist = formatDistanceParts(stats.length_m)

  return (
    <>
      {/* Same visual label style Start_point/End_point and the
          Shade_priority legend already use, but a real heading here (the
          only one below the page's own <h1>) -- unlike those two, this
          text isn't captioning a form control, it's introducing a block
          of read-only output (the stats and directions below), so it
          gets a heading's own navigation benefit (screen readers can jump
          between headings) instead of a label/legend's control-naming
          role, which wouldn't apply here. Kept as a plain sibling of
          .section, not a child of it, so its own margin-bottom controls
          the title-to-content gap directly -- a child of .section would
          pick up that flex container's uniform --space-md gap instead,
          which is right for the *other* gaps inside .section (stat row to
          directions list) but too loose for a title hugging its own
          content, the same tight relationship .presetGroup legend's
          margin-bottom already gets. */}
      <h2 className={styles.sectionTitle}>My_route</h2>
      <div className={styles.section}>
        <div className={styles.statRow}>
          <div className={styles.stat}>
            {/* Unit in the same lighter <small> the eta box's "min" gets --
                the value is the datum, the unit is context. */}
            <span className={styles.statVal}>
              {dist.value}
              <small> {dist.unit}</small>
            </span>
            <span className={styles.statLabel}>dist</span>
          </div>
          <div className={styles.stat}>
            <span className={styles.statVal}>
              {Math.round(stats.minutes)}
              <small> min</small>
            </span>
            <span className={styles.statLabel}>eta</span>
          </div>
          {stats.park_canopy_share < CANOPY_SHARE_HIDES_TREE_COUNT && (
            <div className={styles.stat}>
              <span className={styles.statVal}>{stats.tree_count}</span>
              <span className={styles.statLabel}>trees</span>
            </div>
          )}
          <div className={styles.stat}>
            <span className={styles.statVal}>{shownShadePct}%</span>
            <span className={styles.statLabel}>shaded</span>
          </div>
        </div>

        {isLowShade && (
          <p className={styles.sparseNote}>
            // LOW_SHADE: {shownShadePct}% shaded over{' '}
            {formatDistance(stats.length_m)} — expect mostly direct sun
          </p>
        )}

        {/* The one caution the product owes every route (user-approved
            wording, 2026-08-28), ABOVE the list so it reads before the
            instructions do (user call, 2026-08-28). Deliberately GENERAL:
            uncertainty about street names is disclosed structurally,
            per-step, as "unnamed path", not by a blanket note. */}
        <p className={styles.disclaimer}>
          // CAUTION: routes follow map data — conditions on the ground may
          differ
        </p>

        {stats.segments.length > 0 ? (
          /* Ordered list, not the old one-sentence paragraph: each turn gets
             its own line, and a screen reader announces "item 2 of 4" instead
             of one long run-on. Built from `segments` (structured data)
             rather than parsing `description` (English prose), so it
             can use formatDistance() and stay unit-consistent with the rest
             of the panel. Always the selected preset's directions -- Shade
             priority's NONE option gives the plain shortest route directly
             (same segments), so there's no separate route to switch to here. */
          /* role="list" is NOT redundant: the stylesheet sets
             list-style: none for the flush-left numbering, which strips
             the list role in Safari/VoiceOver. */
          <ol className={styles.directionsList} role="list">
            {stats.segments.map((step, i) => (
              <li key={i}>
                {stepText(step)} — {formatDistance(step.length_m)}
              </li>
            ))}
          </ol>
        ) : (
          // Empty segments means start == end, so the server's generic
          // "already there" text is accurate.
          <p className={styles.description}>&gt; {description}</p>
        )}
      </div>
    </>
  )
}
