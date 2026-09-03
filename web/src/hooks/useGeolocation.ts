import { useEffect, useState } from 'react'

export interface GeoPosition {
  lat: number
  lon: number
  /** GPS uncertainty radius in meters — drawn as the halo around the dot. */
  accuracy: number
}

/** Why there's no position: permission refused vs everything else (no
 * signal, a timeout, no browser support). Two buckets, not the raw
 * GeolocationPositionError, because the UI only has two answers — "go
 * flip the browser setting" vs "try again". */
export type GeoError = 'denied' | 'unavailable'

export interface GeolocationState {
  position: GeoPosition | null
  error: GeoError | null
}

/** Subscribe to the browser's live position — but only once `enabled` is
 * true. Location is opt-in (a button click), never requested on page load:
 * ambushing first-time visitors with a permission prompt is both hostile UX
 * and a Lighthouse best-practices failure.
 *
 * `position` is null until the first fix; `error` says why when a failure
 * is the reason (until 2026-09-02 errors were swallowed into the same
 * null, which left the ⌖ accessory stuck on "acquiring" forever after a
 * denial — `use-location-ux`'s founding bug). A later successful fix
 * clears the error: watchPosition keeps trying, and e.g. a timeout
 * followed by a real fix is a recovery, not a failure. The app works
 * fully without location either way. */
export function useGeolocation(enabled: boolean): GeolocationState {
  const [position, setPosition] = useState<GeoPosition | null>(null)
  const [error, setError] = useState<GeoError | null>(null)

  useEffect(() => {
    if (!enabled) return
    // Truthiness, not `'geolocation' in navigator`: some environments
    // expose the property as undefined, which `in` calls supported.
    if (!navigator.geolocation) {
      setError('unavailable')
      return
    }

    // The classic subscribe/unsubscribe pair: watchPosition when enabled
    // flips on, clearWatch in the cleanup so the GPS isn't left running.
    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        setError(null)
        setPosition({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        })
      },
      (err) => {
        // code 1 is PERMISSION_DENIED; 2 (unavailable) and 3 (timeout)
        // both mean "no fix right now" as far as the UI cares.
        setError(err?.code === 1 ? 'denied' : 'unavailable')
        setPosition(null)
      },
      { enableHighAccuracy: true },
    )
    return () => navigator.geolocation.clearWatch(watchId)
  }, [enabled])

  return { position, error }
}
