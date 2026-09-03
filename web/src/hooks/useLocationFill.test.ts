import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLocationFill } from './useLocationFill'

// The accuracy gate and settle timeout are pure timing/threshold logic —
// exactly what Playwright can't exercise (its mock geolocation grants one
// fixed position) and what froze a coarse cell-tower fix into the start
// point on a real phone (field test, 2026-09-02). Same stubbing approach
// as useGeolocation.test.ts, one layer up.
describe('useLocationFill', () => {
  const watchPosition = vi.fn()
  const clearWatch = vi.fn()
  let successCb: (pos: unknown) => void
  let errorCb: (err: unknown) => void

  const fix = (accuracy: number) => ({
    coords: { latitude: 40.68, longitude: -73.99, accuracy },
  })

  beforeEach(() => {
    vi.useFakeTimers()
    watchPosition.mockReset().mockImplementation((success: typeof successCb, error: typeof errorCb) => {
      successCb = success
      errorCb = error
      return 1
    })
    clearWatch.mockReset()
    Object.defineProperty(navigator, 'geolocation', {
      value: { watchPosition, clearWatch },
      configurable: true,
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('starts idle and never subscribes until requested', () => {
    const { result } = renderHook(() => useLocationFill(vi.fn()))
    expect(result.current.status).toBe('idle')
    expect(watchPosition).not.toHaveBeenCalled()
  })

  it('fills from the first fix that passes the accuracy gate, not the first fix', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    expect(result.current.status).toBe('acquiring')

    act(() => successCb(fix(300))) // cell-tower grade — must NOT fill
    expect(onFill).not.toHaveBeenCalled()
    expect(result.current.status).toBe('acquiring')

    act(() => successCb(fix(30)))
    expect(onFill).toHaveBeenCalledExactlyOnceWith({ lat: 40.68, lon: -73.99, accuracy: 30 })
    expect(result.current.status).toBe('ready')
  })

  it('never chases later fixes after filling — retargeting is a new request', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    act(() => successCb(fix(30)))
    act(() => successCb(fix(5))) // better fix later: start stays put
    expect(onFill).toHaveBeenCalledTimes(1)

    act(() => result.current.request()) // the deliberate re-tap
    expect(onFill).toHaveBeenCalledTimes(2)
  })

  it('a re-request with a warm passing fix fills immediately', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    act(() => successCb(fix(20)))
    expect(onFill).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('ready')

    act(() => result.current.request())
    // No new fix needed — the gate effect re-runs against the position in
    // hand, still inside this act.
    expect(onFill).toHaveBeenCalledTimes(2)
    expect(result.current.status).toBe('ready')
  })

  it('falls back to the best coarse fix at the settle timeout', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    act(() => successCb(fix(300)))
    act(() => successCb(fix(120))) // best so far, still over the gate
    act(() => successCb(fix(180)))
    expect(onFill).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(10_000))
    expect(onFill).toHaveBeenCalledExactlyOnceWith({ lat: 40.68, lon: -73.99, accuracy: 120 })
    expect(result.current.status).toBe('ready')
  })

  it('with no fix at all by the deadline, the first one to arrive fills', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    act(() => vi.advanceTimersByTime(15_000))
    expect(onFill).not.toHaveBeenCalled() // nothing to fill from yet

    act(() => successCb(fix(400))) // past the deadline: anything beats nothing
    expect(onFill).toHaveBeenCalledExactlyOnceWith({ lat: 40.68, lon: -73.99, accuracy: 400 })
  })

  it('an error ends the pending fill with the denied status, not eternal acquiring', () => {
    const onFill = vi.fn()
    const { result } = renderHook(() => useLocationFill(onFill))
    act(() => result.current.request())
    act(() => errorCb({ code: 1 }))
    expect(result.current.status).toBe('denied')
    expect(onFill).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(60_000)) // and the timeout can't resurrect it
    expect(onFill).not.toHaveBeenCalled()
  })
})
