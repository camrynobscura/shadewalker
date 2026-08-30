import { cleanup, renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { GeocodeResult } from '../api'
import { SUGGEST_DEBOUNCE_MS, useGeocodeSuggestions } from './useGeocodeSuggestions'

// Mock the network boundary, not fetch itself -- suggest() is api.ts's
// concern (covered in api.test.ts); this file owns the debounce/cancel/
// enabled logic layered on top of it.
const { suggest } = vi.hoisted(() => ({ suggest: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  suggest,
}))

const RESULTS: GeocodeResult[] = [
  { lat: 40.68, lon: -73.998, label: '250 Court St, Brooklyn' },
  { lat: 40.6865, lon: -73.9922, label: 'Court St & Baltic St, Brooklyn' },
]

beforeEach(() => {
  vi.useFakeTimers()
  suggest.mockResolvedValue(RESULTS)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  suggest.mockReset()
})

function render(initialProps: { query: string; enabled: boolean }) {
  return renderHook(({ query, enabled }) => useGeocodeSuggestions(query, enabled), { initialProps })
}

async function elapse(ms: number) {
  // advanceTimersByTimeAsync flushes the promise chain a resolved
  // suggest() queues behind the timer -- the plain sync variant would
  // fire the timeout but leave .then(setSuggestions) forever pending.
  await act(() => vi.advanceTimersByTimeAsync(ms))
}

describe('useGeocodeSuggestions', () => {
  it('fetches once the debounce window passes, not before', async () => {
    const { result } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS - 1)
    expect(suggest).not.toHaveBeenCalled()
    await elapse(1)
    expect(suggest).toHaveBeenCalledTimes(1)
    expect(result.current).toEqual(RESULTS)
  })

  it('never fetches below the minimum length (whitespace does not count)', async () => {
    const { result } = render({ query: '  co  ', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS * 2)
    expect(suggest).not.toHaveBeenCalled()
    expect(result.current).toEqual([])
  })

  it('a fresh keystroke inside the window cancels the pending lookup', async () => {
    const { rerender } = render({ query: 'cour', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS - 50)
    rerender({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    // One request total, for the text as it stands after the last key --
    // the per-keystroke politeness the debounce exists to provide.
    expect(suggest).toHaveBeenCalledTimes(1)
    expect(suggest.mock.calls[0][0]).toBe('court')
  })

  it('disabling clears the list without another fetch', async () => {
    const { result, rerender } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current).toEqual(RESULTS)
    rerender({ query: 'court st', enabled: false })
    await elapse(SUGGEST_DEBOUNCE_MS * 2)
    expect(result.current).toEqual([])
    expect(suggest).toHaveBeenCalledTimes(1)
  })

  it('a failed lookup clears rather than keeping a stale list', async () => {
    const { result, rerender } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current).toEqual(RESULTS)
    suggest.mockRejectedValue(new TypeError('network down'))
    rerender({ query: 'court str', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current).toEqual([])
  })
})
