/** Test-only: a fake request that never answers and rejects only when its
 * signal aborts — the way a browser's fetch does, with the abort reason (a
 * DOMException).
 *
 * The one difference it irons out: in this test environment (jsdom under
 * Vitest) a plain `abort()`'s reason is a DOMException from another copy
 * of the class, so `err instanceof DOMException` — how the hooks tell a
 * cancel from a failure — is false there, and a cancel would read as a
 * network failure. Such a reason is rebuilt as the global DOMException,
 * same name and message. One that already is (the route hook's own
 * timeout) passes through untouched. */
export function rejectOnAbort(signal: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    function rejectWithReason() {
      const reason: unknown = signal.reason
      if (reason instanceof DOMException) {
        reject(reason)
      } else {
        const { name, message } = reason as { name: string; message: string }
        reject(new DOMException(message, name))
      }
    }
    if (signal.aborted) rejectWithReason()
    else signal.addEventListener('abort', rejectWithReason)
  })
}
