import { useEffect, useState } from 'react'
import type { RouteFeature, RouteStep } from '../api'
import { formatDistance, spokenDistance } from '../format'
import { ShareIcon } from './icons'
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

     1. Whatever runs into an arrowhead stops one unit short of the apex.
        Run it all the way in and the 1.8-wide stroke's square cap
        projects past the head's outline as two small corners flanking
        the point (0.42px on the elbows and sharps, 0.18px on continue's
        shaft). Ending a unit early tucks the cap inside the head's own
        stroke.

     2. Every glyph's x-extent centers on 8, so a column of them lines
        up; each left/right pair is an exact mirror about x=8. (Ink
        centers still differ by ~0.2px, because a miter point reaches
        1.27 units past an apex while a square cap reaches 0.9 — not
        worth off-scale coordinates to chase.)

     3. Every tail shows 3 units below its head, ending at y=11. An
        arrow's visual mass sits in its upper half, so a longer tail is a
        lone stroke hanging past the step text's baseline — it reads as
        a descender and makes the arrow look uncentered. At y=11 the
        inked bottom (plus the cap's 0.9) lands on the baseline. This is
        why continue's shaft is 5.5 units rather than 3: it starts up
        inside the head at y=5.5 and only the run from the head's own
        y=8 downward is visible, so it shows the same 3 units as an
        elbow's stub and every glyph bottoms out together. */
  continue: 'M8 11 V5.5 M4.5 8 L8 4.5 L11.5 8',
  /* Elbow wings reach y=5.25/10.75 (exact 45deg; tip reach 2.75 units).
     At full height the head is the descender: a square cap on a 45deg
     tip corners out 1.27 units past the endpoint (not the 0.9 of a flat
     cap), so a lower wing at 11.5 would ink ~1px below the text baseline
     even with the stub sitting on it. At 2.75 the head bottoms out level
     with the stub, and its reach matches cross_side's heads. */
  left: 'M12 11 V8 H5 M6.75 5.25 L4 8 L6.75 10.75',
  right: 'M4 11 V8 H11 M9.25 5.25 L12 8 L9.25 10.75',
  /* Sharps: shaft at x=11.25 bottoming on the family line; a 45deg
     return sweeping the full width to an L-head whose corner is the
     point (wings right+up = pointing down-left); 1.7u of daylight
     between the wing end and the shaft. The bend is a real join, kept
     sane by the path's strokeMiterlimit={2}: a 45deg miter would spike
     2.35u past the corner, and abutting two capped subpaths instead
     visibly misfits (the diagonal's edge peels off the stub's flank
     ~2px below its top). The limit turns joins tighter than 60deg into
     a flat chamfer; the sharps' bend is the only join under 60deg in
     all six glyphs, so nothing else changes. Ink extents center on 8
     exactly and top out level with the elbows. */
  sharp_left: 'M11.25 11 V4.5 L5.5 10.25 M7.75 11 H4.75 V8',
  sharp_right: 'M4.75 11 V4.5 L10.5 10.25 M8.25 11 H11.25 V8',
  /* Heads kept shallow so the wings don't crowd the middle — a clear
     stretch of shaft must stay visible between them. */
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
  // Spoken-only, first route only: the arrival announcement otherwise
  // jumps from "FINDING" straight to results, right past the
  // Shade_priority control -- the one control that matters most at that
  // exact moment. A screen-reader user who never wanders upward would
  // simply not know it exists. Announced once; the flag flips after the
  // first arrival and the tip unmounts (removals are never announced).
  const [priorityHinted, setPriorityHinted] = useState(false)
  // An effect ON PURPOSE (the one oxlint `set-state-in-effect` exception):
  // the flag must flip AFTER the tip has been on screen, so the live region
  // has seen it arrive. Set while rendering, as the rule prefers, it would
  // flip before the tip ever reached the screen, and nothing would be read.
  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
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
            {/* The growing vine: one path draws itself across the open
                panel -- horizontal growth reads as progress with no bar --
                sprouting leaves as the tip passes and running visibly
                behind the label's letters (no backing patch, no box). */}
            {/* No visible text: the vine alone is the pending state --
                motion with a direction reads as work, and the
                visually-hidden sentence above keeps the spoken
                announcement intact. The one exception is reduced motion,
                where there is no motion to read as work and a still vine
                is just decoration: CSS swaps the vine for this plain line.
                aria-hidden with the rest of the block — the sentence above
                is the spoken one. */}
            <p className={styles.loadingText}>
              <span>&gt; </span>finding your route…
            </p>
            <div className={styles.vineStage}>
              <svg viewBox="0 0 520 64" preserveAspectRatio="none" className={styles.vineSvg}>
                {/* One vine. Leaf anchors are computed points on the
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
          above the address fields (its `error` prop) -- not here. */}
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
        {/* Icon is decoration — the aria-label above stays the whole
            spoken name. */}
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

  return (
    <>
      {/* A real heading (the only one below the page's own <h1>): this
          introduces read-only output, the chosen route's directions, so
          it gets a heading's navigation benefit rather than a label's.
          Plain "Directions" has no underscore to split into a spoken
          twin. */}
      <h2 className={styles.sectionTitle}>Directions</h2>
      <div className={styles.routeBody}>
        <div className={styles.directionsGroup}>
          {/* The one caution the product owes every route, above the list
              so it reads before the instructions do. Deliberately general:
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
                  the explanation. */}
              <a className={styles.cautionLink} href="/about.html#caution">
                learn more
              </a>
            </span>
          </p>

          {stats.segments.length > 0 ? (
            /* Ordered list, not one sentence: each turn gets its own line,
               and a screen reader announces "item 2 of 4" instead of one
               long run-on. Built from `segments` (structured data) rather
               than parsing `description` (English prose), so it can use
               formatDistance() and stay unit-consistent with the rest of
               the panel. Always the selected preset's directions -- Shade
               priority's NONE option gives the plain shortest route directly
               (same segments), so there's no separate route to switch to here. */
            /* role="list" is not redundant: the stylesheet sets
               list-style: none for the flush-left numbering, which strips
               the list role in Safari/VoiceOver. */
            <ol className={styles.directionsList} role="list">
              {stats.segments.map((step, i) => (
                <li key={i}>
                  <StepGlyph action={step.action} />
                  {/* Two lines by design, everywhere: a one-liner "text —
                      524 ft" sits right at the panel's line length at every
                      width, so the wrap point is whatever falls last — the
                      bare number, the bare unit — a different orphan per
                      step. Instruction first (it's what you scan for
                      mid-walk), then how far to continue along it, as its
                      own quieter line. */}
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
