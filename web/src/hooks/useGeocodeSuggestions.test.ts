import { cleanup, renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { GeocodeResult } from '../api'
import { rejectOnAbort } from '../test/rejectOnAbort'
import { SUGGEST_DEBOUNCE_MS, useGeocodeSuggestions } from './useGeocodeSuggestions'

// Mock the network boundary, not fetch itself -- suggest() is api.ts's
// concern (covered in api.test.ts); this file owns the debounce, answer
// ordering, remembering and enabled logic layered on top of it.
const { suggest } = vi.hoisted(() => ({ suggest: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  suggest,
}))

const RESULTS: GeocodeResult[] = [
  { lat: 40.68, lon: -73.998, label: '250 Court St, Brooklyn' },
  { lat: 40.6865, lon: -73.9922, label: 'Court St & Baltic St, Brooklyn' },
]
const NEWER: GeocodeResult[] = [{ lat: 40.69, lon: -73.99, label: 'Court Street, Brooklyn' }]

/** A lookup the test answers by hand, to control the order answers land in. */
function deferred() {
  let resolve!: (results: GeocodeResult[]) => void
  const promise = new Promise<GeocodeResult[]>((res) => (resolve = res))
  return { promise, resolve }
}

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
    expect(result.current.searching).toBe(true)
    await elapse(1)
    expect(suggest).toHaveBeenCalledTimes(1)
    expect(result.current).toEqual({ suggestions: RESULTS, searching: false })
  })

  it('never fetches below the minimum length (whitespace does not count)', async () => {
    const { result } = render({ query: '  co  ', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS * 2)
    expect(suggest).not.toHaveBeenCalled()
    expect(result.current).toEqual({ suggestions: [], searching: false })
  })

  it('a fresh keystroke inside the window replaces the lookup that was about to be sent', async () => {
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
    expect(result.current.suggestions).toEqual(RESULTS)
    rerender({ query: 'court st', enabled: false })
    await elapse(SUGGEST_DEBOUNCE_MS * 2)
    expect(result.current).toEqual({ suggestions: [], searching: false })
    expect(suggest).toHaveBeenCalledTimes(1)
  })

  it('an answer for earlier text lands while typing goes on, and the list is still catching up', async () => {
    const first = deferred()
    const second = deferred()
    suggest.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result, rerender } = render({ query: 'cour', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS) // 'cour' is in flight
    rerender({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS) // 'court' is in flight too; 'cour' was not cancelled
    expect(suggest).toHaveBeenCalledTimes(2)
    expect(suggest.mock.calls[0][1].aborted).toBe(false)

    await act(async () => first.resolve(RESULTS))
    expect(result.current).toEqual({ suggestions: RESULTS, searching: true })

    await act(async () => second.resolve(NEWER))
    expect(result.current).toEqual({ suggestions: NEWER, searching: false })
  })

  it('an older answer that lands after a newer one is dropped', async () => {
    const first = deferred()
    const second = deferred()
    suggest.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result, rerender } = render({ query: 'cour', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    rerender({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)

    await act(async () => second.resolve(NEWER))
    await act(async () => first.resolve(RESULTS))
    expect(result.current).toEqual({ suggestions: NEWER, searching: false })
  })

  it('switching off drops a lookup still out: its late answer never shows', async () => {
    const slow = deferred()
    suggest.mockReturnValueOnce(slow.promise)
    const { result, rerender } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    rerender({ query: 'court st', enabled: false })
    expect(suggest.mock.calls[0][1].aborted).toBe(true)
    await act(async () => slow.resolve(RESULTS))
    expect(result.current).toEqual({ suggestions: [], searching: false })
  })

  it('a cancelled lookup is not a failure: nothing is cleared', async () => {
    const { result, rerender } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    suggest.mockImplementation((_query: string, signal: AbortSignal) => rejectOnAbort(signal))
    rerender({ query: 'court str', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS) // in flight, never answers
    expect(result.current).toEqual({ suggestions: RESULTS, searching: true })
  })

  it('text typed before is answered from memory, without another fetch', async () => {
    suggest.mockResolvedValueOnce(RESULTS).mockResolvedValueOnce(NEWER)
    const { result, rerender } = render({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    rerender({ query: 'court s', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current.suggestions).toEqual(NEWER)

    rerender({ query: 'court', enabled: true }) // a backspace
    await elapse(0)
    expect(result.current).toEqual({ suggestions: RESULTS, searching: false })
    expect(suggest).toHaveBeenCalledTimes(2)
  })

  it('an empty answer is not remembered: the same text asks again', async () => {
    suggest.mockResolvedValueOnce([]).mockResolvedValueOnce(NEWER).mockResolvedValueOnce(RESULTS)
    const { result, rerender } = render({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    rerender({ query: 'court s', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    rerender({ query: 'court', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(suggest).toHaveBeenCalledTimes(3)
    expect(result.current.suggestions).toEqual(RESULTS)
  })

  it('a failed lookup clears rather than keeping a stale list', async () => {
    const { result, rerender } = render({ query: 'court st', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current.suggestions).toEqual(RESULTS)
    suggest.mockRejectedValue(new TypeError('network down'))
    rerender({ query: 'court str', enabled: true })
    await elapse(SUGGEST_DEBOUNCE_MS)
    expect(result.current).toEqual({ suggestions: [], searching: false })
  })
})
