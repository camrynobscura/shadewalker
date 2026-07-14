import { useEffect, useState } from 'react'
import { fetchRoute, RouteError, type Point, type RouteResponse } from '../api'

export interface UseRouteQueryResult {
  start: Point | null
  end: Point | null
  treeWeight: number
  route: RouteResponse | null
  loading: boolean
  error: string | null
  /** Where the route actually starts/ends once the server resolves it onto
   * the street network — can differ from `start`/`end` (what was clicked or
   * geocoded), since that point may sit mid-block. Display-only. */
  snappedStart: Point | null
  snappedEnd: Point | null
  setTreeWeight: (weight: number) => void
  setStart: (point: Point | null) => void
  setEnd: (point: Point | null) => void
  clear: () => void
}

/** Owns the request → response lifecycle for a route: start/end/treeWeight
 * state, the fetch-on-change effect (with abort-on-supersede so a slow
 * stale response can't paint over a fresh one), and the resolved snap
 * points the server returns alongside a route. Deliberately doesn't touch
 * the URL — App.tsx mirrors the returned start/end/treeWeight to the query
 * string itself, a separate concern that doesn't need to know how the
 * fetch works. */
export function useRouteQuery(
  initialStart: Point | null,
  initialEnd: Point | null,
  initialTreeWeight: number,
): UseRouteQueryResult {
  const [start, setStartRaw] = useState<Point | null>(initialStart)
  const [end, setEndRaw] = useState<Point | null>(initialEnd)
  const [treeWeight, setTreeWeight] = useState<number>(initialTreeWeight)

  const [route, setRoute] = useState<RouteResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [snappedStart, setSnappedStart] = useState<Point | null>(null)
  const [snappedEnd, setSnappedEnd] = useState<Point | null>(null)

  // Wrap the raw setters so picking a new point immediately drops its own
  // stale snapped marker — without this, setting a new `start` while `end`
  // stays put (e.g. searching a different start address) wouldn't clear
  // `snappedStart`, and the old marker would sit in the wrong place until
  // the next fetch resolves.
  function setStart(p: Point | null) {
    setSnappedStart(null)
    setStartRaw(p)
  }
  function setEnd(p: Point | null) {
    setSnappedEnd(null)
    setEndRaw(p)
  }

  function clear() {
    setStart(null)
    setEnd(null)
    setRoute(null)
    setError(null)
  }

  // Fetch whenever the request changes. The AbortController in the cleanup
  // cancels the in-flight request each time a newer one supersedes it (e.g.
  // dragging the slider) — otherwise slow responses could arrive out of
  // order and paint a stale route over a fresh one.
  useEffect(() => {
    if (!start || !end) {
      setRoute(null)
      setSnappedStart(null)
      setSnappedEnd(null)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    fetchRoute(start, end, treeWeight, controller.signal)
      .then((data) => {
        setRoute(data)
        setSnappedStart(data.snapped.start)
        setSnappedEnd(data.snapped.end)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return // superseded, not an error
        // RouteError = the server responded with a specific, useful reason
        // (e.g. outside coverage) — show that. Anything else (dead server,
        // no network) gets the generic fallback instead of a raw fetch error.
        setError(err instanceof RouteError ? err.message : 'Could not find a route — is the server running?')
        setSnappedStart(null)
        setSnappedEnd(null)
        setLoading(false)
      })
    return () => controller.abort()
  }, [start, end, treeWeight])

  return {
    start,
    end,
    treeWeight,
    route,
    loading,
    error,
    snappedStart,
    snappedEnd,
    setTreeWeight,
    setStart,
    setEnd,
    clear,
  }
}
