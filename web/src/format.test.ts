import { describe, expect, it } from 'vitest'
import { formatDistance } from './format'

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
