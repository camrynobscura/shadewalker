import { useEffect, useState } from 'react'
import { fetchCoverage, type CoverageFeature, type Point } from './api'
import { Controls } from './components/Controls'
import { DEFAULT_TREE_WEIGHT, snapToPreset } from './presets'
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
          {/* The wordmark is a home link -- clicking it navigates to "/"
              (no query params), the app's default state, which clears any
              route (user call 2026-08-31). A full navigation, not an
              in-place clear, so it also resets the map center and zoom to
              default -- a true reset. aria-label: VoiceOver reads
              "Shade_walker" as one mushed word; the label speaks it as two
              while the screen keeps the underscore. */}
          <a href="/" className={styles.homeLink} aria-label="Shade Walker, home">
            Shade_walker
            {/* Decorative terminal cursor — never announced. */}
            <span className={styles.cursor} aria-hidden="true" />
          </a>
        </h1>
        {/* Tagline + instructions ride BESIDE the wordmark (user call
            2026-08-30: the header was spending three stacked lines of
            height on text that earns one row). Siblings of the h1, not
            inside it: the accessible heading stays just the wordmark. */}
        <div className={styles.headerText}>
          <p className={styles.tagline}>
            {/* The prompt glyph is decoration -- unspoken, or every read
                starts with "greater than" (VoiceOver pass, 2026-08-31). */}
            <span aria-hidden="true">&#62; </span>find the shadiest walking route in NYC
          </p>
          {/* Always rendered, never toggled on route state -- it once
              hid itself when a route existed, and re-picking a point made
              it flicker in and out, shifting the layout on every click.
              Living in the header (before the map in DOM order) also keeps
              it on screen without scrolling in the mobile stack. */}
          <p className={styles.instructions}>
            <span aria-hidden="true">&#62; </span>tap the map to set a start and end point, or search two addresses below
          </p>
        </div>
        {/* A real navigation, not a bare link: a full page of its own
            deserves the landmark. margin-left auto rides the header's
            flex row to the right edge. */}
        <nav className={styles.headerNav} aria-label="Site">
          <a className={styles.aboutLink} href="/about.html">
            ABOUT
          </a>
        </nav>
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
            start={start}
            end={end}
            onSetStart={updateStart}
            onSetEnd={updateEnd}
            onClear={handleClear}
            position={position}
            locationEnabled={locationEnabled}
            onEnableLocation={() => setLocationEnabled(true)}
            canClear={start !== null || end !== null}
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
