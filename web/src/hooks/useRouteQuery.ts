import { useEffect, useState } from 'react'
import { fetchRoute, RouteError, type Point, type RouteFeature, type RouteResponse } from '../api'
import { TREE_PRESETS } from '../presets'
import { sameWalkTime, type WalkTime } from '../walkTime'

const TREE_WEIGHTS = TREE_PRESETS.map((preset) => preset.value)

/** How long one route request may take before the client gives up. The
 * slowest live route measured after the 2026-09-09 fold was ~1s for
 * 13km, and the box has one worker, so anything past 10s is a stuck
 * worker or a queue of visitors, not a long walk (user call
 * 2026-09-09). Without this, a hung server showed the vine forever. */
export const ROUTE_TIMEOUT_MS = 10_000

/** The trip a route was asked for: a snapshot of the fields at FIND_ROUTE,
 * so editing them afterwards doesn't re-route until it's pressed again. */
interface RouteRequest {
  start: Point
  end: Point
  walkTime: WalkTime | null
}

function sameRequest(a: RouteRequest | null, b: RouteRequest): boolean {
  return (
    a !== null &&
    a.start.lat === b.start.lat &&
    a.start.lon === b.start.lon &&
    a.end.lat === b.end.lat &&
    a.end.lon === b.end.lon &&
    sameWalkTime(a.walkTime, b.walkTime)
  )
}

export interface UseRouteQueryResult {
  start: Point | null
  end: Point | null
  treeWeight: number
  /** The set departure time, or null for "leave now". */
  walkTime: WalkTime | null
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
  setWalkTime: (time: WalkTime | null) => void
  /** FIND_ROUTE: route the trip as the fields stand now. A no-op without
   * both points; the same trip again (after going back to look) keeps the
   * route already drawn, unless that one failed. */
  findRoute: () => void
}

/** Owns the request → response lifecycle for a route: start/end/treeWeight
 * and walk-time state, the fetch effect (with abort-on-supersede so a slow
 * stale response can't paint over a fresh one), and the resolved snap
 * points the server returns alongside a route.
 *
 * On a phone (`auto` false) routes are fetched on FIND_ROUTE
 * (`findRoute`), not whenever both points exist (user, 2026-09-27): the
 * phone panel is two screens, and the button is the checkpoint where a
 * wrong address gets caught before the app moves on. Points given at
 * mount (a shared link's) count as already found. Editing a field keeps
 * the drawn route until the next FIND_ROUTE; emptying one clears it,
 * since there's no trip left. On desktop (`auto` true) there's no second
 * screen to move to, so no button either: the fields' trip is routed the
 * moment it's complete, and again whenever it changes (user,
 * 2026-09-27 -- the behaviour from before FIND_ROUTE). Deliberately doesn't touch
 * the URL — App.tsx mirrors the returned start/end/treeWeight to the query
 * string itself, a separate concern that doesn't need to know how the
 * fetch works.
 *
 * Fetches every Shade_priority preset (TREE_WEIGHTS) in one request per
 * start/end pair, not one request per preset -- changing `treeWeight`
 * alone never triggers a new fetch, it's a pure lookup into whatever the
 * last fetch already returned. This exists because comparing presets by
 * flipping Shade_priority back and forth is a real, expected usage
 * pattern (the route rows show all four at once), and re-fetching over the
 * network on every click made that feel laggy for no real benefit --
 * computing all four presets server-side costs microseconds more than
 * computing one. */
export function useRouteQuery(
  initialStart: Point | null,
  initialEnd: Point | null,
  initialTreeWeight: number,
  initialWalkTime: WalkTime | null = null,
  auto = false,
): UseRouteQueryResult {
  const [start, setStartRaw] = useState<Point | null>(initialStart)
  const [end, setEndRaw] = useState<Point | null>(initialEnd)
  const [treeWeight, setTreeWeight] = useState<number>(initialTreeWeight)
  const [walkTime, setWalkTimeRaw] = useState<WalkTime | null>(initialWalkTime)
  const [request, setRequest] = useState<RouteRequest | null>(() =>
    initialStart && initialEnd ? { start: initialStart, end: initialEnd, walkTime: initialWalkTime } : null,
  )

  const [route, setRoute] = useState<RouteResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Desktop: the complete trip in the fields IS the request. Adjusted
  // while rendering (React's pattern for state that follows other state)
  // rather than in an effect, so the fetch can't lag a render behind. A
  // failed trip isn't retried until something in it changes -- the same
  // as before FIND_ROUTE existed.
  if (auto && start && end) {
    const fields = { start, end, walkTime }
    if (!sameRequest(request, fields)) setRequest(fields)
  }

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
    if (!p) setRequest(null)
  }
  function setEnd(p: Point | null) {
    setSnappedEnd(null)
    setEndRaw(p)
    if (!p) setRequest(null)
  }
  function setWalkTime(t: WalkTime | null) {
    setWalkTimeRaw((current) => (sameWalkTime(current, t) ? current : t))
  }

  function findRoute() {
    if (!start || !end) return
    const next = { start, end, walkTime }
    // A failed request gets a fresh object, so the same trip retries.
    setRequest((current) => (sameRequest(current, next) && !error ? current : next))
  }

  // Fetch whenever a new trip is requested -- deliberately NOT on
  // treeWeight, see this hook's own doc comment above. The
  // AbortController in the cleanup cancels the in-flight request each
  // time a newer one supersedes it (e.g. FIND_ROUTE again before the
  // previous fetch resolves) — otherwise slow responses could arrive out
  // of order and paint a stale route over a fresh one.
  useEffect(() => {
    if (!request) {
      setRoute(null)
      setSnappedStart(null)
      setSnappedEnd(null)
      return
    }
    const controller = new AbortController()
    // The same controller serves both ends: cleanup aborts with the
    // default AbortError (superseded, say nothing), the deadline aborts
    // with a TimeoutError (say so). fetch rejects with the abort reason,
    // so the catch below can tell them apart.
    const deadline = setTimeout(
      () => controller.abort(new DOMException('route request timed out', 'TimeoutError')),
      ROUTE_TIMEOUT_MS,
    )
    setLoading(true)
    setError(null)
    fetchRoute(request.start, request.end, TREE_WEIGHTS, controller.signal, request.walkTime)
      .then((data) => {
        setRoute(data)
        setSnappedStart(data.snapped.start)
        setSnappedEnd(data.snapped.end)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === 'AbortError') return // superseded, not an error
        // RouteError = the server responded with a specific, useful reason
        // (e.g. outside coverage) — show that. A timeout gets its own
        // line. Anything else (no network, a dropped request, a 5xx from
        // the proxy) gets the generic fallback instead of a raw fetch
        // error. Worded for the person on a phone, not the developer —
        // "is the server running?" shipped to a real user's screen via a
        // flaky tunnel (2026-09-02). Controls' error slot prefixes
        // "// ERROR:", so these read as its sentence body.
        if (err instanceof DOMException && err.name === 'TimeoutError') {
          setError('the server took too long — try again')
        } else {
          setError(
            err instanceof RouteError
              ? err.message
              : "couldn't load the route — check your connection and try again",
          )
        }
        setSnappedStart(null)
        setSnappedEnd(null)
        setLoading(false)
      })
      .finally(() => clearTimeout(deadline))
    return () => {
      clearTimeout(deadline)
      controller.abort()
    }
  }, [request])

  const selected = route?.routes.find((r) => r.properties.tree_weight === treeWeight) ?? null
  const baseline = route?.routes.find((r) => r.properties.tree_weight === 0) ?? null

  return {
    start,
    end,
    treeWeight,
    walkTime,
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
    setWalkTime,
    findRoute,
  }
}
