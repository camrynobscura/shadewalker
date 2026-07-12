import { useEffect, useState } from 'react'

export interface GeoPosition {
  lat: number
  lon: number
  /** GPS uncertainty radius in meters — drawn as the halo around the dot. */
  accuracy: number
}

/** Subscribe to the browser's live position. Returns null until the first
 * fix, and null forever if permission is denied or unsupported — callers
 * must handle the null case (the app works fully without location).
 *
 * The classic subscribe/unsubscribe useEffect pair: watchPosition on mount,
 * clearWatch in the cleanup on unmount so the GPS isn't left running.
 */
export function useGeolocation(): GeoPosition | null {
  const [position, setPosition] = useState<GeoPosition | null>(null)

  useEffect(() => {
    if (!('geolocation' in navigator)) return

    const watchId = navigator.geolocation.watchPosition(
      (pos) =>
        setPosition({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        }),
      () => setPosition(null), // denied / unavailable → no dot, app unaffected
      { enableHighAccuracy: true },
    )
    return () => navigator.geolocation.clearWatch(watchId)
  }, [])

  return position
}
