import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RouteError, type RouteFeature, type RouteResponse } from '../api'
import { useRouteQuery } from './useRouteQuery'

const { fetchRoute } = vi.hoisted(() => ({ fetchRoute: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchRoute,
}))

const START = { lat: 40.68, lon: -73.99 }
const END = { lat: 40.686, lon: -73.984 }

function feature(treeWeight: number, lengthM: number, treeCount: number, shadeFraction: number): RouteFeature {
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
    routes: [feature(0, 90, 2, 0.2), feature(5, 95, 3, 0.3), feature(15, 100, 5, 0.5), feature(40, 110, 8, 0.7)],
    snapped: { start: START, end: END },
    month: 7,
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
    expect(result.current.error).toBe('Could not find a route — is the server running?')
  })

  it('setStart clears a stale snappedStart immediately, before the next fetch resolves', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.snappedStart).not.toBeNull())

    fetchRoute.mockImplementation(() => new Promise(() => {})) // never resolves
    act(() => result.current.setStart({ lat: 40.7, lon: -73.9 }))
    expect(result.current.snappedStart).toBeNull()
  })

  it('clear() resets start, end, route, selected, and error together', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.route).not.toBeNull())

    act(() => result.current.clear())
    expect(result.current.start).toBeNull()
    expect(result.current.end).toBeNull()
    expect(result.current.route).toBeNull()
    expect(result.current.selected).toBeNull()
    expect(result.current.error).toBeNull()
  })
})
