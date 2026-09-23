import { useEffect, useState } from 'react'
import { type Point } from './api'
import { Controls } from './components/Controls'
import { DEFAULT_TREE_WEIGHT, snapToPreset } from './presets'
import { Header } from './components/Header'
import { MapView } from './components/MapView'
import { RouteStats } from './components/RouteStats'
import { useLocationFill } from './hooks/useLocationFill'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from './hooks/useMediaQuery'
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

/* A label param (`fromq`/`toq`) is display text for the matching point —
   only meaningful when that point parsed. Without it, reload used to
   reverse-geocode the bare coordinate, and the nearest-thing name that
   came back could differ from what was typed ("Court Street" reloading as
   a Montague Street address — user report 2026-09-02) or, when the lookup
   failed, stay raw coordinates. */
function initialLabel(params: URLSearchParams, labelKey: string, pointKey: string): string | null {
  return parsePoint(params.get(pointKey)) ? params.get(labelKey) : null
}

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
  } = useRouteQuery(
    parsePoint(initialParams.get('from')),
    parsePoint(initialParams.get('to')),
    initialTreeWeight(initialParams),
  )

  /* The fields' display labels, mirrored to the URL beside the points so a
     reload (or a shared link) shows the same text the fields showed — not
     a fresh reverse-geocode of the coordinate (see initialLabel above).
     Cleared whenever the matching point changes; the new text is reported
     back up by Controls once it exists. */
  const [startLabel, setStartLabel] = useState<string | null>(() =>
    initialLabel(initialParams, 'fromq', 'from'),
  )
  const [endLabel, setEndLabel] = useState<string | null>(() =>
    initialLabel(initialParams, 'toq', 'to'),
  )

  // The safety net against a stale label outliving its point: every path
  // that moves a point goes through these, and label-reporting (Controls)
  // happens after. React batches the pair, so the URL never sees the gap.
  function setStart(p: Point | null) {
    setStartLabel(null)
    updateStart(p)
  }
  function setEnd(p: Point | null) {
    setEndLabel(null)
    updateEnd(p)
  }

  /* Where the map should pan, imperatively: set to the location fix when
     it fills the start field, so the map visibly answers the tap even
     though RouteFraming deliberately ignores single points (a user in
     Brooklyn watched nothing move while the app stayed on the Village —
     2026-09-02). A fresh object per fill, so re-tapping ⌖ pans again even
     to the same spot. */
  const [panTarget, setPanTarget] = useState<Point | null>(null)

  const location = useLocationFill((fix) => {
    setStart({ lat: fix.lat, lon: fix.lon })
    setPanTarget({ lat: fix.lat, lon: fix.lon })
  })

  /* Mobile full-screen map (user, 2026-09-02: the 45vh mobile map "feels
     really cramped", worst right when a route exists and the panel matters
     least). The toggle hides the panel AND the header (user call
     2026-09-02) — iPhone Safari has no element fullscreen API, so this is
     a layout mode, not the Fullscreen API. Mobile-only: the desktop
     two-column layout already gives the map most of the screen.
     `mapExpanded` is derived, not stored, so resizing/rotating past the
     breakpoint restores the full layout on its own — the raw flag just
     waits, harmlessly, for the next mobile-width render. */
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const [wantMapExpanded, setWantMapExpanded] = useState(false)
  const mapExpanded = wantMapExpanded && isMobile

  // Escape backs out of the expanded map, matching every other dismissable
  // state in the app. Window-level and expanded-only: the panel (where the
  // search overlay has its own Escape handling) is display:none while this
  // listener exists, so the two can never both be live.
  useEffect(() => {
    if (!mapExpanded) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setWantMapExpanded(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [mapExpanded])

  // Mirror state → URL (labels included — the point is the truth, the
  // label is what the field showed for it).
  useEffect(() => {
    const params = new URLSearchParams()
    if (start) params.set('from', formatPoint(start))
    if (start && startLabel) params.set('fromq', startLabel)
    if (end) params.set('to', formatPoint(end))
    if (end && endLabel) params.set('toq', endLabel)
    params.set('w', String(treeWeight))
    window.history.replaceState(null, '', `?${params}`)
  }, [start, end, treeWeight, startLabel, endLabel])

  // Map clicks fill A, then B, then start a fresh route.
  function handleMapClick(p: Point) {
    if (!start || (start && end)) {
      setStart(p)
      setEnd(null)
    } else {
      setEnd(p)
    }
  }

  return (
    <div className={styles.app}>
      {/* Skip link: first thing keyboard focus reaches, jumps past the map.
          Gone while the map is expanded — its target is display:none, so a
          link to it would be a silent no-op. */}
      {!mapExpanded && (
        <a href="#controls" className={styles.skipLink}>
          Skip to route controls
        </a>
      )}

      {/* The shared header component — About renders the same one; every
          header comment lives in Header.tsx now. Unmounted (not hidden)
          while the map is expanded: it holds no state worth preserving —
          the cursor-blink once-per-tab memo is module-level in Header.tsx,
          so a remount can't re-fire it. */}
      {!mapExpanded && <Header page="map" />}

      {/* <main> wraps BOTH the map and the panel: the panel is the app's
          primary input, not a sidebar — without it the page is an empty
          map of New York — so it can't honestly be <aside>/"complementary"
          (craftsmanship review 2026-09-09). Each half is its own named
          region inside main: MapView's "Map", and the <section> below. */}
      <main className={styles.layout}>
        <div className={styles.mapArea}>
          {/* The wordmark <h1> left with the header, and a page with zero
              headings is a real hole in the a11y tree (axe caught it:
              page-has-heading-one), not a formality — so expanded mode
              keeps the page's one h1, visually hidden since the mode's
              whole point is spending no pixels on chrome, and inside main
              (all content belongs to a landmark). Plain "Shade Walker",
              not the underscored wordmark: there's nothing visual here to
              theme, and VoiceOver reads "Shade_walker" as one mushed word
              (the reason Header.tsx's link carries the same spoken form). */}
          {mapExpanded && <h1 className={styles.visuallyHidden}>Shade Walker</h1>}
          <MapView
            start={snappedStart ?? start}
            end={snappedEnd ?? end}
            selected={selected}
            baseline={baseline}
            position={location.position}
            panTo={panTarget}
            onMapClick={handleMapClick}
            expanded={mapExpanded}
            onToggleExpanded={isMobile ? () => setWantMapExpanded((v) => !v) : null}
          />
        </div>

        {/* A named <section> = a "region" landmark, so the panel is still
            one jump away in a landmark list. Stays MOUNTED while the map
            is expanded, unlike the header: display:none (panelHidden)
            keeps the address fields' typed-but-unresolved text and the
            aria-live regions alive, while still removing the panel from
            the a11y tree and tab order. */}
        <section
          id="controls"
          tabIndex={-1}
          className={mapExpanded ? `${styles.panel} ${styles.panelHidden}` : styles.panel}
          aria-label="Route controls and details"
        >
          <Controls
            treeWeight={treeWeight}
            onTreeWeightChange={setTreeWeight}
            start={start}
            end={end}
            onSetStart={setStart}
            onSetEnd={setEnd}
            initialStartLabel={startLabel}
            initialEndLabel={endLabel}
            onStartLabel={setStartLabel}
            onEndLabel={setEndLabel}
            locationStatus={location.status}
            onUseLocation={location.request}
            error={error}
            selected={selected}
            baseline={baseline}
          />
          <RouteStats route={selected} description={route?.description ?? ''} loading={loading} />
        </section>
      </main>
    </div>
  )
}
