import styles from './Header.module.css'

/* The one site header, shared by the map page and About (user call
   2026-09-01 — the two pages' headers had already drifted apart, and
   every redesign would have had to land twice; About became a second
   React entry for exactly this component). The `page` prop carries
   every difference between the two renderings:
   - the map page's wordmark is that page's <h1>; About has a real h1 of
     its own ("About Shade Walker"), so its wordmark is a styled <p> —
     one h1 per page;
   - the "tap the map…" instructions line describes the app screen and
     is absent on About;
   - the ABOUT link self-links on About and carries aria-current, so
     assistive tech (and any future styling) knows it's the current
     page (user call 2026-09-01: same link on both pages, no MAP swap —
     the wordmark and the footer already link back to the map). */
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
          <span className={styles.cursor} aria-hidden="true" />
        </a>
      </Wordmark>
      {/* Tagline + instructions ride BESIDE the wordmark (user call
          2026-08-30: the header was spending three stacked lines of
          height on text that earns one row). Siblings of the wordmark,
          not inside it: the accessible heading stays just the wordmark. */}
      <div className={styles.headerText}>
        <p className={styles.tagline}>
          {/* The prompt glyph is decoration -- unspoken, or every read
              starts with "greater than" (VoiceOver pass, 2026-08-31). */}
          <span aria-hidden="true">&#62; </span>find the shadiest walking route in NYC
        </p>
        {/* On the map page this never toggles on route state -- it once
            hid itself when a route existed, and re-picking a point made
            it flicker in and out, shifting the layout on every click.
            On About it's absent by design: it instructs a screen that
            isn't there. */}
        {onMap && (
          <p className={styles.instructions}>
            <span aria-hidden="true">&#62; </span>tap the map to set a start and end point, or search two addresses below
          </p>
        )}
      </div>
      {/* A real navigation, not a bare link: a full page of its own
          deserves the landmark. margin-left auto rides the header's
          flex row to the right edge. */}
      <nav className={styles.headerNav} aria-label="Site">
        <a
          className={styles.aboutLink}
          href="/about.html"
          aria-current={onMap ? undefined : 'page'}
        >
          ABOUT
        </a>
      </nav>
    </header>
  )
}
