import { describe, expect, it } from 'vitest'
import { displayShade } from './shade'

describe('displayShade', () => {
  it('leaves the endpoints alone: bare stays bare, full stays full', () => {
    expect(displayShade(0)).toBe(0)
    expect(displayShade(1)).toBe(1)
  })

  it('is strictly monotone, so preset ordering and the LOW_SHADE firing set survive', () => {
    const samples = [0, 0.05, 0.15, 0.2, 0.4, 0.6, 0.8, 0.95, 1]
    for (let i = 1; i < samples.length; i++) {
      expect(displayShade(samples[i])).toBeGreaterThan(displayShade(samples[i - 1]))
    }
  })

  it('never displays below the measured value (k >= 1 lifts, never lowers)', () => {
    for (const f of [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99]) {
      expect(displayShade(f)).toBeGreaterThanOrEqual(f)
    }
  })

  // The two pinned exemplar routes from the 2026-08-27 derivation
  // (history/display-curve.md, month=8): the Mall's measured 76.7% must
  // display 82.6%, and the Brooklyn Bridge Park Greenway counter-case's
  // 20.7% must stay LOW at 24.3% -- if either pin moves, the exponent
  // changed and the derivation needs re-running, not the pin.
  it('reproduces the derivation exemplars at k = 1.2', () => {
    expect(displayShade(0.767)).toBeCloseTo(0.826, 3)
    expect(displayShade(0.207)).toBeCloseTo(0.243, 3)
  })
})
