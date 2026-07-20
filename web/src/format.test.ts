import { describe, expect, it } from 'vitest'
import { formatCoords, formatDistance, formatDistanceParts } from './format'

describe('formatDistance', () => {
  it('rounds a short distance to whole feet', () => {
    expect(formatDistance(30)).toBe('98 ft')
  })

  it('stays in feet just under the 0.1-mile threshold', () => {
    // 528 ft (0.1 mi) = 160.9344 m -- one meter under that stays "ft".
    expect(formatDistance(159.9344)).toBe('525 ft')
  })

  it('switches to miles exactly at the 0.1-mile threshold', () => {
    expect(formatDistance(160.9344)).toBe('0.1 mi')
  })

  it('keeps one decimal for a multi-mile distance', () => {
    expect(formatDistance(3218.69)).toBe('2.0 mi')
  })
})

describe('formatDistanceParts', () => {
  it('splits a miles distance into value and unit', () => {
    expect(formatDistanceParts(3218.69)).toEqual({ value: '2.0', unit: 'mi' })
  })

  it('splits a feet distance into value and unit', () => {
    expect(formatDistanceParts(30)).toEqual({ value: '98', unit: 'ft' })
  })

  it('always agrees with formatDistance', () => {
    // The two exist as one formatting rule with two output shapes -- the
    // stat box's <small>-styled unit must never drift from the prose form.
    for (const meters of [5, 30, 159.9344, 160.9344, 1000, 3218.69, 20000]) {
      const { value, unit } = formatDistanceParts(meters)
      expect(`${value} ${unit}`).toBe(formatDistance(meters))
    }
  })
})

describe('formatCoords', () => {
  it('renders lat/lon to 4 decimal places, comma-separated', () => {
    expect(formatCoords({ lat: 40.6795, lon: -73.9962 })).toBe('40.6795, -73.9962')
  })

  it('pads a value with fewer decimal places out to 4', () => {
    expect(formatCoords({ lat: 40.68, lon: -73.5 })).toBe('40.6800, -73.5000')
  })

  it('rounds rather than truncates past the 4th decimal place', () => {
    expect(formatCoords({ lat: 40.67951, lon: -73.99615 })).toBe('40.6795, -73.9962')
  })
})
