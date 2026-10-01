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
import { shadeLayersFromLink } from './shadeLayers'
import { formatWalkTime, walkTimeFromLink, walkTimeParam } from './walkTime'
import styles from './App.module.css'

/* ── URL state ────────────────────────────────────────────────────────────
   The whole route request lives in the query string (?from=lat,lon&to=…&w=…,
   plus &at=2026-09-27T09:00 once a departure time is set, or
   &arrive=… for an arrival, and &layers=trees|buildings once the shade
   pill is set) so any
   route is bookmarkable and shareable. Read once at startup, write on
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
   only meaningful when that point parsed. Without it, a reload would
   reverse-geocode the bare coordinate, and the nearest-thing name that
   comes back can differ from what was typed ("Court Street" reloading as
   a Montague Street address) or, when the lookup fails, stay raw
   coordinates. */
function initialLabel(params: URLSearchParams, labelKey: string, pointKey: string): string | null {
  return parsePoint(params.get(pointKey)) ? params.get(labelKey) : null
}

export default function App() {
  // Only the full-screen map button depends on it: the panel is the same
  // at every width and routes by itself.
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const {
    start,
    end,
    treeWeight,
    walkTime,
    layers,
    route,
    routeWalkTime,
    selected,
    baseline,
    loading,
    error,
    snappedStart,
    snappedEnd,
    setTreeWeight,
    setStart: updateStart,
    setEnd: updateEnd,
    setWalkTime,
    setLayers,
  } = useRouteQuery(
    parsePoint(initialParams.get('from')),
    parsePoint(initialParams.get('to')),
    initialTreeWeight(initialParams),
    walkTimeFromLink(initialParams),
    shadeLayersFromLink(initialParams),
  )

  /* The fields' display labels, mirrored to the URL beside the points so a
     reload (or a shared link) shows the same text the fields showed — not
     a fresh reverse-geocode of the coordinate (see initialLabel above).
     Cleared whenever the matching point changes; the new text is reported
     back up by Controls once it exists. */
  const [startLabel, setStartLabel] = useState<string | null>(() =>
    initialLabel(initialParams, 'fromq', 'from'),
  )
  const [endLabel, setEndLabel] = useState<string | null>(() => initialLabel(initialParams, 'toq', 'to'))

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
     though RouteFraming deliberately ignores single points. A fresh
     object per fill, so re-tapping ⌖ pans again even to the same spot. */
  const [panTarget, setPanTarget] = useState<Point | null>(null)

  /* The latest address typed or picked in a field, for the map to bring
     into view while it's the only point (MapView's RevealLonePoint):
     otherwise its marker lands off-screen. Only the fields set it -- a
     map tap lands where the user is already looking. */
  const [revealPoint, setRevealPoint] = useState<Point | null>(null)
  function setStartFromField(p: Point | null) {
    setStart(p)
    if (p) setRevealPoint(p)
  }
  function setEndFromField(p: Point | null) {
    setEnd(p)
    if (p) setRevealPoint(p)
  }

  const location = useLocationFill((fix) => {
    setStart({ lat: fix.lat, lon: fix.lon })
    setPanTarget({ lat: fix.lat, lon: fix.lon })
  })

  /* Mobile full-screen map: the 45vh mobile map feels cramped, worst
     right when a route exists and the panel matters least. The toggle
     hides the panel and the header — iPhone Safari has no element
     fullscreen API, so this is
     a layout mode, not the Fullscreen API. Mobile-only: the desktop
     two-column layout already gives the map most of the screen.
     `mapExpanded` is derived, not stored, so resizing/rotating past the
     breakpoint restores the full layout on its own — the raw flag just
     waits, harmlessly, for the next mobile-width render. */
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
  // label is what the field showed for it). The time only when one was
  // picked: a link without it means "now" whenever it's opened, which
  // is what most shared routes want.
  useEffect(() => {
    const params = new URLSearchParams()
    if (start) params.set('from', formatPoint(start))
    if (start && startLabel) params.set('fromq', startLabel)
    if (end) params.set('to', formatPoint(end))
    if (end && endLabel) params.set('toq', endLabel)
    params.set('w', String(treeWeight))
    if (walkTime) params.set(walkTimeParam(walkTime), formatWalkTime(walkTime))
    if (layers !== 'both') params.set('layers', layers)
    window.history.replaceState(window.history.state, '', `?${params}`)
  }, [start, end, treeWeight, walkTime, layers, startLabel, endLabel])

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

      {/* <main> wraps both the map and the panel: the panel is the app's
          primary input, not a sidebar — without it the page is an empty
          map of New York — so it can't honestly be <aside>/"complementary".
          Each half is its own named region inside main: MapView's "Map",
          and the <section> below. */}
      <main className={styles.layout}>
        <div className={mapExpanded ? `${styles.mapArea} ${styles.mapAreaExpanded}` : styles.mapArea}>
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
            reveal={revealPoint}
            onMapClick={handleMapClick}
            expanded={mapExpanded}
            onToggleExpanded={isMobile ? () => setWantMapExpanded((v) => !v) : null}
          />
        </div>

        {/* A named <section> = a "region" landmark, so the panel is still
            one jump away in a landmark list. Stays mounted while the map
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
            onSetStart={setStartFromField}
            onSetEnd={setEndFromField}
            initialStartLabel={startLabel}
            initialEndLabel={endLabel}
            onStartLabel={setStartLabel}
            onEndLabel={setEndLabel}
            locationStatus={location.status}
            onUseLocation={location.request}
            error={error}
            selected={selected}
            night={route?.night ?? false}
            walkTime={walkTime}
            onWalkTimeChange={setWalkTime}
            layers={layers}
            onLayersChange={setLayers}
            routes={route?.routes ?? null}
            routeWalkTime={routeWalkTime}
            routeLayers={route?.layers ?? 'both'}
            loading={loading}
          />
          <RouteStats route={selected} description={route?.description ?? ''} loading={loading} />
        </section>
      </main>
    </div>
  )
}
