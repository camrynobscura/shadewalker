import { useCallback, useSyncExternalStore } from 'react'

/** Reactive matchMedia: re-renders when the query flips (rotation, window
 * resize). Used to gate behavior — not styling, which stays in CSS — on
 * the same breakpoints the stylesheets use. useSyncExternalStore is
 * React's hook for reading a value that lives outside React: it reads
 * the answer on every render, so a changed `query` is never a render
 * behind. */
export function useMediaQuery(query: string): boolean {
  // Stable per query — a new subscribe function makes React re-subscribe.
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    [query],
  )
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches)
}

/* The app's one layout breakpoint — keep in sync with the @media rules in
   App.module.css, Header.module.css, and Controls.module.css, which hold
   the same 720px literal (CSS can't export it). */
export const MOBILE_LAYOUT_QUERY = '(max-width: 720px)'
