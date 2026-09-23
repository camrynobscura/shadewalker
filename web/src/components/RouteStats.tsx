import { Fragment, useEffect, useState } from 'react'
import type { RouteFeature, RouteStep } from '../api'
import { formatDistance, formatDistanceParts, formatEtaParts, spokenDistance, spokenEta } from '../format'
import { ShareIcon } from './icons'
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
    <svg className={styles.stepGlyph} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
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

/** When at least this share of the route's tree score is park-canopy AREA
 * credit (not countable trees), hide the "trees: N" stat -- the count
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
  // Spoken-only, FIRST route only: the arrival announcement otherwise
  // jumps from "FINDING" straight to results, right past the
  // Shade_priority control -- the one control that matters most at that
  // exact moment. A screen-reader user who never wanders upward would
  // simply not know it exists (VoiceOver pass, 2026-08-31). Announced
  // once; the flag flips after the first arrival and the tip unmounts
  // (removals are never announced).
  const [priorityHinted, setPriorityHinted] = useState(false)
  useEffect(() => {
    if (route && !loading) setPriorityHinted(true)
  }, [route, loading])
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
      {loading && (
        <>
          {/* The spoken version stays the plain sentence this live region
              has always announced; the terminal-voice visuals below are
              decoration a screen reader shouldn't spell out. */}
          <p className={styles.visuallyHidden}>Finding your route…</p>
          <div aria-hidden="true" className={styles.loadingBlock}>
            {/* The growing vine (picked over a glitch box, falling leaves
                and a pixel tree, 2026-09-01): one path draws itself across
                the open panel -- horizontal growth reads as progress with
                no bar -- sprouting leaves as the tip passes and running
                visibly behind the label's letters (no backing patch; user
                call). Deliberately no box either: that treatment belonged
                to the rejected glitch candidate. */}
            {/* No visible text (user call, 2026-09-01, after trying the
                label overlaid, above, and colored): the vine alone is the
                pending state -- motion with a direction reads as work, and
                the visually-hidden sentence above keeps the spoken
                announcement intact. The ONE exception is reduced motion,
                where there is no motion to read as work and a still vine
                is just decoration: CSS swaps the vine for this plain line
                (user call 2026-09-23; until then the block sat at
                opacity 0 for the whole wait, a static opacity the
                cancelled animation never lifted). aria-hidden with the
                rest of the block — the sentence above is the spoken one. */}
            <p className={styles.loadingText}>
              <span>&gt; </span>finding your route…
            </p>
            <div className={styles.vineStage}>
              <svg viewBox="0 0 520 64" preserveAspectRatio="none" className={styles.vineSvg}>
                {/* One vine (tried three 2026-09-01, walked back same day —
                    user call). Leaf anchors are computed points ON the
                    Bézier path (crest/trough/slope), so every leaf's base
                    touches the vine and grows out of it — origin classes
                    put the scale-from point at that base corner. Pointed
                    almond shape: two quadratics meeting sharp at base and
                    tip; leaves cluster in pairs near the wave's turns,
                    the way real vines bunch. */}
                {/* The tail is an explicit C, not another S: an S mirrors
                    the previous control to (524,48) — PAST the endpoint —
                    which hooked the vine back on itself at the very end.
                    This one continues the incoming slope and eases flat. */}
                <path
                  className={styles.vinePath}
                  d="M4 32 C 44 16, 84 48, 124 32 S 204 16, 244 32 S 324 48, 364 32 S 444 16, 484 32 C 494 36, 504 38.5, 518 39"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.oBL} ${styles.vl1}`}
                  d="M34 27.5 Q 39 11.5, 54 14.5 Q 48 28.5, 34 27.5 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.vineLeafDim} ${styles.oTR} ${styles.vl2}`}
                  d="M94 36.5 Q 89 52.5, 74 49.5 Q 80 35.5, 94 36.5 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.oBR} ${styles.vl6}`}
                  d="M154 23 Q 149 7, 134 10 Q 140 24, 154 23 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.oBL} ${styles.vl3}`}
                  d="M184 20 Q 189 4, 204 7 Q 198 21, 184 20 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.vineLeafDim} ${styles.oTL} ${styles.vl4}`}
                  d="M304 44 Q 309 60, 324 57 Q 318 43, 304 44 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.oTL} ${styles.vl7}`}
                  d="M334 41 Q 339 57, 354 54 Q 348 40, 334 41 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.oBR} ${styles.vl5}`}
                  d="M424 20 Q 419 4, 404 7 Q 410 21, 424 20 Z"
                />
                <path
                  className={`${styles.vineLeaf} ${styles.vineLeafDim} ${styles.oBL} ${styles.vl8}`}
                  d="M454 23 Q 459 7, 474 10 Q 468 24, 454 23 Z"
                />
              </svg>
            </div>
          </div>
        </>
      )}
      {/* A rejected route's error message is Controls' alert slot, right
          above the address fields (its `error` prop) -- not here. This
          component only ever rendered it when a route successfully loaded
          anyway, so there's nothing route-specific left for it to say. */}
      {route && !loading && !priorityHinted && (
        <p className={styles.visuallyHidden}>
          Tip: the Shade priority setting above these results chooses how far the route detours for extra
          shade.
        </p>
      )}
      {route && !loading && <StatsBody route={route} description={description} />}
    </div>
  )
}

/** Shares the current route. The whole request already lives in the URL
 * (App.tsx mirrors start/end/weight to the query string), so "share" is
 * just handing that URL along -- the native share sheet where the browser
 * has one (mobile/Safari), a clipboard copy with confirmation everywhere
 * else. */
function ShareButton() {
  const [copied, setCopied] = useState(false)

  async function onShare() {
    const url = window.location.href
    if (navigator.share) {
      // Native sheet: canceling is a normal outcome, not an error, and
      // needs no clipboard fallback on a device that has share.
      try {
        await navigator.share({ title: 'Shade Walker route', url })
      } catch {
        /* dismissed */
      }
      return
    }
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    } catch {
      /* clipboard blocked (insecure context, denied) -- no graceful action */
    }
  }

  return (
    <div className={styles.share}>
      <button type="button" className={styles.shareButton} onClick={onShare} aria-label="Share route">
        {/* Icon is decoration (user ask 2026-09-03) — the aria-label
            above stays the whole spoken name. */}
        <ShareIcon />
        SHARE_ROUTE
      </button>
      {/* Polite live region so the copy is announced without stealing focus. */}
      <span className={styles.shareStatus} role="status">
        {copied ? 'link copied' : ''}
      </span>
    </div>
  )
}

function StatsBody({ route, description }: { route: RouteFeature; description: string }) {
  const stats = route.properties
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
          .routeBody, not a child of it, so its own margin-bottom controls
          the title-to-content gap directly -- a child would pick up that
          flex container's uniform --space-lg gap instead, which is right
          for the *other* gaps inside it (stat row to directions list) but
          too loose for a title hugging its own content, the same tight
          relationship the Shade_priority legend's margin-bottom gets. */}
      {/* Content split, not aria-label: inside the aria-live wrapper a
          label AND the text can both be announced -- "My route" twice
          (VoiceOver pass). aria-hidden text is excluded from both the
          announcement and the heading's name; the sr twin serves both. */}
      <h2 className={styles.sectionTitle}>
        <span aria-hidden="true">My_route</span>
        <span className={styles.visuallyHidden}>My route</span>
      </h2>
      <div className={styles.routeBody}>
        {/* Label above value (user call 2026-08-28), eta leading — the
            question a walker asks first. A plain named list, not a <dl>:
            it WAS a definition list (2026-08-30 to 2026-09-09), but every
            <dt> was aria-hidden, so the term-to-value pairing a <dl>
            promises never reached AT — each value speaks a self-contained
            phrase ("74 percent shaded") instead, a VoiceOver-pass call
            (2026-08-31: "eta" read as a word, and the values stand on
            their own). The markup now claims exactly that: four items.
            role="list" because .statRow sets list-style: none, which
            strips the role in Safari (same as the directions list). */}
        <ul className={styles.statRow} role="list" aria-label="Route stats">
          <li className={styles.stat}>
            {/* The visible labels are aria-hidden (see above); each value
                carries its own spoken twin with full words. */}
            <span className={styles.statLabel} aria-hidden="true">
              eta
            </span>
            {/* The compact visual ("2 hr 9 min") is aria-hidden; the
                sr-only twin speaks full words. Same pattern on distance. */}
            <span className={styles.statVal}>
              <span aria-hidden="true">
                {formatEtaParts(stats.minutes).map((part, i) => (
                  <Fragment key={part.unit}>
                    {i > 0 ? ' ' : null}
                    {part.value}
                    <small> {part.unit}</small>
                  </Fragment>
                ))}
              </span>
              <span className={styles.visuallyHidden}>{spokenEta(stats.minutes)}</span>
            </span>
          </li>
          <li className={styles.stat}>
            {/* Unit in the same lighter <small> the eta box's "min" gets --
                the value is the datum, the unit is context. */}
            <span className={styles.statLabel} aria-hidden="true">
              distance
            </span>
            <span className={styles.statVal}>
              <span aria-hidden="true">
                {dist.value}
                <small> {dist.unit}</small>
              </span>
              <span className={styles.visuallyHidden}>{spokenDistance(stats.length_m)}</span>
            </span>
          </li>
          <li className={styles.stat}>
            <span className={styles.statLabel} aria-hidden="true">
              shaded
            </span>
            <span className={styles.statVal}>
              {/* Same <small> treatment AND same leading space as eta's
                  "min" and distance's "mi" -- the unit gap matches across
                  all three boxes (user call, 2026-08-31). */}
              <span aria-hidden="true">
                {shownShadePct}
                <small> %</small>
              </span>
              <span className={styles.visuallyHidden}>{shownShadePct} percent shaded</span>
            </span>
          </li>
          {stats.park_canopy_share < CANOPY_SHARE_HIDES_TREE_COUNT && (
            <li className={styles.stat}>
              <span className={styles.statLabel} aria-hidden="true">
                trees
              </span>
              <span className={styles.statVal}>
                <span aria-hidden="true">{stats.tree_count}</span>
                <span className={styles.visuallyHidden}>
                  {`${stats.tree_count} ${stats.tree_count === 1 ? 'tree' : 'trees'}`}
                </span>
              </span>
            </li>
          )}
        </ul>

        <div className={styles.directionsGroup}>
          <h3 className={styles.subTitle}>Directions</h3>

          {/* The one caution the product owes every route (user-approved
              wording, 2026-08-28), ABOVE the list so it reads before the
              instructions do (user call, 2026-08-28). Deliberately GENERAL:
              uncertainty about street names is disclosed structurally,
              per-step, as "unnamed path", not by a blanket note. */}
          <p className={styles.disclaimer}>
            <span className={styles.disclaimerMark} aria-hidden="true">
              &gt;
            </span>
            <span>
              {/* sr-only label: the ">" is decorative, so screen readers still
                  get the "caution" framing the visible text no longer states. */}
              <span className={styles.visuallyHidden}>Caution: </span>
              walking routes may not always reflect real-world conditions —{' '}
              {/* The moment a user doubts the data is the moment they'll take
                  the explanation (user call 2026-08-30). */}
              <a className={styles.cautionLink} href="/about.html#caution">
                learn more
              </a>
            </span>
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
                  {/* Two lines by design, everywhere (user, 2026-09-03):
                      the one-liner "text — 524 ft" sat right at the panel's
                      line length at every width, so the wrap point was
                      whatever fell last — the bare number, the bare unit —
                      a different orphan per step. Instruction first (it's
                      what you scan for mid-walk), then how far to continue
                      along it, as its own quieter line. */}
                  <span>
                    <span aria-hidden="true" className={styles.stepMain}>
                      {stepText(step)}
                    </span>
                    <span aria-hidden="true" className={styles.stepDist}>
                      {formatDistance(step.length_m)}
                    </span>
                    {/* Spoken twin: "524 feet", not "five two four F T". */}
                    <span className={styles.visuallyHidden}>
                      {stepText(step)}, {spokenDistance(step.length_m)}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            // Empty segments means start == end, so the server's generic
            // "already there" text is accurate.
            <p className={styles.description}>
              <span aria-hidden="true">&gt; </span>
              {description}
            </p>
          )}
        </div>

        <ShareButton />
      </div>
    </>
  )
}
