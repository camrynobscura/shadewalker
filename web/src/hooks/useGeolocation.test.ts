import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useGeolocation } from './useGeolocation'

// jsdom has no real Geolocation implementation -- stub navigator.geolocation
// per test so watchPosition's success/error callbacks can be invoked
// directly, simulating what a real browser would call them with.
describe('useGeolocation', () => {
  const watchPosition = vi.fn()
  const clearWatch = vi.fn()

  beforeEach(() => {
    watchPosition.mockReset()
    clearWatch.mockReset()
    Object.defineProperty(navigator, 'geolocation', {
      value: { watchPosition, clearWatch },
      configurable: true,
    })
  })

  afterEach(cleanup)

  it('never subscribes while disabled, and returns null position and error', () => {
    const { result } = renderHook(() => useGeolocation(false))
    expect(watchPosition).not.toHaveBeenCalled()
    expect(result.current).toEqual({ position: null, error: null })
  })

  it('resolves to a position once the success callback fires', () => {
    watchPosition.mockImplementation((success: (pos: unknown) => void) => {
      success({ coords: { latitude: 40.68, longitude: -73.99, accuracy: 12 } })
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current.position).toEqual({ lat: 40.68, lon: -73.99, accuracy: 12 })
    expect(result.current.error).toBeNull()
  })

  it('maps a code-1 failure to the denied error (and no position)', () => {
    watchPosition.mockImplementation((_s: unknown, error: (e: unknown) => void) => {
      error({ code: 1 })
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current).toEqual({ position: null, error: 'denied' })
  })

  it('maps any other failure to unavailable', () => {
    watchPosition.mockImplementation((_s: unknown, error: (e: unknown) => void) => {
      error({ code: 3 }) // TIMEOUT
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current).toEqual({ position: null, error: 'unavailable' })
  })

  it('a fix after a failure clears the error — watchPosition keeps trying', () => {
    let successCb: (pos: unknown) => void = () => {}
    watchPosition.mockImplementation((success: typeof successCb, error: (e: unknown) => void) => {
      successCb = success
      error({ code: 3 })
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current.error).toBe('unavailable')
    act(() => successCb({ coords: { latitude: 40.68, longitude: -73.99, accuracy: 9 } }))
    expect(result.current.error).toBeNull()
    expect(result.current.position).not.toBeNull()
  })

  it('reports unavailable when the browser has no geolocation at all', () => {
    // The old code returned early here, leaving callers waiting forever.
    Object.defineProperty(navigator, 'geolocation', { value: undefined, configurable: true })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current).toEqual({ position: null, error: 'unavailable' })
  })

  it('clears the watch when disabled again', () => {
    watchPosition.mockReturnValue(42)
    const { rerender } = renderHook(({ enabled }) => useGeolocation(enabled), {
      initialProps: { enabled: true },
    })
    rerender({ enabled: false })
    expect(clearWatch).toHaveBeenCalledWith(42)
  })
})
