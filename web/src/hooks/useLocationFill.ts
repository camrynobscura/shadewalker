import { useEffect, useRef, useState } from 'react'
import { useGeolocation, type GeoPosition } from './useGeolocation'

/* One ⌖ tap = one gated fill (user calls, 2026-09-02). The first fix
   watchPosition delivers is often cell-tower-grade — the field test froze
   a start point blocks away while the live blue dot (which keeps
   updating) was right — so a tap ARMS a fill rather than grabbing
   whatever fix exists at that instant. The fill fires on the first fix
   accurate to ACCURACY_GATE_M; if none arrives within SETTLE_TIMEOUT_MS,
   the best fix so far is good enough (indoors, bad reception — waiting
   forever helps no one). After filling, the start NEVER chases later,
   better fixes: the live use case is someone walking the route with the
   page open, and a start point that follows the walker redraws the route
   under them. Re-tapping ⌖ is the deliberate way to retarget. */
const ACCURACY_GATE_M = 50
const SETTLE_TIMEOUT_MS = 10_000

export type LocationFillStatus =
  /** Never asked — the tap that changes this also triggers the browser's
   * permission prompt, so it must come from a user gesture. */
  | 'idle'
  /** Waiting: for permission, for a first fix, or for one that passes the
   * accuracy gate. */
  | 'acquiring'
  /** Live fix in hand, no fill pending — a tap now re-arms. */
  | 'ready'
  | 'denied'
  | 'unavailable'

export interface LocationFill {
  status: LocationFillStatus
  /** The live fix, for the map's blue dot — updates continuously,
   * independent of any armed fill. */
  position: GeoPosition | null
  /** Every ⌖ tap: enables the watch on first use, and (re-)arms a gated
   * fill. Safe to call in any status — after an error it's the retry. */
  request: () => void
}

export function useLocationFill(onFill: (p: GeoPosition) => void): LocationFill {
  const [enabled, setEnabled] = useState(false)
  const [armed, setArmed] = useState(false)
  const { position, error } = useGeolocation(enabled)

  // Refs, not state: consumed inside effects, never rendered.
  const armedAtRef = useRef(0)
  const bestFixRef = useRef<GeoPosition | null>(null)
  const onFillRef = useRef(onFill)
  onFillRef.current = onFill

  function request() {
    setEnabled(true)
    armedAtRef.current = Date.now()
    bestFixRef.current = null
    setArmed(true)
  }

  // The gate: every fix while armed is a candidate. Runs synchronously on
  // arming too (request() re-renders with `armed` true and the current
  // position already in hand), which is what makes a re-tap with a warm,
  // accurate fix fill instantly.
  useEffect(() => {
    if (!armed || !position) return
    const best = bestFixRef.current
    if (!best || position.accuracy < best.accuracy) bestFixRef.current = position
    if (position.accuracy <= ACCURACY_GATE_M) {
      // A passing fix fills with ITSELF, not best-so-far: both clear the
      // bar, and the fresh one describes where the phone is now.
      setArmed(false)
      onFillRef.current(position)
    } else if (Date.now() - armedAtRef.current >= SETTLE_TIMEOUT_MS) {
      setArmed(false)
      onFillRef.current(bestFixRef.current ?? position) // ?? for the types; best was just set above
    }
  }, [armed, position])

  // The settle timeout: fires even if no NEW fix arrives after arming
  // (the gate effect above only runs when one does). No fix at all by the
  // deadline → stay armed, and the gate's own deadline branch fills from
  // whatever arrives first.
  useEffect(() => {
    if (!armed) return
    const timer = setTimeout(() => {
      if (bestFixRef.current) {
        setArmed(false)
        onFillRef.current(bestFixRef.current)
      }
    }, SETTLE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [armed])

  // A failure while a fill is pending ends the wait — the error status is
  // the answer the tap gets, not an eternal "acquiring".
  useEffect(() => {
    if (error) setArmed(false)
  }, [error])

  const status: LocationFillStatus = !enabled
    ? 'idle'
    : error
      ? error
      : armed || !position
        ? 'acquiring'
        : 'ready'

  return { status, position, request }
}
