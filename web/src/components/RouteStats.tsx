import type { RouteFeature } from '../api'
import { formatDistance, formatDistanceParts } from '../format'
import styles from './RouteStats.module.css'

/** Below ~0.02 trees-per-meter along the route, even the "greenest" option
 * is objectively sparse — say so instead of overselling (honest stats). */
const SPARSE_TREES_PER_M = 0.02

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
  const isSparse = stats.tree_count / stats.length_m < SPARSE_TREES_PER_M
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
          <div className={styles.stat}>
            <span className={styles.statVal}>{stats.tree_count}</span>
            <span className={styles.statLabel}>trees</span>
          </div>
          <div className={styles.stat}>
            <span className={styles.statVal}>{Math.round(stats.shade_fraction * 100)}%</span>
            <span className={styles.statLabel}>shaded</span>
          </div>
        </div>

        {isSparse && (
          <p className={styles.sparseNote}>
            // LOW_TREE_DENSITY: {stats.tree_count} over {formatDistance(stats.length_m)} — expect
            limited shade
          </p>
        )}

        {stats.segments.length > 0 ? (
          /* Ordered list, not the old one-sentence paragraph: each turn gets
             its own line, and a screen reader announces "item 2 of 4" instead
             of one long run-on. Built from `segments` (structured data)
             rather than parsing `description` (English prose), so it
             can use formatDistance() and stay unit-consistent with the rest
             of the panel. Always the selected preset's directions -- Shade
             priority's NONE option gives the plain shortest route directly
             (same segments), so there's no separate route to switch to here. */
          <ol className={styles.directionsList}>
            {stats.segments.map((segment, i) => (
              <li key={i}>
                {i === 0 ? 'Head' : 'then'} {formatDistance(segment.length_m)} along {segment.name}
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
