import { divIcon } from 'leaflet'
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

/* Markers as labeled divIcons: start and end differ by letter AND shape AND
   color (circle vs square), so color-blind users aren't relying on hue alone
   (WCAG 1.4.1 "use of color"). */
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

/** A ring well outside the coverage polygon in every direction. Paired with
 * the coverage ring as a Polygon's two rings (outer boundary + hole),
 * Leaflet fills only the area *between* them — everything outside coverage
 * gets dimmed, the coverage area itself stays a clear "hole". Sized off the
 * coverage ring itself rather than a hardcoded NYC box, so this keeps
 * working unchanged once Stage 2 covers more than one tile. */
function maskRing(coverageRing: [number, number][]): [number, number][] {
  const lons = coverageRing.map(([lon]) => lon)
  const lats = coverageRing.map(([, lat]) => lat)
  const margin = 0.5 // degrees (~55 km) — comfortably beyond any zoom-out
  // a user would realistically reach in an NYC-scoped app, and bigger than
  // Stage 2's eventual citywide coverage ring too
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

/** Dims everything outside the routable area, plus a dashed line marking
 * its edge. Two separate Polygons rather than one: the dimming needs a
 * hole (fill between two rings, coverage area excluded); the edge needs
 * its own stroke-only pass so the boundary itself reads crisply on top. */
function CoverageOverlay({ coverage }: { coverage: CoverageFeature }) {
  const ring = coverage.geometry.coordinates[0]
  const outer = maskRing(ring)
  const toLatLng = ([lon, lat]: [number, number]): [number, number] => [lat, lon]

  return (
    <>
      <Polygon
        // The hole ring has to wind opposite the outer ring — Leaflet's SVG
        // paths use the default (nonzero) fill-rule, which fills straight
        // through a same-direction inner ring instead of punching a hole.
        // Reversing point order flips winding without changing the shape.
        positions={[outer.map(toLatLng), [...ring].reverse().map(toLatLng)]}
        pathOptions={{ stroke: false, fillColor: '#0b2418', fillOpacity: 0.16 }}
        interactive={false}
      />
      <Polygon
        // Same weight/dash/opacity as the fastest route's pink line below —
        // green instead, so it reads as "a line like that one" rather than
        // an unrelated new style.
        positions={[ring.map(toLatLng)]}
        pathOptions={{ color: '#00a86b', weight: 3, dashArray: '6 8', opacity: 0.85, fill: false }}
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
  green: RouteFeature | null
  shortest: RouteFeature | null
  coverage: CoverageFeature | null
  position: GeoPosition | null
  onMapClick: (p: Point) => void
}

export function MapView({ start, end, green, shortest, coverage, position, onMapClick }: MapViewProps) {
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
          url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
          subdomains={['a', 'b']} /* only the two hosts we preconnect in index.html */
          // detectRetina is what actually makes Leaflet fill the URL's {r}
          // token with @2x -- without it, {r} always resolves to empty and
          // every display gets the same base-resolution tile regardless of
          // its real pixel density, softer than it needs to be on any
          // physically high-DPI screen (most modern laptops/phones).
          detectRetina
        />
        <ClickHandler onMapClick={onMapClick} />

        {/* Drawn first (and non-interactive) so the route lines and markers
            always sit visually on top of it, never the other way round. */}
        {coverage && <CoverageOverlay coverage={coverage} />}

        {/* Shortest first so the green route draws on top of it. Routes are
            told apart by pattern (dashed vs solid), not color alone.
            Each route is two Polylines on the same coords: a wide translucent
            "casing" underneath plus the crisp line on top — the cheap way to
            fake the Greenhouse glow (SVG strokes can't blur). */}
        {shortest && (
          <>
            <Polyline
              positions={toLatLngs(shortest)}
              pathOptions={{ color: '#ff2bd6', weight: 9, opacity: 0.15 }}
            />
            <Polyline
              positions={toLatLngs(shortest)}
              pathOptions={{ color: '#ff2bd6', weight: 3, dashArray: '6 8', opacity: 0.85 }}
            />
          </>
        )}
        {green && (
          <>
            <Polyline
              positions={toLatLngs(green)}
              pathOptions={{ color: '#00a86b', weight: 11, opacity: 0.2 }}
            />
            <Polyline
              positions={toLatLngs(green)}
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
      <Legend hasRoute={Boolean(green || shortest)} hasCoverage={Boolean(coverage)} />
    </div>
  )
}
