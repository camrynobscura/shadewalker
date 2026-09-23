import { divIcon, type Map as LeafletMap } from 'leaflet'
import { useEffect, useId, useRef } from 'react'
import {
  Circle,
  CircleMarker,
  MapContainer,
  Marker,
  Polyline,
  TileLayer,
  useMap,
  useMapEvents,
} from 'react-leaflet'
import type { Point, RouteFeature } from '../api'
import type { GeoPosition } from '../hooks/useGeolocation'
import { CollapseIcon, CrosshairIcon, ExpandIcon } from './icons'
import styles from './MapView.module.css'

// Where the map opens before any route exists: Washington Square, framing
// Greenwich Village + the East Village (user call 2026-09-01). Midtown
// looked dramatic but its shade scores are low and FLAT, so first clicks
// there returned near-identical routes across every preset; Village blocks
// vary enough that the presets visibly diverge, which is the actual demo.
// (Was Midtown 2026-08-31; Carroll Gardens, the pilot area, before that.)
const INITIAL_CENTER: [number, number] = [40.7320, -73.9985]

/* CARTO started watermarking keyless raster tile requests in 2026-08
   ("API KEY REQUIRED" repeated across the map). The key is a build-time
   input (VITE_CARTO_KEY in web/.env.local, gitignored) and is public by
   nature — it rides in every tile URL a visitor's browser requests, so
   keeping it out of git is rotation hygiene, not secrecy. Keyless builds
   still render, just watermarked, which keeps dev and e2e working with
   no local setup. Key mechanics: docs.carto.com/faqs/carto-basemaps. */
const CARTO_KEY = import.meta.env.VITE_CARTO_KEY
const TILE_URL =
  'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png' +
  (CARTO_KEY ? `?key=${CARTO_KEY}` : '')

/* Markers as labeled divIcons: start and end are told apart by their letter,
   not color -- both render the same magenta (see MapView.module.css), so
   color-blind users aren't relying on hue alone (WCAG 1.4.1 "use of
   color"). */
const startIcon = divIcon({
  className: '', // suppress Leaflet's default white-box styling
  html: `<span class="${styles.marker} ${styles.markerStart}" aria-hidden="true">A</span>`,
  iconSize: [30, 30],
  iconAnchor: [15, 15],
})
const endIcon = divIcon({
  className: '',
  html: `<span class="${styles.marker} ${styles.markerEnd}" aria-hidden="true">B</span>`,
  iconSize: [30, 30],
  iconAnchor: [15, 15],
})

/** GeoJSON stores [lon, lat]; Leaflet wants [lat, lon]. One flip, one place. */
function toLatLngs(feature: RouteFeature): [number, number][] {
  return feature.geometry.coordinates.map(([lon, lat]) => [lat, lon])
}


const reducedMotion = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** Invisible helper: react-leaflet hooks only work in children of
 * MapContainer, so map-click handling lives in its own tiny component. */
function ClickHandler({ onMapClick }: { onMapClick: (p: Point) => void }) {
  useMapEvents({
    click: (e) => onMapClick({ lat: e.latlng.lat, lon: e.latlng.lng }),
  })
  return null
}

/** Repaints the map whenever its CONTAINER resizes. Leaflet only watches
 * window resize (trackResize), so a container that changes size while the
 * window doesn't — the expand toggle hiding the panel and header — leaves
 * the newly revealed strip unpainted (grey, tile-less) until
 * invalidateSize runs. A ResizeObserver on the container covers every such
 * case structurally — this toggle and anything future — instead of syncing
 * a call to one specific state flip. animate:false: a mode flip should
 * repaint instantly, not slide. */
function InvalidateOnResize() {
  const map = useMap()
  useEffect(() => {
    const observer = new ResizeObserver(() => map.invalidateSize({ animate: false }))
    observer.observe(map.getContainer())
    return () => observer.disconnect()
  }, [map])
  return null
}

/* One padded rectangle, used in both directions: what fitBounds aims the
   route inside, and what the preset-switch guard in RouteFraming treats
   as "already visible". Uneven padding, not a uniform one: the legend
   (bottom-left, up to ~163x88px with all 3 rows shown), the Locate-me
   button (bottom-right), and on mobile the expand toggle (top-right,
   44px ending 54px from each edge) all float over the map itself, so a
   plain 48px on every side still let a fitted point land right behind
   one of them. paddingTopLeft's x covers the legend's width and its y
   clears the expand toggle's depth; paddingBottomRight's x clears the
   toggle's width and its y covers whichever bottom overlay is taller --
   that alone keeps every fitted point out of the bottom strip entirely,
   so it doesn't matter which corner it's actually closer to. */
const FIT_PAD_TOP_LEFT: [number, number] = [190, 60]
const FIT_PAD_BOTTOM_RIGHT: [number, number] = [60, 100]

/** Whether every point already sits inside the current view's padded
 * rectangle -- checked in screen pixels so the padding means exactly
 * what it means to fitBounds. */
function fullyVisible(map: LeafletMap, points: [number, number][]): boolean {
  const size = map.getSize()
  return points.every((point) => {
    const px = map.latLngToContainerPoint(point)
    return (
      px.x >= FIT_PAD_TOP_LEFT[0] &&
      px.y >= FIT_PAD_TOP_LEFT[1] &&
      px.x <= size.x - FIT_PAD_BOTTOM_RIGHT[0] &&
      px.y <= size.y - FIT_PAD_BOTTOM_RIGHT[1]
    )
  })
}

/** Keeps the whole route in view as start/end/the selected preset change --
 * without this, the map's viewport never moves on its own (INITIAL_CENTER
 * is only ever applied once, at mount), so with all 5 boroughs live, an
 * address search or click outside whatever's currently on screen would
 * compute and draw a real route the user can't actually see without
 * manually panning to find it. A preset switch only reframes when the
 * newly selected route actually leaves the padded view -- see the guard
 * below. */
function RouteFraming({
  start,
  end,
  selected,
  baseline,
}: {
  start: Point | null
  end: Point | null
  selected: RouteFeature | null
  baseline: RouteFeature | null
}) {
  const map = useMap()
  // The endpoint pair the map last framed, compared by VALUE: the same
  // pair firing this effect again means only the preset (or a re-fetch)
  // changed, which is the case the guard below may hold still.
  const framedPairRef = useRef<string | null>(null)

  useEffect(() => {
    if (!start || !end) {
      // Route cleared: the next complete pair frames unconditionally.
      framedPairRef.current = null
      return
    }
    const route = selected ?? baseline
    const points: [number, number][] = [
      [start.lat, start.lon],
      [end.lat, end.lon],
      ...(route ? toLatLngs(route) : []),
    ]
    const pair = `${start.lat},${start.lon}|${end.lat},${end.lon}`
    // Preset switch with the new route already fully on screen: hold the
    // camera. Refitting anyway nudged the map sideways on every
    // Shade_priority flip (each preset's bounds differ slightly), which
    // reads as jitter when flipping through routes to compare them
    // (user, 2026-09-07). A route that escapes the current view -- e.g.
    // switching to MAX while zoomed in on a MED detail -- still falls
    // through to the fit, pan and zoom both.
    if (pair === framedPairRef.current && fullyVisible(map, points)) return
    map.fitBounds(points, {
      paddingTopLeft: FIT_PAD_TOP_LEFT,
      paddingBottomRight: FIT_PAD_BOTTOM_RIGHT,
      maxZoom: 17,
      animate: !reducedMotion(),
    })
    framedPairRef.current = pair
    // Deliberately no handling for "only one of start/end set": panning
    // the instant point A lands was more disruptive than useful in
    // practice -- it re-centers/zooms the view around a point the user
    // likely just clicked while already looking straight at it. Wait for
    // the pair to frame together instead.
  }, [start, end, selected, baseline, map])

  return null
}


/** Explains the map's line styles. Real text (not just aria-hidden swatches)
 * so the meaning doesn't depend on noticing the color/dash difference —
 * screen readers get it too, since it's plain content in reading order,
 * not decoration. Rendered only once a route exists — there's nothing
 * to explain on an empty map. */
function Legend({ hasRoute }: { hasRoute: boolean }) {
  if (!hasRoute) return null
  return (
    // Named: an unnamed grouping announces as "list, 3 items" with no
    // clue what the list IS (2026-08-30 tree-read finding).
    <ul className={styles.legend} aria-label="Map legend">
      <li className={styles.legendRow}>
        <span className={styles.legendSwatch} aria-hidden="true" />
        shadiest route
      </li>
      <li className={styles.legendRow}>
        <span className={`${styles.legendSwatch} ${styles.legendSwatchDashed}`} aria-hidden="true" />
        fastest route
      </li>
    </ul>
  )
}

/** Pans to `point` whenever a NEW object arrives — the imperative "look
 * here" channel, used when a location fix fills the start field so the
 * map visibly answers the tap (RouteFraming deliberately ignores single
 * points, so without this a far-from-viewport fix changed nothing on
 * screen — user field report, 2026-09-02). Object identity is the
 * trigger on purpose: re-requesting location pans again even when the
 * fix lands on the same coordinates. */
function PanTo({ point }: { point: Point | null }) {
  const map = useMap()
  useEffect(() => {
    if (point) map.setView([point.lat, point.lon], 16, { animate: !reducedMotion() })
  }, [point, map])
  return null
}

/** Two things Leaflet's own DOM needs that react-leaflet can't set from
 * props (audit 2026-09-09). The container is a keyboard tab stop (arrow
 * keys pan, +/- zoom) that announced as nothing but its contents; it
 * gets a role and a short name — a group, not a landmark, so it's read
 * on focus and never in the landmark list — and `describedBy` points at
 * MapView's hidden instructions, so the keys are announced once on
 * focus rather than repeated inside the name (a name is read every
 * time; a description once). And the vector overlay's <svg> — the route
 * lines — is hidden from AT: it read as a nameless image, and the
 * directions list is the accessible route. The svg exists only once the
 * first path is drawn, so this re-checks on every render (one
 * querySelector); it must stay the LAST child of MapContainer so its
 * effect runs after the Polylines' have added their layers. */
function MapA11y({ describedBy }: { describedBy: string }) {
  const map = useMap()
  useEffect(() => {
    const container = map.getContainer()
    container.setAttribute('role', 'group')
    container.setAttribute('aria-label', 'Map')
    container.setAttribute('aria-describedby', describedBy)
    map.getPane('overlayPane')?.querySelector('svg')?.setAttribute('aria-hidden', 'true')
  })
  return null
}

/** "Locate me" — rendered inside the map so useMap() can pan it. */
function LocateButton({ position }: { position: GeoPosition | null }) {
  const map = useMap()
  if (!position) return null
  return (
    <button
      type="button"
      className={styles.locateButton}
      onClick={() => map.setView([position.lat, position.lon], 16, { animate: !reducedMotion() })}
    >
      {/* Icon is decoration (aria-hidden inside the component); the text
          after it is the accessible name. SVG, not the ⌖ character — that
          glyph renders as tofu in iOS's mono fallback chain (2026-09-02),
          and it's the same mark as the start field's location accessory. */}
      <CrosshairIcon /> Locate me
    </button>
  )
}

interface MapViewProps {
  start: Point | null
  end: Point | null
  /** The currently selected Shade_priority preset's route. */
  selected: RouteFeature | null
  /** The NONE (tree_weight=0) route, drawn alongside `selected` for
   * comparison -- identical to `selected` when NONE itself is the
   * selected preset, same as before this was named `green`/`shortest`. */
  baseline: RouteFeature | null
  position: GeoPosition | null
  /** Imperative pan target — see PanTo. */
  panTo: Point | null
  onMapClick: (p: Point) => void
  /** Whether the mobile full-screen map mode is on — flips the toggle's
   * icon and pressed state. The layout change itself (hiding the header
   * and panel) is App's, not this component's. */
  expanded: boolean
  /** null = don't render the toggle at all (desktop — the two-column
   * layout already gives the map most of the screen). */
  onToggleExpanded: (() => void) | null
}

export function MapView({
  start,
  end,
  selected,
  baseline,
  position,
  panTo,
  onMapClick,
  expanded,
  onToggleExpanded,
}: MapViewProps) {
  const helpId = useId()
  return (
    <div className={styles.mapRegion} role="region" aria-label="Map">
      {/* A landmark's aria-label is re-read every time a screen reader user
          navigates the landmark list, not just once -- a full instruction
          sentence there gets repetitive fast. Real usage instructions go
          here instead, visually hidden but still in the accessibility
          tree: read once, in normal order, the first time someone actually
          enters this region (e.g. via the landmarks list), not repeated on
          every subsequent landmark-list pass the way the label would be.
          Doubles as the map container's aria-describedby (MapA11y), so
          keyboard users focusing the map hear the keys once. */}
      <p id={helpId} className={styles.visuallyHidden}>
        Click to set your start and end points; arrow keys pan and plus and minus keys zoom. You can
        also type addresses in the route controls.
      </p>
      {/* zoomAnimation must be OFF under reduced motion, not just quick:
          base.css's prefers-reduced-motion rule nulls every CSS
          transition, and Leaflet's animated zoom waits on a transitionend
          event to leave its "animating" state -- an event a nulled
          transition may never fire. Stuck there, Leaflet silently ignores
          every later setView/fitBounds: one click of the +/- control
          could freeze route framing, PanTo and Locate-me for the rest of
          the session (found via the steady-preset-camera e2e,
          2026-09-07). Instant zoom is also simply what the preference
          asks for. Mount-time read by design: react-leaflet map options
          are immutable, and a mid-session OS toggle is rare enough to
          not chase. */}
      <MapContainer
        center={INITIAL_CENTER}
        zoom={15}
        zoomAnimation={!reducedMotion()}
        className={styles.map}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url={TILE_URL}
          subdomains={['a', 'b']} /* only the two hosts we preconnect in index.html */
          // detectRetina is DELIBERATELY OFF, and maxNativeZoom is why.
          //
          // With it on, Leaflet fills the URL's {r} token with @2x and asks
          // for tiles one zoom level ABOVE the map's own. CARTO serves @2x
          // only to z17: a z18 @2x request does not 404, it HANGS and times
          // out (measured 2026-08-24 -- z17 @2x 200, z18 @2x 15s timeout,
          // while plain z18 and z19 both return 200). Leaflet leaves a tile
          // it never receives blank, so zooming in past a point scattered
          // grey squares over the map and zooming back out cleared them.
          //
          // Plain tiles go to z19, so turning this off buys real detail at
          // exactly the zoom that matters here: seeing WHICH SIDE of a street
          // a route uses is the whole point of per-sidewalk routing. The cost
          // is softer rendering on high-DPI screens, accepted knowingly
          // (user decision, 2026-08-24). It should also help Lighthouse
          // performance, since a base tile is a quarter of @2x's pixels.
          //
          // maxNativeZoom is the belt-and-braces half: it caps what is
          // REQUESTED while maxZoom caps what the map allows, so past z19
          // Leaflet upscales the last real tile instead of requesting one
          // that may not exist. That makes blank tiles structurally
          // impossible even if CARTO's ceiling moves again.
          maxNativeZoom={19}
          maxZoom={20}
        />
        <ClickHandler onMapClick={onMapClick} />
        <InvalidateOnResize />
        <PanTo point={panTo} />
        <RouteFraming start={start} end={end} selected={selected} baseline={baseline} />

        {/* Baseline first so the selected route draws on top of it. Routes
            are told apart by pattern (dashed vs solid), not color alone.
            Each route is two Polylines on the same coords: a wide translucent
            "casing" underneath plus the crisp line on top — the cheap way to
            fake the Greenhouse glow (SVG strokes can't blur). Colour comes
            from the className (MapView.module.css, on the theme tokens);
            pathOptions carries only geometry. */}
        {baseline && (
          <>
            <Polyline
              positions={toLatLngs(baseline)}
              pathOptions={{ className: styles.routeBaseline, weight: 9, opacity: 0.15 }}
            />
            <Polyline
              positions={toLatLngs(baseline)}
              pathOptions={{ className: styles.routeBaseline, weight: 3, dashArray: '6 8', opacity: 0.85 }}
            />
          </>
        )}
        {selected && (
          <>
            <Polyline
              positions={toLatLngs(selected)}
              pathOptions={{ className: styles.routeSelected, weight: 11, opacity: 0.2 }}
            />
            <Polyline
              positions={toLatLngs(selected)}
              pathOptions={{ className: styles.routeSelected, weight: 4.5, opacity: 0.95 }}
            />
          </>
        )}

        {/* interactive/keyboard off: the markers are visual decoration (the
            same info is in the panel as text). Without this, Leaflet renders
            them as focusable role="button" divs with no accessible name —
            nameless buttons that do nothing, a double a11y fault. */}
        {start && (
          <Marker position={[start.lat, start.lon]} icon={startIcon} interactive={false} keyboard={false} />
        )}
        {end && (
          <Marker position={[end.lat, end.lon]} icon={endIcon} interactive={false} keyboard={false} />
        )}

        {/* The blue dot + its GPS-accuracy halo. */}
        {position && (
          <>
            <Circle
              center={[position.lat, position.lon]}
              radius={position.accuracy}
              pathOptions={{ className: styles.locationHalo, weight: 1, opacity: 0.4, fillOpacity: 0.08 }}
            />
            <CircleMarker
              center={[position.lat, position.lon]}
              radius={7}
              pathOptions={{ className: styles.locationDot, weight: 2, fillOpacity: 1 }}
            />
          </>
        )}

        <LocateButton position={position} />
        <MapA11y describedBy={helpId} />
      </MapContainer>
      {/* Purely decorative texture; aria-hidden keeps it out of the
          accessibility tree entirely. */}
      <div className={styles.scanlines} aria-hidden="true" />
      <Legend hasRoute={Boolean(selected || baseline)} />
      {/* Mobile full-screen toggle, top-right — the one free corner
          (Locate me bottom-right, zoom top-left, legend bottom-left).
          A toggle button, so the accessible name stays "Expand map" in
          both states and aria-pressed carries which one it's in. */}
      {onToggleExpanded && (
        <button
          type="button"
          className={styles.expandButton}
          aria-label="Expand map"
          aria-pressed={expanded}
          onClick={onToggleExpanded}
        >
          {expanded ? <CollapseIcon /> : <ExpandIcon />}
        </button>
      )}
    </div>
  )
}
