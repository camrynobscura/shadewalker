import { afterEach, describe, expect, it, vi } from 'vitest'
import { geocode, reverseGeocode } from './api'

// Both functions are thin fetches against our own /geocode proxy since
// 2026-08-30 — label building (including the address-not-POI reverse
// rule, the Lucali story) lives server-side now and is pinned in
// tests/test_server_geocode.py. What's left to test here is exactly what
// this file owns: the response shapes and the null paths.

function mockFetchOnce(body: unknown, ok = true) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok, json: () => Promise.resolve(body) }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('geocode', () => {
  it('returns the first proxy result', async () => {
    mockFetchOnce({ results: [{ lat: 40.6818, lon: -74.0004, label: '575 Henry Street, Brooklyn' }] })
    const result = await geocode('575 Henry St')
    expect(result).toEqual({ lat: 40.6818, lon: -74.0004, label: '575 Henry Street, Brooklyn' })
  })

  it('returns null when the proxy has no matches', async () => {
    mockFetchOnce({ results: [] })
    expect(await geocode('zzzzzz')).toBeNull()
  })

  it('returns null when the request fails (proxy down, upstream 502)', async () => {
    mockFetchOnce({}, false)
    expect(await geocode('Court St')).toBeNull()
  })
})

describe('reverseGeocode', () => {
  it('returns the proxy label', async () => {
    mockFetchOnce({ label: '575 Henry Street' })
    expect(await reverseGeocode({ lat: 40.6818319, lon: -74.0003712 })).toBe('575 Henry Street')
  })

  it('returns null when nothing address-shaped is nearby', async () => {
    // e.g. a click inside a park polygon — the caller keeps showing
    // coordinates, which is the honest label there.
    mockFetchOnce({ label: null })
    expect(await reverseGeocode({ lat: 40.662, lon: -73.975 })).toBeNull()
  })

  it('returns null when the request itself fails', async () => {
    mockFetchOnce({}, false)
    expect(await reverseGeocode({ lat: 40.68, lon: -73.99 })).toBeNull()
  })
})
