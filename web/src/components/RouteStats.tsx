import type { RouteResponse } from '../api'
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
  return (
    /* aria-live="polite": screen readers announce whatever appears in here
       (new route, error) after the user's current action finishes. */
    <div aria-live="polite">
      {loading && <p className={styles.quiet}>Finding your route…</p>}
      {error && <p className={styles.error}>{error}</p>}
      {data && !loading && !error && <StatsBody data={data} />}
    </div>
  )
}

function StatsBody({ data }: { data: RouteResponse }) {
  const green = data.green.properties
  const shortest = data.shortest.properties
  const { extra_length_m, extra_trees } = data.comparison
  const isSparse = green.tree_count / green.length_m < SPARSE_TREES_PER_M

  return (
    /* No visible heading here — the section's aria-label carries the
       accessible name instead, matching the flat terminal-readout layout
       the Greenhouse design uses in place of prose headings. */
    <section aria-label="Route details">
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

      <p className={styles.compare}>
        {extra_trees > 0
          ? `+${extra_trees} trees detected · anomaly: +${formatDistance(extra_length_m)} · acceptable`
          : '// no anomaly — shortest path is already the greenest'}
      </p>

      {isSparse && (
        <p className={styles.sparseNote}>
          // LOW_TREE_DENSITY: {green.tree_count} over {formatDistance(green.length_m)} — expect
          limited shade
        </p>
      )}

      <p className={styles.description}>&gt; {data.description}</p>

      <p className={styles.quiet}>
        // shortest_alt: {formatDistance(shortest.length_m)} · {Math.round(shortest.minutes)} min ·{' '}
        {shortest.tree_count} trees (dashed on map)
      </p>
    </section>
  )
}

const METERS_PER_FOOT = 0.3048
const FEET_PER_MILE = 5280

function formatDistance(meters: number): string {
  const feet = meters / METERS_PER_FOOT
  // Same idea as the old km/m split, just at imperial units: short routes
  // read better as whole feet, longer ones as miles with one decimal —
  // the convention US map apps use.
  return feet >= FEET_PER_MILE * 0.1
    ? `${(feet / FEET_PER_MILE).toFixed(1)} mi`
    : `${Math.round(feet)} ft`
}
