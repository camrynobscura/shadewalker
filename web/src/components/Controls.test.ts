import { describe, expect, it } from 'vitest'
import { snapToPreset } from './Controls'

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
