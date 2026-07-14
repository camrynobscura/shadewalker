import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RouteError, type RouteResponse } from '../api'
import { useRouteQuery } from './useRouteQuery'

const { fetchRoute } = vi.hoisted(() => ({ fetchRoute: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchRoute,
}))

const START = { lat: 40.68, lon: -73.99 }
const END = { lat: 40.686, lon: -73.984 }

function fakeResponse(): RouteResponse {
  return {
    green: { type: 'Feature', geometry: { type: 'LineString', coordinates: [] }, properties: { length_m: 100, minutes: 1, tree_count: 5, shade_fraction: 0.5, segments: [] } },
    shortest: { type: 'Feature', geometry: { type: 'LineString', coordinates: [] }, properties: { length_m: 90, minutes: 1, tree_count: 2, shade_fraction: 0.2, segments: [] } },
    snapped: { start: START, end: END },
    comparison: { extra_length_m: 10, extra_trees: 3, extra_shade_pct: 30, month: 7, tree_weight: 15 },
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

  it('fetches once both points are present, and exposes the resolved route', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))

    await waitFor(() => expect(result.current.route).not.toBeNull())
    expect(result.current.route?.description).toBe('Head 100 m along Court Street.')
    expect(result.current.snappedStart).toEqual(START)
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toBeNull()
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

  it('clear() resets start, end, route, and error together', async () => {
    fetchRoute.mockResolvedValue(fakeResponse())
    const { result } = renderHook(() => useRouteQuery(START, END, 15))
    await waitFor(() => expect(result.current.route).not.toBeNull())

    act(() => result.current.clear())
    expect(result.current.start).toBeNull()
    expect(result.current.end).toBeNull()
    expect(result.current.route).toBeNull()
    expect(result.current.error).toBeNull()
  })
})
