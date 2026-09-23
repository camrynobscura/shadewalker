import { describe, expect, it } from 'vitest'
import {
  formatCoords,
  formatDistance,
  formatDistanceParts,
  formatEtaParts,
  spokenDistance,
  spokenEta,
} from './format'

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

describe('formatEtaParts', () => {
  it('stays in minutes under an hour', () => {
    expect(formatEtaParts(42)).toEqual([{ value: '42', unit: 'min' }])
  })

  it('splits an hour-plus eta into hr and min', () => {
    expect(formatEtaParts(150)).toEqual([
      { value: '2', unit: 'hr' },
      { value: '30', unit: 'min' },
    ])
  })

  it('drops a zero-minute remainder rather than showing "0 min"', () => {
    expect(formatEtaParts(120)).toEqual([{ value: '2', unit: 'hr' }])
  })

  it('rolls 59.6 over into "1 hr", never "60 min"', () => {
    // Rounding must happen BEFORE the hour split: round(59.6) = 60.
    expect(formatEtaParts(59.6)).toEqual([{ value: '1', unit: 'hr' }])
  })

  it('rounds before splitting so 119.7 reads "2 hr"', () => {
    expect(formatEtaParts(119.7)).toEqual([{ value: '2', unit: 'hr' }])
  })

  it('keeps a sub-minute eta at "0 min"', () => {
    expect(formatEtaParts(0.3)).toEqual([{ value: '0', unit: 'min' }])
  })

  it('stays in minutes just under the hour', () => {
    expect(formatEtaParts(59.4)).toEqual([{ value: '59', unit: 'min' }])
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

describe('spoken twins', () => {
  it('speaks hours and minutes as full plural words', () => {
    expect(spokenEta(129.4)).toBe('2 hours 9 minutes')
  })

  it('keeps the singular for exactly one hour', () => {
    expect(spokenEta(60)).toBe('1 hour')
  })

  it('speaks miles and feet as words', () => {
    expect(spokenDistance(10783)).toBe('6.7 miles')
    expect(spokenDistance(30)).toBe('98 feet')
  })
})
