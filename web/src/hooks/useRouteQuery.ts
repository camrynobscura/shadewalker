import { useEffect, useState } from 'react'
import { fetchRoute, RouteError, type Point, type RouteFeature, type RouteResponse } from '../api'
import { TREE_PRESETS } from '../presets'

const TREE_WEIGHTS = TREE_PRESETS.map((preset) => preset.value)

export interface UseRouteQueryResult {
  start: Point | null
  end: Point | null
  treeWeight: number
  route: RouteResponse | null
  /** The Feature matching the currently selected treeWeight -- what
   * RouteStats displays, and one of the two lines MapView draws. */
  selected: RouteFeature | null
  /** The Feature for tree_weight=0 (NONE) -- always fetched alongside
   * whatever's selected, since it's the baseline every comparison and the
   * map's "fastest route" line is measured against. */
  baseline: RouteFeature | null
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
 * fetch works.
 *
 * Fetches every Shade_priority preset (TREE_WEIGHTS) in one request per
 * start/end pair, not one request per preset -- changing `treeWeight`
 * alone never triggers a new fetch, it's a pure lookup into whatever the
 * last fetch already returned. This exists because comparing presets by
 * flipping Shade_priority back and forth is a real, expected usage
 * pattern (see the comparison line under it), and re-fetching over the
 * network on every click made that feel laggy for no real benefit --
 * computing all four presets server-side costs microseconds more than
 * computing one. */
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

  // Fetch whenever start/end changes -- deliberately NOT treeWeight, see
  // this hook's own doc comment above. The AbortController in the cleanup
  // cancels the in-flight request each time a newer one supersedes it
  // (e.g. picking a new start before the previous fetch resolves) —
  // otherwise slow responses could arrive out of order and paint a stale
  // route over a fresh one.
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
    fetchRoute(start, end, TREE_WEIGHTS, controller.signal)
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
  }, [start, end])

  const selected = route?.routes.find((r) => r.properties.tree_weight === treeWeight) ?? null
  const baseline = route?.routes.find((r) => r.properties.tree_weight === 0) ?? null

  return {
    start,
    end,
    treeWeight,
    route,
    selected,
    baseline,
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
