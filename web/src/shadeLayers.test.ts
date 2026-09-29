import { describe, expect, it } from 'vitest'
import { describeShadeLayers, shadeLayersFromLink } from './shadeLayers'

describe('shadeLayersFromLink', () => {
  it.each([
    ['layers=trees', 'trees'],
    ['layers=buildings', 'buildings'],
    ['', 'both'],
    ['layers=both', 'both'],
    ['layers=sun', 'both'],
    ['layers=Trees', 'both'],
  ])('reads "%s" as %s', (query, expected) => {
    expect(shadeLayersFromLink(new URLSearchParams(query))).toBe(expected)
  })
})

describe('describeShadeLayers', () => {
  it("says the pick in the pill's words", () => {
    expect(describeShadeLayers('both')).toBe('All shade')
    expect(describeShadeLayers('trees')).toBe('Tree shade')
    expect(describeShadeLayers('buildings')).toBe('Building shade')
  })
})
