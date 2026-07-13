import { divIcon } from 'leaflet'
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
      ⌖ Locate me
    </button>
  )
}

interface MapViewProps {
  start: Point | null
  end: Point | null
  green: RouteFeature | null
  shortest: RouteFeature | null
  position: GeoPosition | null
  onMapClick: (p: Point) => void
}

export function MapView({ start, end, green, shortest, position, onMapClick }: MapViewProps) {
  return (
    <div
      className={styles.mapRegion}
      role="region"
      aria-label="Map. Click to set your start and end points; you can also type addresses in the route controls."
    >
      <MapContainer center={PILOT_CENTER} zoom={15} className={styles.map}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
          subdomains={['a', 'b']} /* only the two hosts we preconnect in index.html */
        />
        <ClickHandler onMapClick={onMapClick} />

        {/* Shortest first so the green route draws on top of it. Routes are
            told apart by pattern (dashed vs solid), not color alone.
            Each route is two Polylines on the same coords: a wide translucent
            "casing" underneath plus the crisp line on top — the cheap way to
            fake the Greenhouse glow (SVG strokes can't blur). */}
        {shortest && (
          <>
            <Polyline
              positions={toLatLngs(shortest)}
              pathOptions={{ color: '#d61bb0', weight: 9, opacity: 0.15 }}
            />
            <Polyline
              positions={toLatLngs(shortest)}
              pathOptions={{ color: '#d61bb0', weight: 3, dashArray: '6 8', opacity: 0.85 }}
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
    </div>
  )
}
