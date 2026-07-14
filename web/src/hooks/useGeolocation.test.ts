import { cleanup, renderHook } from '@testing-library/react'
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

  it('never subscribes while disabled, and returns null', () => {
    const { result } = renderHook(() => useGeolocation(false))
    expect(watchPosition).not.toHaveBeenCalled()
    expect(result.current).toBeNull()
  })

  it('resolves to a position once the success callback fires', () => {
    watchPosition.mockImplementation((success: (pos: unknown) => void) => {
      success({ coords: { latitude: 40.68, longitude: -73.99, accuracy: 12 } })
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current).toEqual({ lat: 40.68, lon: -73.99, accuracy: 12 })
  })

  it('resolves to null if the error callback fires (e.g. permission denied)', () => {
    watchPosition.mockImplementation((_success: unknown, error: () => void) => {
      error()
      return 1
    })
    const { result } = renderHook(() => useGeolocation(true))
    expect(result.current).toBeNull()
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
