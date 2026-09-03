import { beforeEach, describe, expect, it, vi } from 'vitest'
import { loadRecents, recordRecent, RECENTS_CAP } from './recents'

const entry = (label: string, lat = 40.68, lon = -73.99) => ({ label, lat, lon })

beforeEach(() => {
  window.localStorage.clear()
})

describe('recordRecent / loadRecents', () => {
  it('round-trips a recorded entry', () => {
    recordRecent(entry('250 Court St'))
    expect(loadRecents()).toEqual([entry('250 Court St')])
  })

  it('orders newest first', () => {
    recordRecent(entry('250 Court St'))
    recordRecent(entry('3rd Ave'))
    expect(loadRecents().map((r) => r.label)).toEqual(['3rd Ave', '250 Court St'])
  })

  it('dedups by label case-insensitively, moving the repeat to the front with its new coords', () => {
    recordRecent(entry('250 Court St', 40.1, -73.1))
    recordRecent(entry('3rd Ave'))
    recordRecent(entry('250 COURT ST', 40.2, -73.2))
    expect(loadRecents()).toEqual([
      { label: '250 COURT ST', lat: 40.2, lon: -73.2 },
      entry('3rd Ave'),
    ])
  })

  it('caps the list, dropping the oldest', () => {
    for (let i = 1; i <= RECENTS_CAP + 2; i++) recordRecent(entry(`Address ${i}`))
    const labels = loadRecents().map((r) => r.label)
    expect(labels).toHaveLength(RECENTS_CAP)
    expect(labels[0]).toBe(`Address ${RECENTS_CAP + 2}`)
    expect(labels).not.toContain('Address 1')
  })

  it('trims the label and ignores blank ones', () => {
    recordRecent(entry('  250 Court St  '))
    recordRecent(entry('   '))
    expect(loadRecents()).toEqual([entry('250 Court St')])
  })

  it('returns nothing for corrupt or wrong-shaped storage', () => {
    window.localStorage.setItem('sw-recents', 'not json {')
    expect(loadRecents()).toEqual([])
    window.localStorage.setItem('sw-recents', '{"a":1}')
    expect(loadRecents()).toEqual([])
  })

  it('filters out entries missing fields, keeping valid ones', () => {
    window.localStorage.setItem(
      'sw-recents',
      JSON.stringify([{ label: 'ok', lat: 1, lon: 2 }, { label: 'no coords' }, null, 5]),
    )
    expect(loadRecents()).toEqual([{ label: 'ok', lat: 1, lon: 2 }])
  })

  it('swallows a write failure instead of throwing', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError')
    })
    expect(() => recordRecent(entry('250 Court St'))).not.toThrow()
    spy.mockRestore()
  })

  it('reads as empty when storage itself throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('SecurityError')
    })
    expect(loadRecents()).toEqual([])
    spy.mockRestore()
  })
})
