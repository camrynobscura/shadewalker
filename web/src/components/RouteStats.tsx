import { Fragment } from 'react'
import type { RouteFeature, RouteStep } from '../api'
import { formatDistance, formatDistanceParts, formatEtaParts } from '../format'
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

/* Stroke paths for the turn glyphs, 16x16. Drawn from axis-aligned and
   45° segments only, with square caps and miter joins — the same
   zero-radius language as the rest of the theme. Arrowheads are open
   strokes, not filled triangles, to stay wireframe. */
const GLYPH_PATHS: Record<Exclude<RouteStep['action'], 'depart'>, string> = {
  /* Three rules hold across every path below, all settled against the
     rendered 18px (one viewBox unit = 1.125px):

     1. WHATEVER RUNS INTO AN ARROWHEAD STOPS ONE UNIT SHORT OF THE APEX.
        Run it all the way in and the 1.8-wide stroke's square cap
        projects past the head's outline as two small corners flanking
        the point — measured 0.42px on the elbows and sharps, 0.18px on
        continue's shaft. Ending a unit early tucks the cap inside the
        head's own stroke. cross_side never showed it because its bar
        already stopped short, which is what first identified the cause.

     2. EVERY glyph's x-extent centers on 8, so a column of them lines
        up. Four were exceptions — the elbows sat at 9 and 7, the sharps
        at 8.75 and 7.25, i.e. left-vs-right differing by 2.25px and
        1.7px in a vertical rail. Each pair is now an exact mirror about
        x=8. (Ink centers still differ by ~0.2px, because a miter point
        reaches 1.27 units past an apex while a square cap reaches 0.9 —
        not worth off-scale coordinates to chase.)

     3. EVERY TAIL SHOWS 3 UNITS BELOW ITS HEAD, ending at y=11, not the
        y=14 they were first drawn at. An arrow's visual mass sits in its
        upper half, so a longer tail was a lone stroke hanging past the
        step text's baseline — it read as a descender and made the arrow
        look uncentered. At y=11 the inked bottom (plus the cap's 0.9)
        lands on the baseline; y=14 hung below it and y=10 floated above,
        both tried. This is why continue's shaft is 5.5 units rather than
        3: it starts up inside the head at y=5.5 and only the run from
        the head's own y=8 downward is visible, so it shows the same 3
        units as an elbow's stub and every glyph bottoms out together. */
  continue: 'M8 11 V5.5 M4.5 8 L8 4.5 L11.5 8',
  /* Elbow wings reach y=5.25/10.75, not the 4.5/11.5 they were first
     drawn at (still exact 45deg; tip reach 2.75 units, was 3.5). At full
     height the HEAD was the descender: a square cap on a 45deg tip
     corners out 1.27 units past the endpoint (not the 0.9 of a flat
     cap), so the lower wing inked ~1px below the text baseline even
     with the stub sitting on it (user call: the pointer fits inside the
     text's height). At 2.75 the head bottoms out level with the stub,
     and its reach matches cross_side's heads, already at 5.25/10.75. */
  left: 'M12 11 V8 H5 M6.75 5.25 L4 8 L6.75 10.75',
  right: 'M4 11 V8 H11 M9.25 5.25 L12 8 L9.25 10.75',
  /* Sharps redrawn 2026-08-29 (user call, judged against live routes:
     Prospect Park's West Dr -> East Dr wishbone). The old drawing had
     three measured defects: the head's arm overlapped the shaft's stroke
     by 0.8u ("touching"), the tail inked 2u below every other glyph's
     shared 11.9u bottom, and the 45deg bend mitered into a 2.35u spike.
     Now: shaft at x=11.25 bottoming on the family line; a 45deg return
     sweeping the full width to an L-head whose corner IS the point
     (wings right+up = pointing down-left); 1.7u of daylight between the
     wing end and the shaft. The bend is a REAL JOIN, kept sane by the
     path's strokeMiterlimit={2}: a 45deg miter would spike 2.35u past
     the corner, and abutting two capped subpaths instead was tried and
     visibly misfit (the diagonal's edge peeled off the stub's flank
     ~2px below its top -- user caught it). The limit turns joins
     tighter than 60deg into a flat chamfer; the sharps' bend is the
     ONLY join under 60deg in all six glyphs, so nothing else changes.
     Ink extents center on 8 exactly and top out level with the
     elbows. */
  sharp_left: 'M11.25 11 V4.5 L5.5 10.25 M7.75 11 H4.75 V8',
  sharp_right: 'M4.75 11 V4.5 L10.5 10.25 M8.25 11 H11.25 V8',
  /* Heads kept shallow so the wings don't crowd the middle — a clear
     stretch of shaft must stay visible between them (user call). */
  cross_side: 'M4 8 H12 M5.5 5.25 L3 8 L5.5 10.75 M10.5 5.25 L13 8 L10.5 10.75',
}

/** Turn glyph for one step — a visual double of stepText's verb, so the
 * svg is aria-hidden and the text stays the accessible instruction.
 * depart is a solid square (a block cursor: "you are here, start"); the
 * rest are stroke arrows from GLYPH_PATHS. currentColor throughout, so
 * .stepGlyph's CSS color is the single ink knob. */
function StepGlyph({ action }: { action: RouteStep['action'] }) {
  return (
    <svg
      className={styles.stepGlyph}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
    >
      {action === 'depart' ? (
        <rect x="5.5" y="5.5" width="5" height="5" fill="currentColor" />
      ) : (
        <path
          d={GLYPH_PATHS[action]}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="square"
          strokeLinejoin="miter"
          /* Bevels only the sharps' 45deg bend (see GLYPH_PATHS); every
             other join is >=90deg and keeps its miter point. */
          strokeMiterlimit={2}
        />
      )}
    </svg>
  )
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
 * most real cover ("83% shaded, 3 trees", FIXES item 4).
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
const CANOPY_SHARE_HIDES_TREE_COUNT = 0.25

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
        {/* Label above value (user call 2026-08-28), eta leading — the
            question a walker asks first. DOM order matches visual order,
            so screen readers also announce label-then-value. A real
            <dl> since 2026-08-30: these are key-value pairs, and dt/dd
            gives AT the term-to-value association the old spans only
            implied by proximity (each pair wrapped in a div, which HTML
            allows inside <dl> exactly for this styling shape). */}
        <dl className={styles.statRow}>
          <div className={styles.stat}>
            <dt className={styles.statLabel}>eta</dt>
            <dd className={styles.statVal}>
              {formatEtaParts(stats.minutes).map((part, i) => (
                <Fragment key={part.unit}>
                  {i > 0 ? ' ' : null}
                  {part.value}
                  <small> {part.unit}</small>
                </Fragment>
              ))}
            </dd>
          </div>
          <div className={styles.stat}>
            {/* Unit in the same lighter <small> the eta box's "min" gets --
                the value is the datum, the unit is context. */}
            <dt className={styles.statLabel}>dist</dt>
            <dd className={styles.statVal}>
              {dist.value}
              <small> {dist.unit}</small>
            </dd>
          </div>
          <div className={styles.stat}>
            <dt className={styles.statLabel}>shaded</dt>
            <dd className={styles.statVal}>{shownShadePct}%</dd>
          </div>
          {stats.park_canopy_share < CANOPY_SHARE_HIDES_TREE_COUNT && (
            <div className={styles.stat}>
              <dt className={styles.statLabel}>trees</dt>
              <dd className={styles.statVal}>{stats.tree_count}</dd>
            </div>
          )}
        </dl>

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
          <strong>// CAUTION:</strong> walking routes may not always reflect
          real-world conditions
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
                <StepGlyph action={step.action} />
                <span>
                  {stepText(step)} — {formatDistance(step.length_m)}
                </span>
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
