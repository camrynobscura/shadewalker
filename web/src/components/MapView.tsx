import { Control, DomEvent, divIcon, type Map as LeafletMap } from 'leaflet'
import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
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
import { MOBILE_LAYOUT_QUERY } from '../hooks/useMediaQuery'
import { CollapseIcon, CrosshairIcon, ExpandIcon } from './icons'
import styles from './MapView.module.css'

// Where the map opens before any route exists: Barclays Center up to
// Washington Square Park, so the East River, its bridges and both shores
// are on screen and the view reads as a place in the city (a close-up of
// street grid alone could be anywhere). Two corners rather than a centre
// and a zoom, so the same area fits a phone's short map and a wide desktop
// one. It still holds the Village, where shade varies enough from block to
// block that a first route's presets visibly differ.
const LANDING_BOUNDS: [[number, number], [number, number]] = [
  [40.6826, -73.9973],
  [40.7308, -73.9754],
]
// A link with one point opens on it, close enough to read its block.
const LONE_POINT_ZOOM = 15

/* CARTO watermarks keyless raster tile requests ("API KEY REQUIRED"
   repeated across the map). The key is a build-time
   input (VITE_CARTO_KEY in web/.env.local, gitignored) and is public by
   nature — it rides in every tile URL a visitor's browser requests, so
   keeping it out of git is rotation hygiene, not secrecy. Keyless builds
   still render, just watermarked, which keeps dev and e2e working with
   no local setup. Key mechanics: docs.carto.com/faqs/carto-basemaps. */
const CARTO_KEY = import.meta.env.VITE_CARTO_KEY
const TILE_URL =
  'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png' + (CARTO_KEY ? `?key=${CARTO_KEY}` : '')

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

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

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
   as "already visible". Uneven padding, not a uniform one: the zoom
   buttons (top-left, 48x92px ending 58px in and 102px down), the legend
   (bottom-left, 146x57px ending 67px up), the map credits and Locate-me
   (bottom-right), and on mobile the expand toggle (top-right, 44px ending
   54px from each edge) all float over the map itself, so a plain 48px on
   every side still let a fitted point land right behind one of them.
   The bottom's 100px keeps every fitted point out of the whole bottom
   strip, so it covers the legend in either corner; the sides' 60px clear
   the zoom buttons and the toggle.

   Phones take less top and bottom: their map is 40% of the screen, and
   the desktop padding would leave an iPhone SE's 220px map 60px for the
   pair (a Brooklyn-Manhattan trip framed at zoom 9, its markers 37px
   apart). The sides already clear both top controls, so the top keeps
   only half a marker (15px) and a little; the bottom clears the legend's
   67px and half a marker. */
const FIT_PAD_TOP_LEFT: [number, number] = [60, 60]
const FIT_PAD_BOTTOM_RIGHT: [number, number] = [60, 100]
const PHONE_FIT_PAD_TOP_LEFT: [number, number] = [60, 20]
const PHONE_FIT_PAD_BOTTOM_RIGHT: [number, number] = [60, 85]

/** The padded rectangle for the layout on screen right now. */
function fitPadding(): { topLeft: [number, number]; bottomRight: [number, number] } {
  return window.matchMedia(MOBILE_LAYOUT_QUERY).matches
    ? { topLeft: PHONE_FIT_PAD_TOP_LEFT, bottomRight: PHONE_FIT_PAD_BOTTOM_RIGHT }
    : { topLeft: FIT_PAD_TOP_LEFT, bottomRight: FIT_PAD_BOTTOM_RIGHT }
}

/** Whether every point already sits inside the current view's padded
 * rectangle -- checked in screen pixels so the padding means exactly
 * what it means to fitBounds. */
function fullyVisible(map: LeafletMap, points: [number, number][]): boolean {
  const size = map.getSize()
  const { topLeft, bottomRight } = fitPadding()
  return points.every((point) => {
    const px = map.latLngToContainerPoint(point)
    return (
      px.x >= topLeft[0] &&
      px.y >= topLeft[1] &&
      px.x <= size.x - bottomRight[0] &&
      px.y <= size.y - bottomRight[1]
    )
  })
}

/** Keeps the whole route in view as start/end/the selected preset change --
 * without this, the map's viewport never moves on its own (LANDING_BOUNDS
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
  // The endpoint pair the map last framed, compared by value: the same
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
    // camera. Refitting anyway nudges the map sideways on every
    // Shade_priority flip (each preset's bounds differ slightly), which
    // reads as jitter when flipping through routes to compare them. A
    // route that escapes the current view -- e.g. switching to MAX while
    // zoomed in on a MED detail -- still falls through to the fit, pan
    // and zoom both.
    if (pair === framedPairRef.current && fullyVisible(map, points)) return
    const { topLeft, bottomRight } = fitPadding()
    map.fitBounds(points, {
      paddingTopLeft: topLeft,
      paddingBottomRight: bottomRight,
      maxZoom: 17,
      animate: !reducedMotion(),
    })
    framedPairRef.current = pair
    // Deliberately no handling for "only one of start/end set": panning
    // the instant point A lands is more disruptive than useful -- it
    // re-centers/zooms the view around a point the user likely just
    // clicked while already looking straight at it. Wait for the pair to
    // frame together instead. A lone point typed or picked in a field is
    // different -- usually somewhere else -- and is RevealLonePoint's.
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
    // clue what the list is.
    <ul className={styles.legend} aria-label="Map legend">
      <li className={styles.legendRow}>
        {/* The picked preset's route, whichever it is: "shadiest" would be
          wrong for MED, LOW and NONE. */}
        <span className={styles.legendSwatch} aria-hidden="true" />
        your route
      </li>
      <li className={styles.legendRow}>
        <span className={`${styles.legendSwatch} ${styles.legendSwatchDashed}`} aria-hidden="true" />
        fastest route
      </li>
    </ul>
  )
}

/** Pans to `point` whenever a new object arrives — the imperative "look
 * here" channel, used when a location fix fills the start field so the
 * map visibly answers the tap (RouteFraming deliberately ignores single
 * points, so without this a far-from-viewport fix would change nothing
 * on screen). Object identity is the trigger on purpose: re-requesting
 * location pans again even when the fix lands on the same coordinates. */
function PanTo({ point }: { point: Point | null }) {
  const map = useMap()
  useEffect(() => {
    if (point) map.setView([point.lat, point.lon], 16, { animate: !reducedMotion() })
  }, [point, map])
  return null
}

/** The one endpoint that's set while the other isn't, or null. */
function lonePoint(start: Point | null, end: Point | null): Point | null {
  if (start && !end) return start
  if (end && !start) return end
  return null
}

/** Pans to an address typed or picked for A or B while the other point is
 * still empty, keeping the zoom, when it's outside the padded view:
 * otherwise the marker lands off-screen and a pick looks like it did
 * nothing. Map taps never come through here -- a tap lands
 * where the user is already looking (RouteFraming's note). `point` is a
 * fresh object per entry, handled once, against the endpoints of the
 * render it arrived in: if the other field filled meanwhile, the pair is
 * RouteFraming's and nothing moves here. */
function RevealLonePoint({
  point,
  start,
  end,
}: {
  point: Point | null
  start: Point | null
  end: Point | null
}) {
  const map = useMap()
  const handledRef = useRef<Point | null>(null)
  useEffect(() => {
    if (!point || point === handledRef.current) return
    handledRef.current = point
    const lone = lonePoint(start, end)
    if (!lone || lone.lat !== point.lat || lone.lon !== point.lon) return
    if (fullyVisible(map, [[lone.lat, lone.lon]])) return
    // No options: Leaflet animates a pan shorter than the map and jumps a
    // longer one (Map.js _tryAnimatedPan), so a far address doesn't swoop.
    map.panTo([lone.lat, lone.lon], reducedMotion() ? { animate: false } : undefined)
  }, [point, start, end, map])
  return null
}

/** Two things Leaflet's own DOM needs that react-leaflet can't set from
 * props. The container is a keyboard tab stop (arrow
 * keys pan, +/- zoom) that announced as nothing but its contents; it
 * gets a role and a short name — a group, not a landmark, so it's read
 * on focus and never in the landmark list — and `describedBy` points at
 * MapView's hidden instructions, so the keys are announced once on
 * focus rather than repeated inside the name (a name is read every
 * time; a description once). And the vector overlay's <svg> — the route
 * lines — is hidden from AT: it read as a nameless image, and the
 * directions list is the accessible route. The svg exists only once the
 * first path is drawn, so this re-checks on every render (one
 * querySelector); it must stay the last child of MapContainer so its
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

/** "Locate me" — a Leaflet control in the bottom-right corner, the React
 * button portaled into the control's container. A control, not a plain
 * button inside the map, for disableClickPropagation, as Leaflet's own
 * controls have: without it a tap on the button also reaches the map as
 * a click and drops a route point under it (e2e location.spec pins
 * it). */
function LocateButton({ position }: { position: GeoPosition | null }) {
  const map = useMap()
  const [container] = useState(() => {
    const div = document.createElement('div')
    DomEvent.disableClickPropagation(div)
    return div
  })
  const visible = position !== null
  useEffect(() => {
    if (!visible) return
    const control = new Control({ position: 'bottomright' })
    control.onAdd = () => container
    control.addTo(map)
    return () => {
      control.remove()
    }
  }, [map, container, visible])
  if (!position) return null
  return createPortal(
    <button
      type="button"
      className={styles.locateButton}
      onClick={() => map.setView([position.lat, position.lon], 16, { animate: !reducedMotion() })}
    >
      {/* Icon is decoration (aria-hidden inside the component); the text
          after it is the accessible name. SVG, not the ⌖ character — that
          glyph renders as tofu in iOS's mono fallback chain, and it's the
          same mark as the start field's location accessory. */}
      <CrosshairIcon /> Locate me
    </button>,
    container,
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
  /** The latest address typed or picked in a field — see RevealLonePoint. */
  reveal: Point | null
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
  reveal,
  onMapClick,
  expanded,
  onToggleExpanded,
}: MapViewProps) {
  const helpId = useId()
  // A link with only A (or only B) opens on that point, not the landing
  // view, so its marker is on screen from the start. Read once:
  // MapContainer applies its opening view at mount only.
  const [openAt] = useState(() => lonePoint(start, end))
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
        Click to set your start and end points; arrow keys pan and plus and minus keys zoom. You can also type
        addresses in the route controls.
      </p>
      {/* zoomAnimation must be off under reduced motion, not just quick:
          base.css's prefers-reduced-motion rule nulls every CSS
          transition, and Leaflet's animated zoom waits on a transitionend
          event to leave its "animating" state -- an event a nulled
          transition may never fire. Stuck there, Leaflet silently ignores
          every later setView/fitBounds: one click of the +/- control
          could freeze route framing, PanTo and Locate-me for the rest of
          the session. Instant zoom is also simply what the preference
          asks for. Mount-time read by design: react-leaflet map options
          are immutable, and a mid-session OS toggle is rare enough to
          not chase. */}
      <MapContainer
        {...(openAt
          ? { center: [openAt.lat, openAt.lon] as [number, number], zoom: LONE_POINT_ZOOM }
          : { bounds: LANDING_BOUNDS })}
        zoomAnimation={!reducedMotion()}
        className={styles.map}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url={TILE_URL}
          subdomains={['a', 'b']} /* only the two hosts we preconnect in index.html */
          // detectRetina stays off. With it on, Leaflet asks for tiles one
          // zoom level above the map's own, and that left blank grey tiles
          // when zoomed in (measured 2026-08-24).
          //
          // High-density screens still get sharp tiles: Leaflet turns the
          // URL's {r} token into @2x on its own, at the map's zoom. Those
          // tiles are about three times the bytes of plain ones, a cost
          // accepted so that which side of a street a route uses stays
          // legible.
          //
          // maxNativeZoom caps what is requested while maxZoom caps what the
          // map allows, so past z19 Leaflet upscales the last real tile
          // instead of requesting one that may not exist.
          maxNativeZoom={19}
          maxZoom={20}
        />
        <ClickHandler onMapClick={onMapClick} />
        <InvalidateOnResize />
        <PanTo point={panTo} />
        <RevealLonePoint point={reveal} start={start} end={end} />
        <RouteFraming start={start} end={end} selected={selected} baseline={baseline} />

        {/* Baseline first so the selected route draws on top of it. Routes
            are told apart by pattern (dashed vs solid), not color alone.
            Each route is two Polylines on the same coords: a wide translucent
            "casing" underneath plus the crisp line on top — the cheap way to
            fake the Greenhouse glow (SVG strokes can't blur). Colour comes
            from the className (MapView.module.css, on the theme tokens);
            pathOptions carries only geometry.
            className must be a top-level prop, never inside pathOptions:
            top-level props reach Leaflet's constructor, so the class is on
            the SVG element when it's created; pathOptions goes through
            setStyle() after the layer is added, which never touches the
            class. StrictMode's double mount hides that in dev, so
            production is where unstyled routes show. */}
        {baseline && (
          <>
            <Polyline
              positions={toLatLngs(baseline)}
              className={styles.routeBaseline}
              pathOptions={{ weight: 9, opacity: 0.15 }}
            />
            <Polyline
              positions={toLatLngs(baseline)}
              className={styles.routeBaseline}
              pathOptions={{ weight: 3, dashArray: '6 8', opacity: 0.85 }}
            />
          </>
        )}
        {selected && (
          <>
            <Polyline
              positions={toLatLngs(selected)}
              className={styles.routeSelected}
              pathOptions={{ weight: 11, opacity: 0.2 }}
            />
            <Polyline
              positions={toLatLngs(selected)}
              className={styles.routeSelected}
              pathOptions={{ weight: 4.5, opacity: 0.95 }}
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
        {end && <Marker position={[end.lat, end.lon]} icon={endIcon} interactive={false} keyboard={false} />}

        {/* The blue dot + its GPS-accuracy halo. */}
        {position && (
          <>
            <Circle
              center={[position.lat, position.lon]}
              radius={position.accuracy}
              className={styles.locationHalo}
              pathOptions={{ weight: 1, opacity: 0.4, fillOpacity: 0.08 }}
            />
            <CircleMarker
              center={[position.lat, position.lon]}
              radius={7}
              className={styles.locationDot}
              pathOptions={{ weight: 2, fillOpacity: 1 }}
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
