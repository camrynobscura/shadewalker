import type { RouteResponse } from '../api'
import { formatDistance } from '../format'
import styles from './RouteStats.module.css'

/** Below ~0.02 trees-per-meter along the route, even the "greenest" option
 * is objectively sparse — say so instead of overselling (honest stats). */
const SPARSE_TREES_PER_M = 0.02

interface RouteStatsProps {
  data: RouteResponse | null
  loading: boolean
  error: string | null
}

export function RouteStats({ data, loading, error }: RouteStatsProps) {
  // This div must stay mounted unconditionally — aria-live only announces
  // *changes* to an already-present node, so swapping it in and out of the
  // DOM (rather than just its content) risks the first update going
  // unannounced. But an empty live region shouldn't claim the --space-lg
  // gap above it the way a populated one does, so the margin that
  // separates it from Controls is conditional on there being anything to
  // show — not the div's own presence.
  const hasContent = loading || Boolean(error) || Boolean(data)
  return (
    <div aria-live="polite" className={hasContent ? styles.liveRegion : undefined}>
      {loading && <p className={styles.quiet}>Finding your route…</p>}
      {error && <p className={styles.error}>{error}</p>}
      {data && !loading && !error && <StatsBody data={data} />}
    </div>
  )
}

function StatsBody({ data }: { data: RouteResponse }) {
  const green = data.green.properties
  const isSparse = green.tree_count / green.length_m < SPARSE_TREES_PER_M

  return (
    /* No visible heading here — the section's aria-label carries the
       accessible name instead, matching the flat terminal-readout layout
       the Greenhouse design uses in place of prose headings. */
    <section aria-label="Route details" className={styles.section}>
      <div className={styles.statRow}>
        <div className={styles.stat}>
          <span className={styles.statVal}>{formatDistance(green.length_m)}</span>
          <span className={styles.statLabel}>dist</span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statVal}>
            {Math.round(green.minutes)}
            <small> min</small>
          </span>
          <span className={styles.statLabel}>eta</span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statVal}>{green.tree_count}</span>
          <span className={styles.statLabel}>trees</span>
        </div>
      </div>

      {isSparse && (
        <p className={styles.sparseNote}>
          // LOW_TREE_DENSITY: {green.tree_count} over {formatDistance(green.length_m)} — expect
          limited shade
        </p>
      )}

      {green.segments.length > 0 ? (
        /* Ordered list, not the old one-sentence paragraph: each turn gets
           its own line, and a screen reader announces "item 2 of 4" instead
           of one long run-on. Built from `segments` (structured data)
           rather than parsing `data.description` (English prose), so it
           can use formatDistance() and stay unit-consistent with the rest
           of the panel. Always the shadiest route's directions — Shade
           priority's NONE option gives the plain shortest route directly
           (same segments), so there's no separate route to switch to here. */
        <ol className={styles.directionsList}>
          {green.segments.map((segment, i) => (
            <li key={i}>
              {i === 0 ? 'Head' : 'then'} {formatDistance(segment.length_m)} along {segment.name}
            </li>
          ))}
        </ol>
      ) : (
        // Empty segments means start == end, so the server's generic
        // "already there" text is accurate.
        <p className={styles.description}>&gt; {data.description}</p>
      )}
    </section>
  )
}
