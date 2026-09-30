import { useEffect, useState } from 'react'
import { suggest, type GeocodeResult } from '../api'

/** Debounce: long enough that a steady typist fires one request per
 * pause, not per keystroke — the client half of being polite to the
 * geocoding upstream (the server's cache is the other half). */
export const SUGGEST_DEBOUNCE_MS = 250
/** Below this many characters a query matches half the city — the
 * results would be noise and the requests pure waste. */
export const SUGGEST_MIN_CHARS = 3

/** Address suggestions for one combobox: debounced, aborted on every
 * newer keystroke (same abort-on-supersede idiom as useRouteQuery, so a
 * slow stale response can never paint over a fresh one), and empty
 * whenever `enabled` is false — the field owns that flag, since only it
 * knows whether the current text was TYPED (suggest) or arrived
 * programmatically from a suggestion pick, a map click, or a
 * reverse-geocode fill (don't re-suggest text we generated ourselves). */
export function useGeocodeSuggestions(query: string, enabled: boolean): GeocodeResult[] {
  const [suggestions, setSuggestions] = useState<GeocodeResult[]>([])
  const trimmed = query.trim()
  const active = enabled && trimmed.length >= SUGGEST_MIN_CHARS

  // Switching off empties the list while rendering (React's "adjusting
  // state when a prop changes"), not in an effect a beat later, so the old
  // list can't show for a render — or come back when suggesting resumes.
  const [wasActive, setWasActive] = useState(active)
  if (active !== wasActive) {
    setWasActive(active)
    if (!active) setSuggestions([])
  }

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      suggest(trimmed, controller.signal)
        .then(setSuggestions)
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return // superseded, not an error
          setSuggestions([]) // a dead network means no dropdown, never a stale one
        })
    }, SUGGEST_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [active, trimmed])

  return suggestions
}
