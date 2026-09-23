import { describe, expect, it } from 'vitest'
import type { RouteFeature } from './api'
import { compareRoutes, snapToPreset } from './presets'

function feature(lengthM: number, minutes: number, treeCount: number, shadeFraction: number): RouteFeature {
  return {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: [] },
    properties: {
      tree_weight: 0,
      length_m: lengthM,
      minutes,
      tree_count: treeCount,
      shade_fraction: shadeFraction,
      park_canopy_share: 0,
      segments: [],
    },
  }
}

describe('compareRoutes', () => {
  it('reports a shadier route costing more time and distance as positive deltas', () => {
    const baseline = feature(1634.4, 19.5, 211, 0.745)
    const selected = feature(1693.3, 20.2, 290, 0.95)

    expect(compareRoutes(selected, baseline)).toEqual({
      extraMinutes: 1, // 20.2 - 19.5 = 0.7, rounds to 1
      // On the DISPLAYED scale (shade.ts, k=1.2): displayShade(0.95) =
      // 0.9725, displayShade(0.745) = 0.8060 -- delta 16.65, rounds to
      // 17. Smaller than the raw 20-point spread because the curve
      // compresses toward the top; matches what the two presets show.
      extraShadePct: 17,
      extraLengthM: 58.9,
    })
  })

  it('reports all-zero deltas when selected and baseline are the same route (NONE selected)', () => {
    const route = feature(1634.4, 19.5, 211, 0.745)
    expect(compareRoutes(route, route)).toEqual({
      extraMinutes: 0,
      extraShadePct: 0,
      extraLengthM: 0,
    })
  })

  it('reports negative deltas when selected is cheaper than baseline', () => {
    const baseline = feature(200, 3, 10, 0.5)
    const selected = feature(150, 2, 5, 0.3)
    expect(compareRoutes(selected, baseline)).toEqual({
      extraMinutes: -1,
      // displayShade(0.3) = 0.3482, displayShade(0.5) = 0.5647 -- the
      // curve widens the mid-range, so the displayed delta (-21.65,
      // rounds to -22) is larger than the raw -20.
      extraShadePct: -22,
      extraLengthM: -50,
    })
  })
})

describe('snapToPreset', () => {
  it.each([0, 5, 15, 40])('leaves an exact preset value %i unchanged', (value) => {
    expect(snapToPreset(value)).toBe(value)
  })

  it('snaps a value strictly between two presets to the nearer one', () => {
    expect(snapToPreset(3)).toBe(5) // closer to 5 than to 0
  })

  it('breaks an exact tie in favor of the later, shadier preset', () => {
    // 10 is equidistant between LOW (5) and MED (15) -- the `<=` in
    // snapToPreset's loop is what makes ties go to the later option.
    expect(snapToPreset(10)).toBe(15)
  })

  it('clamps a value above the top preset to the top preset', () => {
    expect(snapToPreset(100)).toBe(40)
  })

  it('clamps a negative value to the bottom preset', () => {
    expect(snapToPreset(-10)).toBe(0)
  })
})
