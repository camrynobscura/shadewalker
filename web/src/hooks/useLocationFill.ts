import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { useGeolocation, type GeoPosition } from './useGeolocation'

/* One ⌖ tap = one gated fill. The first fix watchPosition delivers is
   often cell-tower-grade — it can put a start point blocks away while
   the live blue dot (which keeps updating) is right — so a tap arms a
   fill rather than grabbing whatever fix exists at that instant. The
   fill fires on the first fix accurate to ACCURACY_GATE_M; if none
   arrives within SETTLE_TIMEOUT_MS, the best fix so far is good enough
   (indoors, bad reception — waiting forever helps no one). After
   filling, the start never chases later, better fixes: the live use
   case is someone walking the route with the
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
  // Whether a fill is pending, for rendering (the status, the settle
  // timer). The pending fill's bookkeeping is a ref: the handlers below
  // read and write it as each fix lands, and it's never rendered.
  const [armed, setArmed] = useState(false)
  const pendingRef = useRef<{ armedAt: number; best: GeoPosition | null } | null>(null)

  function fill(fix: GeoPosition) {
    pendingRef.current = null
    setArmed(false)
    onFill(fix)
  }

  // The gate: every fix while armed is a candidate, handled as it arrives.
  function consider(fix: GeoPosition) {
    const pending = pendingRef.current
    if (!pending) return
    const best = pending.best && pending.best.accuracy <= fix.accuracy ? pending.best : fix
    pending.best = best
    if (fix.accuracy <= ACCURACY_GATE_M) {
      // A passing fix fills with itself, not best-so-far: both clear the
      // bar, and the fresh one describes where the phone is now.
      fill(fix)
    } else if (Date.now() - pending.armedAt >= SETTLE_TIMEOUT_MS) {
      fill(best)
    }
  }

  // A failure while a fill is pending ends the wait — the error status is
  // the answer the tap gets, not an eternal "acquiring".
  function abandon() {
    pendingRef.current = null
    setArmed(false)
  }

  const { position, error } = useGeolocation(enabled, { onFix: consider, onError: abandon })

  function request() {
    setEnabled(true)
    pendingRef.current = { armedAt: Date.now(), best: null }
    setArmed(true)
    // The fix already in hand is the first candidate: a re-tap with a
    // warm, accurate fix fills at once.
    if (position) consider(position)
  }

  // The settle timeout: fires even if no new fix arrives after arming
  // (the gate only runs when one does). No fix at all by the deadline →
  // stay armed, and the gate's own deadline branch fills from whatever
  // arrives first.
  const settle = useEffectEvent(() => {
    const best = pendingRef.current?.best
    if (best) fill(best)
  })
  useEffect(() => {
    if (!armed) return
    const timer = setTimeout(() => settle(), SETTLE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [armed])

  const status: LocationFillStatus = !enabled
    ? 'idle'
    : error
      ? error
      : armed || !position
        ? 'acquiring'
        : 'ready'

  return { status, position, request }
}
