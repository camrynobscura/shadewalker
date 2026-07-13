import { useEffect, useState } from 'react'
import { fetchCoverage, fetchRoute, RouteError, type CoverageFeature, type Point, type RouteResponse } from './api'
import { Controls, DEFAULT_TREE_WEIGHT, snapToPreset } from './components/Controls'
import { MapView } from './components/MapView'
import { RouteStats } from './components/RouteStats'
import { useGeolocation } from './hooks/useGeolocation'
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

const initialParams = new URLSearchParams(window.location.search)

export default function App() {
  // Lazy initializers (the () => form) run once, on first render only —
  // afterwards the URL follows the state, not the other way around.
  const [start, setStart] = useState<Point | null>(() => parsePoint(initialParams.get('from')))
  const [end, setEnd] = useState<Point | null>(() => parsePoint(initialParams.get('to')))
  const [treeWeight, setTreeWeight] = useState<number>(() => {
    // Old bookmarks may carry any slider value 0–40; snap it to a preset.
    const w = Number(initialParams.get('w'))
    return initialParams.has('w') && Number.isFinite(w) ? snapToPreset(w) : DEFAULT_TREE_WEIGHT
  })

  const [route, setRoute] = useState<RouteResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

  // Fetch whenever the request changes. The AbortController in the cleanup
  // cancels the in-flight request each time a newer one supersedes it (e.g.
  // dragging the slider) — otherwise slow responses could arrive out of
  // order and paint a stale route over a fresh one.
  useEffect(() => {
    if (!start || !end) {
      setRoute(null)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    fetchRoute(start, end, treeWeight, controller.signal)
      .then((data) => {
        setRoute(data)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return // superseded, not an error
        // RouteError = the server responded with a specific, useful reason
        // (e.g. outside coverage) — show that. Anything else (dead server,
        // no network) gets the generic fallback instead of a raw fetch error.
        setError(err instanceof RouteError ? err.message : 'Could not find a route — is the server running?')
        setLoading(false)
      })
    return () => controller.abort()
  }, [start, end, treeWeight])

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
      setStart(p)
      setEnd(null)
    } else {
      setEnd(p)
    }
  }

  function handleClear() {
    setStart(null)
    setEnd(null)
    setRoute(null)
    setError(null)
  }

  return (
    <div className={styles.app}>
      {/* Skip link: first thing keyboard focus reaches, jumps past the map. */}
      <a href="#controls" className={styles.skipLink}>
        Skip to route controls
      </a>

      <header className={styles.header}>
        <h1 className={styles.title}>
          Shady Stroll <span className={styles.tagline}>- find the shadiest path for your route</span>
        </h1>
      </header>

      <div className={styles.layout}>
        <main className={styles.mapArea}>
          <MapView
            start={start}
            end={end}
            green={route?.green ?? null}
            shortest={route?.shortest ?? null}
            coverage={coverage}
            position={position}
            onMapClick={handleMapClick}
          />
        </main>

        <aside id="controls" tabIndex={-1} className={styles.panel} aria-label="Route controls and details">
          <Controls
            treeWeight={treeWeight}
            onTreeWeightChange={setTreeWeight}
            onSetStart={setStart}
            onSetEnd={setEnd}
            onClear={handleClear}
            position={position}
            locationEnabled={locationEnabled}
            onEnableLocation={() => setLocationEnabled(true)}
            hasRoute={route !== null}
            route={route}
          />
          <RouteStats data={route} loading={loading} error={error} />
          {!start && !end && (
            <p className={styles.hint}>
              Click the map (A, then B) or type two addresses to find your shadiest walk.
            </p>
          )}
        </aside>
      </div>
    </div>
  )
}
