import { afterEach, describe, expect, it, vi } from 'vitest'
import { reverseGeocode } from './api'

function mockFetchOnce(body: unknown, ok = true) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok, json: () => Promise.resolve(body) }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('reverseGeocode', () => {
  it('uses house_number + road, not the matched POI name', async () => {
    // The real response for a point right outside Lucali (a Carroll
    // Gardens restaurant) -- display_name on this same response reads
    // "Lucali, 575, Henry Street, ...", business name first. Building the
    // label from `address` directly, not `display_name`, is the fix.
    mockFetchOnce({
      name: 'Lucali',
      display_name:
        'Lucali, 575, Henry Street, Columbia Street Waterfront District, Brooklyn, Kings County, New York, 11231, United States',
      address: { amenity: 'Lucali', house_number: '575', road: 'Henry Street' },
    })
    const label = await reverseGeocode({ lat: 40.6818319, lon: -74.0003712 })
    expect(label).toBe('575, Henry Street')
  })

  it('falls back to just the road when there is no house number', async () => {
    // e.g. a point inside a park, matched to a numbered sports pitch --
    // the real response had `address.leisure: "2"` alongside `road`, no
    // house_number. The "2" must never leak into the label either.
    mockFetchOnce({
      name: '2',
      display_name: '2, West Drive, Brooklyn, Kings County, New York, 11215, United States',
      address: { leisure: '2', road: 'West Drive' },
    })
    const label = await reverseGeocode({ lat: 40.662, lon: -73.975 })
    expect(label).toBe('West Drive')
  })

  it('returns null when Nominatim has no address-shaped answer at all', async () => {
    mockFetchOnce({ error: 'Unable to geocode' })
    const label = await reverseGeocode({ lat: 40.5, lon: -74.5 })
    expect(label).toBeNull()
  })

  it('returns null when the request itself fails', async () => {
    mockFetchOnce({}, false)
    const label = await reverseGeocode({ lat: 40.68, lon: -73.99 })
    expect(label).toBeNull()
  })
})
