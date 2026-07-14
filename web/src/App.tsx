import { useEffect, useState } from 'react'
import { fetchCoverage, type CoverageFeature, type Point } from './api'
import { Controls, DEFAULT_TREE_WEIGHT, snapToPreset } from './components/Controls'
import { MapView } from './components/MapView'
import { RouteStats } from './components/RouteStats'
import { useGeolocation } from './hooks/useGeolocation'
import { useRouteQuery } from './hooks/useRouteQuery'
import styles from './App.module.css'

/* ── URL state ────────────────────────────────────────────────────────────
   The whole route request lives in the query string (?from=lat,lon&to=…&w=…)
   so any route is bookmarkable and shareable. Read once at startup, write on
   every change with replaceState (which doesn't pollute Back-button history). */

function parsePoint(value: string | null): Point | null {
  if (!value) return null
  const [lat, lon] = value.split(',').map(Number)
  return Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon } : null
}

function formatPoint(p: Point): string {
  return `${p.lat.toFixed(5)},${p.lon.toFixed(5)}`
}

// Old bookmarks may carry any slider value 0–40; snap it to a preset.
function initialTreeWeight(params: URLSearchParams): number {
  const w = Number(params.get('w'))
  return params.has('w') && Number.isFinite(w) ? snapToPreset(w) : DEFAULT_TREE_WEIGHT
}

const initialParams = new URLSearchParams(window.location.search)

export default function App() {
  const {
    start,
    end,
    treeWeight,
    route,
    selected,
    baseline,
    loading,
    error,
    snappedStart,
    snappedEnd,
    setTreeWeight,
    setStart: updateStart,
    setEnd: updateEnd,
    clear: handleClear,
  } = useRouteQuery(
    parsePoint(initialParams.get('from')),
    parsePoint(initialParams.get('to')),
    initialTreeWeight(initialParams),
  )

  const [locationEnabled, setLocationEnabled] = useState(false)
  const position = useGeolocation(locationEnabled)

  const [coverage, setCoverage] = useState<CoverageFeature | null>(null)
  // Fetched once, not tied to any route request. Purely a visual aid — the
  // server enforces real coverage on every /route call regardless of
  // whether this loaded, so a failure here just means no boundary drawn.
  useEffect(() => {
    fetchCoverage()
      .then(setCoverage)
      .catch(() => {})
  }, [])

  // Mirror state → URL.
  useEffect(() => {
    const params = new URLSearchParams()
    if (start) params.set('from', formatPoint(start))
    if (end) params.set('to', formatPoint(end))
    params.set('w', String(treeWeight))
    window.history.replaceState(null, '', `?${params}`)
  }, [start, end, treeWeight])

  // Map clicks fill A, then B, then start a fresh route.
  function handleMapClick(p: Point) {
    if (!start || (start && end)) {
      updateStart(p)
      updateEnd(null)
    } else {
      updateEnd(p)
    }
  }

  return (
    <div className={styles.app}>
      {/* Skip link: first thing keyboard focus reaches, jumps past the map. */}
      <a href="#controls" className={styles.skipLink}>
        Skip to route controls
      </a>

      <header className={styles.header}>
        <h1 className={styles.title}>
          Shadewalker <span className={styles.tagline}>- find the shadiest walking route in NYC</span>
        </h1>
        {/* The one piece of visible instruction guaranteed to be on screen
            before any scrolling, on every viewport size -- it's rendered
            before the map in DOM order, so it survives the mobile layout's
            stack-map-above-panel reflow (the old copy of this text lived at
            the bottom of the control panel, past the address fields and
            Shade_priority slider, invisible without scrolling on mobile).
            Always rendered, never conditionally hidden -- it was originally
            tied to "no route yet" and toggled off once a route existed, but
            that meant re-picking a point on an existing route (e.g. a new
            start while the old route is still showing) made it flicker back
            in and out, visibly shifting the whole layout on every click.
            Always-on trades a little permanent header height for a header
            that never jumps around mid-interaction. */}
        <p className={styles.instructions}>
          Tap the map to set a start and end point, or search two addresses below.
        </p>
      </header>

      <div className={styles.layout}>
        <main className={styles.mapArea}>
          <MapView
            start={snappedStart ?? start}
            end={snappedEnd ?? end}
            selected={selected}
            baseline={baseline}
            coverage={coverage}
            position={position}
            onMapClick={handleMapClick}
          />
        </main>

        <aside id="controls" tabIndex={-1} className={styles.panel} aria-label="Route controls and details">
          <Controls
            treeWeight={treeWeight}
            onTreeWeightChange={setTreeWeight}
            onSetStart={updateStart}
            onSetEnd={updateEnd}
            onClear={handleClear}
            position={position}
            locationEnabled={locationEnabled}
            onEnableLocation={() => setLocationEnabled(true)}
            hasRoute={route !== null}
            error={error}
            selected={selected}
            baseline={baseline}
          />
          <RouteStats route={selected} description={route?.description ?? ''} loading={loading} />
        </aside>
      </div>
    </div>
  )
}
