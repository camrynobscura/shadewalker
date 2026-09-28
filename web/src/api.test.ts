import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchRoute, geocode, GeocodeUnavailableError, reverseGeocode, RouteError } from './api'

// Both functions are thin fetches against our own /geocode proxy since
// 2026-08-30 — label building (including the address-not-POI reverse
// rule, the Lucali story) lives server-side now and is pinned in
// tests/test_server_geocode.py. What's left to test here is exactly what
// this file owns: the response shapes and the null paths.

function mockFetchOnce(body: unknown, ok = true, status = ok ? 200 : 502) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok, status, json: () => Promise.resolve(body) }))
}

/** A response whose body isn't JSON at all — Caddy's or Vite's proxy
 * error page when uvicorn is down. */
function mockFetchOnceHtml(status: number) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: false, status, json: () => Promise.reject(new SyntaxError('not json')) }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

const A = { lat: 40.68, lon: -73.99 }
const B = { lat: 40.686, lon: -73.984 }

describe('fetchRoute time', () => {
  function requestedParams(): URLSearchParams {
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    return new URL(url, 'http://localhost').searchParams
  }

  it('sends no time for "now", so the server uses its own clock', async () => {
    mockFetchOnce({})
    await fetchRoute(A, B, [0], new AbortController().signal, null)
    for (const key of ['month', 'day', 'hour', 'minute', 'arrive'])
      expect(requestedParams().has(key)).toBe(false)
  })

  it('sends all four parts of a picked time, and no year', async () => {
    mockFetchOnce({})
    const time = { year: 2028, month: 2, day: 29, hour: 9, minute: 5, arrive: false }
    await fetchRoute(A, B, [0], new AbortController().signal, time)
    const params = requestedParams()
    expect(['month', 'day', 'hour', 'minute'].map((key) => params.get(key))).toEqual(['2', '29', '9', '5'])
    expect(params.has('year')).toBe(false)
    expect(params.has('arrive')).toBe(false)
  })

  it('marks an arrival, so the server scores the walk for when it leaves', async () => {
    mockFetchOnce({})
    const time = { year: 2026, month: 9, day: 28, hour: 13, minute: 0, arrive: true }
    await fetchRoute(A, B, [0], new AbortController().signal, time)
    const params = requestedParams()
    expect(params.get('arrive')).toBe('true')
    expect(['month', 'day', 'hour', 'minute'].map((key) => params.get(key))).toEqual(['9', '28', '13', '0'])
  })
})

describe('fetchRoute errors', () => {
  it("shows the server's detail for a 4xx that carries one", async () => {
    mockFetchOnce({ detail: 'No path between these points' }, false, 422)
    await expect(fetchRoute(A, B, [0], new AbortController().signal)).rejects.toThrow(
      new RouteError('No path between these points'),
    )
  })

  it('treats a 5xx as a broken server, not a message (a plain Error, so the caller uses its generic wording)', async () => {
    mockFetchOnceHtml(502)
    const failure = fetchRoute(A, B, [0], new AbortController().signal)
    await expect(failure).rejects.toThrow(Error)
    await expect(failure).rejects.not.toBeInstanceOf(RouteError)
  })

  it('treats a 4xx without our detail the same way', async () => {
    mockFetchOnceHtml(404)
    await expect(fetchRoute(A, B, [0], new AbortController().signal)).rejects.not.toBeInstanceOf(RouteError)
  })

  it('has a wait message for a 429 even without a detail body', async () => {
    mockFetchOnce({ error: 'Rate limit exceeded: 30 per 1 minute' }, false, 429)
    await expect(fetchRoute(A, B, [0], new AbortController().signal)).rejects.toThrow(
      new RouteError('too many routes at once — wait a moment and try again'),
    )
  })
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

  it('throws GeocodeUnavailableError when the request fails (proxy down, upstream 502) -- never null, which means "no match"', async () => {
    mockFetchOnce({ detail: 'Geocoding is temporarily unavailable' }, false, 502)
    await expect(geocode('Court St')).rejects.toBeInstanceOf(GeocodeUnavailableError)
  })

  it('throws GeocodeUnavailableError when fetch itself throws (no network)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(geocode('Court St')).rejects.toBeInstanceOf(GeocodeUnavailableError)
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

  it('returns null when fetch throws (no network) -- the coordinate fallback stays, nothing rejects unhandled', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    expect(await reverseGeocode({ lat: 40.68, lon: -73.99 })).toBeNull()
  })
})
