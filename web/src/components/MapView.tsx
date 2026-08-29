import { divIcon } from 'leaflet'
import { useEffect } from 'react'
import {
  Circle,
  CircleMarker,
  MapContainer,
  Marker,
  Polygon,
  Polyline,
  TileLayer,
  useMap,
  useMapEvents,
} from 'react-leaflet'
import type { CoverageFeature, Point, RouteFeature } from '../api'
import type { GeoPosition } from '../hooks/useGeolocation'
import styles from './MapView.module.css'

const PILOT_CENTER: [number, number] = [40.677, -73.993]

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

/** A ring well outside every coverage piece in every direction. Paired
 * with the coverage rings as a Polygon's outer boundary + holes, Leaflet
 * fills only the area *between* them — everything outside coverage gets
 * dimmed, each coverage piece stays a clear "hole". Sized off every
 * piece's combined bounds rather than a hardcoded NYC box, so this keeps
 * working unchanged as more pieces (Governors Island, Staten Island) get
 * added. */
function maskRing(coverageRings: [number, number][][]): [number, number][] {
  const points = coverageRings.flat()
  const lons = points.map(([lon]) => lon)
  const lats = points.map(([, lat]) => lat)
  const margin = 0.5 // degrees (~55 km) — comfortably beyond any zoom-out
  // a user would realistically reach in an NYC-scoped app, and bigger than
  // Stage 2's eventual citywide coverage extent too
  const lonMin = Math.min(...lons) - margin
  const lonMax = Math.max(...lons) + margin
  const latMin = Math.min(...lats) - margin
  const latMax = Math.max(...lats) + margin
  return [
    [lonMin, latMin],
    [lonMax, latMin],
    [lonMax, latMax],
    [lonMin, latMax],
    [lonMin, latMin],
  ]
}

/** The offshore frame (FIXES 12, redesigned with the user 2026-08-18):
 * ONE generous dashed boundary drawn through the WATER around the whole
 * routable world — never tracing coastlines — with the outside dim
 * feathering in over two steps (~800m) instead of starting hard at the
 * line. The frame geometry comes from the server
 * (server/coverage_frame.py: coverage buffered offshore, closed across
 * the harbor, clipped to political geometry incl. three user-designed
 * corridors); the true click-acceptance region stays
 * coverage.geometry, which the server checks — the frame is
 * deliberately more generous, and the water inside it isn't clickable
 * anyway.
 *
 * Three stacked dim masks make the feather: each dims a little more the
 * further outside the frame you are (0.06 + 0.05 + 0.05 = the old 0.16
 * full strength past ~800m). The line itself is soft on purpose —
 * lighter and thinner than any route line, so the map's subject stays
 * the route (the user's design review picked soft over the old 3px). */
function CoverageOverlay({ coverage }: { coverage: CoverageFeature }) {
  const { frame, frame_feather_350, frame_feather_800 } = coverage.properties
  const outer = maskRing(frame_feather_800.length ? frame_feather_800 : frame)
  const toLatLng = ([lon, lat]: [number, number]): [number, number] => [lat, lon]

  // Hole rings have to wind opposite the outer ring — Leaflet's SVG
  // paths use the default (nonzero) fill-rule, which fills straight
  // through a same-direction inner ring instead of punching a hole.
  // Reversing point order flips winding without changing the shape.
  const dim = (holes: [number, number][][], opacity: number) => (
    <Polygon
      positions={[outer.map(toLatLng), ...holes.map((ring) => [...ring].reverse().map(toLatLng))]}
      pathOptions={{ stroke: false, fillColor: '#0b2418', fillOpacity: opacity }}
      interactive={false}
    />
  )

  return (
    <>
      {dim(frame, 0.06)}
      {dim(frame_feather_350, 0.05)}
      {dim(frame_feather_800, 0.05)}
      <Polygon
        // Dashed like the fastest route's pink line but green, thinner,
        // and quieter — reads as "a line in that family" without
        // competing with the routes. Array-of-arrays: each frame piece
        // (there's normally exactly one) is its own closed shape.
        positions={frame.map((ring) => [ring.map(toLatLng)])}
        pathOptions={{ color: '#00a86b', weight: 1.5, dashArray: '6 8', opacity: 0.45, fill: false }}
        interactive={false}
      />
    </>
  )
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

/** Keeps the whole route in view as start/end/the selected preset change --
 * without this, the map's viewport never moves on its own (PILOT_CENTER is
 * only ever applied once, at mount), so with all 5 boroughs live, an
 * address search or click outside whatever's currently on screen would
 * compute and draw a real route the user can't actually see without
 * manually panning to find it. Reframes on every preset switch too, not
 * just the initial start/end pick, since a higher Shade_priority detour can
 * extend well past the bounds a lower one fit. */
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

  useEffect(() => {
    const animate = !reducedMotion()
    if (start && end) {
      const route = selected ?? baseline
      const points: [number, number][] = [
        [start.lat, start.lon],
        [end.lat, end.lon],
        ...(route ? toLatLngs(route) : []),
      ]
      // Uneven padding, not a uniform one: the legend (bottom-left, up to
      // ~163x88px with all 3 rows shown) and the Locate-me button
      // (bottom-right) both float over the map itself, so a plain 48px on
      // every side still let a fitted point land right behind one of them.
      // paddingTopLeft's x covers the legend's width; paddingBottomRight's
      // y covers whichever of the two overlays is taller -- that alone
      // keeps every fitted point out of the bottom strip entirely, so it
      // doesn't matter which corner it's actually closer to.
      map.fitBounds(points, {
        paddingTopLeft: [190, 48],
        paddingBottomRight: [48, 100],
        maxZoom: 17,
        animate,
      })
    }
    // Deliberately no else-branch for "only one of start/end set": panning
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
 * not decoration. Route rows only once a route exists; the coverage row
 * as soon as the boundary itself has loaded, independent of that — it's
 * meant to help *before* someone tries a route, not just explain one after. */
function Legend({ hasRoute, hasCoverage }: { hasRoute: boolean; hasCoverage: boolean }) {
  if (!hasRoute && !hasCoverage) return null
  return (
    <ul className={styles.legend}>
      {hasRoute && (
        <>
          <li className={styles.legendRow}>
            <span className={styles.legendSwatch} aria-hidden="true" />
            shadiest route
          </li>
          <li className={styles.legendRow}>
            <span className={`${styles.legendSwatch} ${styles.legendSwatchDashed}`} aria-hidden="true" />
            fastest route
          </li>
        </>
      )}
      {hasCoverage && (
        <li className={styles.legendRow}>
          <span className={`${styles.legendSwatch} ${styles.legendSwatchCoverage}`} aria-hidden="true" />
          coverage area
        </li>
      )}
    </ul>
  )
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
      {/* Same treatment as the legend swatches and A/B markers elsewhere in
          this file: the glyph is decoration, the real accessible name is
          the text after it. Without aria-hidden, some screen readers
          announce the Unicode character's own name before "Locate me". */}
      <span aria-hidden="true">⌖</span> Locate me
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
  coverage: CoverageFeature | null
  position: GeoPosition | null
  onMapClick: (p: Point) => void
}

export function MapView({ start, end, selected, baseline, coverage, position, onMapClick }: MapViewProps) {
  return (
    <div className={styles.mapRegion} role="region" aria-label="Map">
      {/* A landmark's aria-label is re-read every time a screen reader user
          navigates the landmark list, not just once -- a full instruction
          sentence there gets repetitive fast. Real usage instructions go
          here instead, visually hidden but still in the accessibility
          tree: read once, in normal order, the first time someone actually
          enters this region (e.g. via the landmarks list), not repeated on
          every subsequent landmark-list pass the way the label would be. */}
      <p className={styles.visuallyHidden}>
        Click to set your start and end points; you can also type addresses in the route controls.
      </p>
      <MapContainer center={PILOT_CENTER} zoom={15} className={styles.map}>
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
        <RouteFraming start={start} end={end} selected={selected} baseline={baseline} />

        {/* Drawn first (and non-interactive) so the route lines and markers
            always sit visually on top of it, never the other way round. */}
        {coverage && <CoverageOverlay coverage={coverage} />}

        {/* Baseline first so the selected route draws on top of it. Routes
            are told apart by pattern (dashed vs solid), not color alone.
            Each route is two Polylines on the same coords: a wide translucent
            "casing" underneath plus the crisp line on top — the cheap way to
            fake the Greenhouse glow (SVG strokes can't blur). */}
        {baseline && (
          <>
            <Polyline
              positions={toLatLngs(baseline)}
              pathOptions={{ color: '#ff2bd6', weight: 9, opacity: 0.15 }}
            />
            <Polyline
              positions={toLatLngs(baseline)}
              pathOptions={{ color: '#ff2bd6', weight: 3, dashArray: '6 8', opacity: 0.85 }}
            />
          </>
        )}
        {selected && (
          <>
            <Polyline
              positions={toLatLngs(selected)}
              pathOptions={{ color: '#00a86b', weight: 11, opacity: 0.2 }}
            />
            <Polyline
              positions={toLatLngs(selected)}
              pathOptions={{ color: '#00a86b', weight: 4.5, opacity: 0.95 }}
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
              pathOptions={{ color: '#0077a3', weight: 1, opacity: 0.4, fillOpacity: 0.08 }}
            />
            <CircleMarker
              center={[position.lat, position.lon]}
              radius={7}
              pathOptions={{ color: '#ffffff', weight: 2, fillColor: '#3388ff', fillOpacity: 1 }}
            />
          </>
        )}

        <LocateButton position={position} />
      </MapContainer>
      {/* Purely decorative texture; aria-hidden keeps it out of the
          accessibility tree entirely. */}
      <div className={styles.scanlines} aria-hidden="true" />
      <Legend hasRoute={Boolean(selected || baseline)} hasCoverage={Boolean(coverage)} />
    </div>
  )
}
