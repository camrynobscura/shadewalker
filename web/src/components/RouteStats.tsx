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
    <section aria-label="Route details">
      <h2 className={styles.heading}>Your green route</h2>

      <dl className={styles.stats}>
        <div>
          <dt>Distance</dt>
          <dd>
            {formatDistance(green.length_m)} <span className={styles.quiet}>({green.minutes} min)</span>
          </dd>
        </div>
        <div>
          <dt>Trees along the way</dt>
          <dd>{green.tree_count}</dd>
        </div>
        <div>
          <dt>vs. shortest path</dt>
          <dd>
            {extra_trees > 0
              ? `+${extra_trees} trees for +${formatDistance(extra_length_m)}`
              : 'the shortest path is already the greenest'}
          </dd>
        </div>
      </dl>

      {isSparse && (
        <p className={styles.sparseNote}>
          This area has few street trees ({green.tree_count} over{' '}
          {formatDistance(green.length_m)}) — this is the best available, but expect
          limited shade.
        </p>
      )}

      <h3 className={styles.subheading}>Directions</h3>
      <p className={styles.description}>{data.description}</p>

      <p className={styles.quiet}>
        Shortest alternative: {formatDistance(shortest.length_m)} ({shortest.minutes} min),{' '}
        {shortest.tree_count} trees — shown dashed on the map.
      </p>
    </section>
  )
}

function formatDistance(meters: number): string {
  return meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`
}
