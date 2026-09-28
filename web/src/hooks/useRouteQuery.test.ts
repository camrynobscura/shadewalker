import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RouteError, type RouteFeature, type RouteResponse } from '../api'
import { ROUTE_TIMEOUT_MS, useRouteQuery } from './useRouteQuery'

const { fetchRoute } = vi.hoisted(() => ({ fetchRoute: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchRoute,
}))

const START = { lat: 40.68, lon: -73.99 }
const END = { lat: 40.686, lon: -73.984 }

function feature(
  treeWeight: number,
  lengthM: number,
  treeCount: number,
  shadeFraction: number,
): RouteFeature {
  return {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: [] },
    properties: {
      tree_weight: treeWeight,
      length_m: lengthM,
      minutes: 1,
      tree_count: treeCount,
      shade_fraction: shadeFraction,
      park_canopy_share: 0,
      segments: [],
    },
  }
}

function fakeResponse(): RouteResponse {
  return {
    routes: [
      feature(0, 90, 2, 0.2),
      feature(5, 95, 3, 0.3),
      feature(15, 100, 5, 0.5),
      feature(40, 110, 8, 0.7),
    ],
    snapped: { start: START, end: END },
    month: 7,
    day: 15,
    hour: 13,
    minute: 0,
    layers: 'both',
    night: false,
    description: 'Head 100 m along Court Street.',
  }
}

afterEach(() => {
  cleanup()
  fetchRoute.mockReset()
})

describe('useRouteQuery', () => {
  it('does not fetch until both start and end are set', () => {
    renderHook(() => useRouteQuery(null, null, 15))
    expect(fetchRoute).not.toHaveBeenCalled()
  })

  it('fetches once both points are present, and derives selected/baseline from treeWeight', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))

    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(result.current.route?.description).toBe('Head 100 m along Court Street.')
    expect(result.current.selected?.properties.tree_weight).toBe(15)
    expect(result.current.baseline?.properties.tree_weight).toBe(0)
    expect(result.current.snappedStart).toEqual(START)
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('changing treeWeight does not trigger a new fetch -- it only re-selects from the already-fetched routes', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(fetchRoute).toHaveBeenCalledTimes(1)

    act(() => result.current.setTreeWeight(40))

    expect(fetchRoute).toHaveBeenCalledTimes(1) // still just the one fetch
    expect(result.current.selected?.properties.tree_weight).toBe(40)
    expect(result.current.baseline?.properties.tree_weight).toBe(0) // unchanged
  })

  it('surfaces a RouteError message directly, and clears any stale snap points', async () => {
    fetchRoute.mockRejectedValue(new RouteError('Start point is outside our current coverage area'))
    const { result } = renderHook(() => useRouteQuery(START, END, 15))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error).toBe('Start point is outside our current coverage area')
    expect(result.current.route).toBeNull()
    expect(result.current.snappedStart).toBeNull()
  })

  it('falls back to a generic message for a non-RouteError failure (e.g. dead server)', async () => {
    fetchRoute.mockRejectedValue(new Error('network down'))
    const { result } = renderHook(() => useRouteQuery(START, END, 15))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error).toBe("couldn't load the route — check your connection and try again")
  })

  it('gives up on a request that never answers after ROUTE_TIMEOUT_MS, with its own message', async () => {
    vi.useFakeTimers()
    try {
      // A hung server: the promise settles only when the signal aborts,
      // and then with the abort reason -- exactly what fetch() does.
      fetchRoute.mockImplementation(
        (_from, _to, _weights, signal: AbortSignal) =>
          new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason))),
      )
      const { result } = renderHook(() => useRouteQuery(START, END, 15))
      expect(result.current.loading).toBe(true)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(ROUTE_TIMEOUT_MS - 1)
      })
      expect(result.current.loading).toBe(true)
      expect(result.current.error).toBeNull()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1)
      })
      expect(result.current.loading).toBe(false)
      expect(result.current.error).toBe('the server took too long — try again')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a superseded request (cleanup abort) stays silent -- no error, no timeout later', async () => {
    vi.useFakeTimers()
    try {
      fetchRoute.mockImplementation(
        (_from, _to, _weights, signal: AbortSignal) =>
          new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason))),
      )
      const { result, unmount } = renderHook(() => useRouteQuery(START, END, 15))
      unmount()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ROUTE_TIMEOUT_MS + 1)
      })
      expect(result.current.error).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('setStart clears a stale snappedStart immediately, before the next fetch resolves', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.snappedStart).not.toBeNull())

    fetchRoute.mockImplementation(() => new Promise(() => {})) // never resolves
    act(() => result.current.setStart({ lat: 40.7, lon: -73.9 }))
    expect(result.current.snappedStart).toBeNull()
  })

  it('sends the set walk time; a new time waits for FIND_ROUTE', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const sunday = { year: 2026, month: 9, day: 27, hour: 9, minute: 0 }
    const { result } = renderHook(() => useRouteQuery(START, END, 15, sunday))
    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(fetchRoute).toHaveBeenCalledTimes(1)
    expect(fetchRoute.mock.calls[0][4]).toEqual(sunday)

    act(() => result.current.setWalkTime(null))
    expect(fetchRoute).toHaveBeenCalledTimes(1) // editing alone never routes

    act(() => result.current.findRoute())
    await waitFor(() => expect(fetchRoute).toHaveBeenCalledTimes(2))
    expect(fetchRoute.mock.calls[1][4]).toBeNull()
  })

  it('without points at mount, routes only on FIND_ROUTE', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(null, null, 15))
    act(() => result.current.setStart(START))
    act(() => result.current.setEnd(END))
    expect(fetchRoute).not.toHaveBeenCalled()

    act(() => result.current.findRoute())
    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(fetchRoute).toHaveBeenCalledTimes(1)
  })

  it('editing a point keeps the drawn route until FIND_ROUTE; the same trip again is not re-fetched', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.route).not.toBeNull())

    act(() => result.current.findRoute()) // back, then FIND_ROUTE on the same trip
    expect(fetchRoute).toHaveBeenCalledTimes(1)

    act(() => result.current.setEnd({ lat: 40.69, lon: -73.98 }))
    expect(result.current.route).not.toBeNull()
    expect(fetchRoute).toHaveBeenCalledTimes(1)

    act(() => result.current.findRoute())
    await waitFor(() => expect(fetchRoute).toHaveBeenCalledTimes(2))
  })

  it('emptying a field clears the route: there is no trip left', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.route).not.toBeNull())

    act(() => result.current.setStart(null))
    await waitFor(() => expect(result.current.route).toBeNull())
  })

  it('auto (desktop): routes as soon as both points are set, and again on every change', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(null, null, 15, null, true))
    act(() => result.current.setStart(START))
    expect(fetchRoute).not.toHaveBeenCalled()
    act(() => result.current.setEnd(END))
    await waitFor(() => expect(fetchRoute).toHaveBeenCalledTimes(1))

    act(() => result.current.setWalkTime({ year: 2026, month: 9, day: 27, hour: 9, minute: 0 }))
    await waitFor(() => expect(fetchRoute).toHaveBeenCalledTimes(2))
    // The same trip again is no change: no third fetch.
    act(() => result.current.setWalkTime({ year: 2026, month: 9, day: 27, hour: 9, minute: 0 }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetchRoute).toHaveBeenCalledTimes(2)
  })

  it('FIND_ROUTE retries the same trip after it failed', async () => {
    fetchRoute.mockRejectedValueOnce(new Error('network down'))
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.error).not.toBeNull())

    fetchRoute.mockResolvedValue(fakeResponse())
    act(() => result.current.findRoute())
    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(fetchRoute).toHaveBeenCalledTimes(2)
  })

  it('setting an equal walk time again does not re-fetch', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const sunday = { year: 2026, month: 9, day: 27, hour: 9, minute: 0 }
    const { result } = renderHook(() => useRouteQuery(START, END, 15, sunday))
    await waitFor(() => expect(result.current.route).not.toBeNull())

    act(() => result.current.setWalkTime({ ...sunday }))
    expect(fetchRoute).toHaveBeenCalledTimes(1)
  })

  // clear() was removed with the CLEAR_ROUTE button (2026-09-02): fields
  // clear individually via each field's ✕, and the wordmark's home link
  // is the full reset.
})
