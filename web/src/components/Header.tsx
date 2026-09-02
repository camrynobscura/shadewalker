import styles from './Header.module.css'

/* Whether this page load gets the cursor's three-blink hello. Map page
   only, and once per TAB SESSION (user call 2026-09-01): every page here
   is a full navigation, so without the sessionStorage memory the blink
   re-fired on About and on every return trip. Module-level memo, not
   state: computed once per page load, stable across re-renders (and
   StrictMode's double-invocations, which would otherwise consume the
   flag before the real render read it). try/catch because storage
   access can throw (private modes); the fallback blinks per map load,
   which is the pre-fix behavior minus About. */
let blinkDecision: boolean | null = null
function cursorShouldBlink(onMap: boolean): boolean {
  if (!onMap) return false
  if (blinkDecision === null) {
    try {
      blinkDecision = sessionStorage.getItem('sw-cursor-blinked') === null
      if (blinkDecision) sessionStorage.setItem('sw-cursor-blinked', '1')
    } catch {
      blinkDecision = true
    }
  }
  return blinkDecision
}

/* The one site header, shared by the map page and About (user call
   2026-09-01 — the two pages' headers had already drifted apart, and
   every redesign would have had to land twice; About became a second
   React entry for exactly this component). The `page` prop carries
   every difference between the two renderings:
   - the map page's wordmark is that page's <h1>; About has a real h1 of
     its own ("About Shade Walker"), so its wordmark is a styled <p> —
     one h1 per page;
   - the tagline/instructions block is map-page-only;
   - the nav link is reciprocal (user call 2026-09-01, revisiting the
     same-day same-link call): ABOUT on the map page, MAP on About —
     with two pages, the nav names the OTHER destination. */
export function Header({ page }: { page: 'map' | 'about' }) {
  const onMap = page === 'map'
  const Wordmark: 'h1' | 'p' = onMap ? 'h1' : 'p'
  return (
    <header className={styles.header}>
      <Wordmark className={styles.title}>
        {/* The wordmark is a home link -- clicking it navigates to "/"
            (no query params), the app's default state, which clears any
            route (user call 2026-08-31). A full navigation, not an
            in-place clear, so it also resets the map center and zoom to
            default -- a true reset. aria-label: VoiceOver reads
            "Shade_walker" as one mushed word; the label speaks it as two
            while the screen keeps the underscore. */}
        <a href="/" className={styles.homeLink} aria-label="Shade Walker, home">
          Shade_walker
          {/* Decorative terminal cursor — never announced. */}
          <span
            className={
              cursorShouldBlink(onMap)
                ? `${styles.cursor} ${styles.cursorBlink}`
                : styles.cursor
            }
            aria-hidden="true"
          />
        </a>
      </Wordmark>
      {/* Tagline + instructions ride BESIDE the wordmark (user call
          2026-08-30: the header was spending three stacked lines of
          height on text that earns one row). Siblings of the wordmark,
          not inside it: the accessible heading stays just the wordmark.
          The whole block is map-page-only (user call 2026-09-01): About's
          header is just wordmark + nav. On the map the instructions line
          never toggles on route state -- it once hid itself when a route
          existed and flickered on every click. */}
      {onMap && (
        <div className={styles.headerText}>
          <p className={styles.tagline}>
            {/* The prompt glyph is decoration -- unspoken, or every read
                starts with "greater than" (VoiceOver pass, 2026-08-31). */}
            <span aria-hidden="true">&#62; </span>find the shadiest walking route in NYC
          </p>
          <p className={styles.instructions}>
            <span aria-hidden="true">&#62; </span>tap the map to set a start and end point, or search two addresses below
          </p>
        </div>
      )}
      {/* A real navigation, not a bare link: a full page of its own
          deserves the landmark. margin-left auto rides the header's
          flex row to the right edge. Reciprocal: each page links to the
          other one. */}
      <nav className={styles.headerNav} aria-label="Site">
        {onMap ? (
          <a className={styles.navLink} href="/about.html">
            ABOUT
          </a>
        ) : (
          <a className={styles.navLink} href="/">
            MAP
          </a>
        )}
      </nav>
    </header>
  )
}
