import { useEffect, useState } from 'react'

export interface GeoPosition {
  lat: number
  lon: number
  /** GPS uncertainty radius in meters — drawn as the halo around the dot. */
  accuracy: number
}

/** Subscribe to the browser's live position — but only once `enabled` is
 * true. Location is opt-in (a button click), never requested on page load:
 * ambushing first-time visitors with a permission prompt is both hostile UX
 * and a Lighthouse best-practices failure.
 *
 * Returns null until the first fix, and null forever if permission is denied
 * or unsupported — callers must handle the null case (the app works fully
 * without location).
 */
export function useGeolocation(enabled: boolean): GeoPosition | null {
  const [position, setPosition] = useState<GeoPosition | null>(null)

  useEffect(() => {
    if (!enabled || !('geolocation' in navigator)) return

    // The classic subscribe/unsubscribe pair: watchPosition when enabled
    // flips on, clearWatch in the cleanup so the GPS isn't left running.
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
  }, [enabled])

  return position
}
