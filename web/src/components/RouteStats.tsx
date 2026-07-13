import { useId, useState } from 'react'
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
  const shortest = data.shortest.properties
  const { extra_length_m, extra_trees } = data.comparison
  const isSparse = green.tree_count / green.length_m < SPARSE_TREES_PER_M

  // Which route's turn-by-turn is showing. Own piece of state (not lifted
  // to App) since it's a display choice about data already in `data` —
  // nothing else needs to know about it. Persists across new route
  // fetches since React keeps state for the same component instance.
  const [directionsFor, setDirectionsFor] = useState<'green' | 'shortest'>('green')
  const activeSegments = directionsFor === 'green' ? green.segments : shortest.segments
  const groupName = useId()

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

      {/* Directions can show either route's turn-by-turn — the map only
          ever drew the shadiest one, but someone comparing routes should
          be able to read the fastest one's directions too. */}
      <fieldset className={styles.toggleGroup}>
        <legend>Directions_for</legend>
        <div className={styles.toggleSeg}>
          <label className={styles.toggleItem}>
            <input
              type="radio"
              name={groupName}
              checked={directionsFor === 'green'}
              onChange={() => setDirectionsFor('green')}
              className={styles.toggleInput}
            />
            <span className={styles.toggleText}>SHADIEST</span>
          </label>
          <label className={styles.toggleItem}>
            <input
              type="radio"
              name={groupName}
              checked={directionsFor === 'shortest'}
              onChange={() => setDirectionsFor('shortest')}
              className={styles.toggleInput}
            />
            <span className={styles.toggleText}>FASTEST</span>
          </label>
        </div>
      </fieldset>

      {activeSegments.length > 0 ? (
        /* Ordered list, not the old one-sentence paragraph: each turn gets
           its own line, and a screen reader announces "item 2 of 4" instead
           of one long run-on. Built from `segments` (structured data)
           rather than parsing `data.description` (English prose), so it
           can use formatDistance() and stay unit-consistent with the rest
           of the panel. */
        <ol className={styles.directionsList}>
          {activeSegments.map((segment, i) => (
            <li key={i}>
              {i === 0 ? 'Head' : 'then'} {formatDistance(segment.length_m)} along {segment.name}
            </li>
          ))}
        </ol>
      ) : (
        // Empty segments means start == end for both routes alike, so the
        // server's generic "already there" text is accurate either way.
        <p className={styles.description}>&gt; {data.description}</p>
      )}

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
