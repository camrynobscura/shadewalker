import { useEffect, useRef, useState } from 'react'
import { suggest, type GeocodeResult } from '../api'

/** Debounce: long enough that a steady typist fires one request per
 * pause, not per keystroke — the client half of being polite to the
 * geocoding upstream (the server's cache is the other half). A 150 ms
 * pause measured no faster and sent about four times the lookups at a
 * moderate typing speed (2026-09-30). */
export const SUGGEST_DEBOUNCE_MS = 250
/** Below this many characters a query matches half the city — the
 * results would be noise and the requests pure waste. */
export const SUGGEST_MIN_CHARS = 3
/** Answers remembered per field, so text typed before (a backspace, a
 * retyped word) fills in without another request. */
const REMEMBERED_ANSWERS = 50

/** One stretch of suggesting: from the field switching on to it
 * switching off. Lookups are numbered in the order they were sent. */
interface Session {
  controller: AbortController
  sent: number
  /** The number of the lookup whose answer is on screen. */
  shown: number
}

/** Address suggestions for one combobox, and whether the list is still
 * catching up with the text.
 *
 * Debounced, but a newer keystroke does not cancel a lookup already
 * sent: the geocoder takes about a second, longer than the gap between
 * keystrokes, so cancelling would mean no answer ever arrives while
 * someone is typing. Each answer is shown as it lands unless a newer
 * one is already showing, so the list can trail the text but never goes
 * backwards. The server finishes a lookup whether or not the browser
 * still listens, so letting them land adds no load.
 *
 * Empty whenever `enabled` is false — the field owns that flag, since
 * only it knows whether the current text was typed (suggest) or arrived
 * programmatically from a suggestion pick, a map click, or a
 * reverse-geocode fill (don't re-suggest text we generated ourselves).
 * Switching off also drops every lookup still out. */
export function useGeocodeSuggestions(
  query: string,
  enabled: boolean,
): { suggestions: GeocodeResult[]; searching: boolean } {
  const [suggestions, setSuggestions] = useState<GeocodeResult[]>([])
  // The text whose answer (or failure) arrived last. While it differs
  // from the text in the field, the list is behind.
  const [settledFor, setSettledFor] = useState<string | null>(null)
  const trimmed = query.trim()
  const active = enabled && trimmed.length >= SUGGEST_MIN_CHARS

  // Switching off empties the list while rendering (React's "adjusting
  // state when a prop changes"), not in an effect a beat later, so the old
  // list can't show for a render — or come back when suggesting resumes.
  const [wasActive, setWasActive] = useState(active)
  if (active !== wasActive) {
    setWasActive(active)
    if (!active) {
      setSuggestions([])
      setSettledFor(null)
    }
  }

  const sessionRef = useRef<Session | null>(null)
  const rememberedRef = useRef(new Map<string, GeocodeResult[]>())

  // Declared before the lookup effect so the session exists when that
  // one first runs.
  useEffect(() => {
    if (!active) return
    const session: Session = { controller: new AbortController(), sent: 0, shown: 0 }
    sessionRef.current = session
    return () => {
      session.controller.abort()
      sessionRef.current = null
    }
  }, [active])

  useEffect(() => {
    if (!active) return
    const session = sessionRef.current
    if (!session) return
    const remembered = rememberedRef.current

    function show(seq: number, results: GeocodeResult[]) {
      if (session!.controller.signal.aborted || seq < session!.shown) return // switched off, or older than what's showing
      session!.shown = seq
      setSuggestions(results)
      setSettledFor(trimmed)
    }

    const known = remembered.get(trimmed)
    const timer = setTimeout(
      () => {
        const seq = ++session.sent
        if (known) {
          show(seq, known)
          return
        }
        suggest(trimmed, session.controller.signal)
          .then((results) => {
            // An empty answer is not remembered: suggest() also returns
            // one when the server refuses, and that must not stick.
            if (results.length > 0) {
              if (remembered.size >= REMEMBERED_ANSWERS) remembered.delete(remembered.keys().next().value!)
              remembered.set(trimmed, results)
            }
            show(seq, results)
          })
          .catch((err: unknown) => {
            if (err instanceof DOMException && err.name === 'AbortError') return // switched off, not an error
            show(seq, []) // a dead network means no dropdown, never a stale one
          })
      },
      known ? 0 : SUGGEST_DEBOUNCE_MS,
    )
    return () => clearTimeout(timer)
  }, [active, trimmed])

  return { suggestions, searching: active && settledFor !== trimmed }
}
