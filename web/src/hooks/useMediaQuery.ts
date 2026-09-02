import { useEffect, useState } from 'react'

/** Reactive matchMedia: re-renders when the query flips (rotation, window
 * resize). Used to gate behavior — not styling, which stays in CSS — on
 * the same breakpoints the stylesheets use. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)

  useEffect(() => {
    const mql = window.matchMedia(query)
    setMatches(mql.matches) // re-sync in case `query` changed between renders
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}

/* The app's one layout breakpoint — keep in sync with the @media rules in
   App.module.css, Header.module.css, and Controls.module.css, which hold
   the same 720px literal (CSS can't export it). */
export const MOBILE_LAYOUT_QUERY = '(max-width: 720px)'
