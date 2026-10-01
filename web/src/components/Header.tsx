import { useEffect, useId, useRef, useState } from 'react'
import { InfoIcon } from './icons'
import styles from './Header.module.css'

/* Whether this page load gets the cursor's three-blink hello. Map page
   only, and once per tab session: every page here is a full navigation,
   so without the sessionStorage memory the blink would re-fire on About
   and on every return trip. Module-level memo, not state: computed once
   per page load, stable across re-renders (and StrictMode's
   double-invocations, which would otherwise consume the flag before the
   real render read it). try/catch because storage access can throw
   (private modes); the fallback just blinks on every map load. */
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

/* Phones only (CSS): the ⓘ opens what the desktop header says beside
   the wordmark, which phones hide to leave room for the panel, plus
   the way to About. A tap-to-open disclosure, not a hover
   tooltip: phones have no hover. It closes on the ⓘ, Escape, or a tap
   anywhere else -- which the backdrop takes, so that tap can't also drop
   a route point on the map. Rendered only while open, so its copy of
   the tagline never doubles the desktop one; aria-controls follows it. */
function AboutPopover() {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open])

  return (
    <div
      className={styles.aboutPopover}
      // Tabbing out closes it. A null relatedTarget is ignored: Safari
      // doesn't focus a tapped link, and closing then would swallow the tap.
      onBlur={(e) => {
        if (e.relatedTarget && !e.currentTarget.contains(e.relatedTarget)) setOpen(false)
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        className={styles.infoButton}
        aria-label="About Shade Walker"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((wasOpen) => !wasOpen)}
      >
        <InfoIcon />
      </button>
      {open && <div className={styles.infoBackdrop} aria-hidden="true" onClick={() => setOpen(false)} />}
      {open && (
        <div id={panelId} className={styles.infoPanel}>
          <p>
            <span aria-hidden="true">&#62; </span>find the shadiest walking route in NYC
          </p>
          <p>
            <span aria-hidden="true">&#62; </span>tap the map or enter two addresses below to find your route
          </p>
          <a className={styles.infoAbout} href="/about.html">
            <span>
              About Shade Walker <span aria-hidden="true">→</span>
            </span>
          </a>
        </div>
      )}
    </div>
  )
}

/* The one site header, shared by the map page and About (About is a
   second React entry for exactly this component, so the two headers
   can't drift apart). The `page` prop carries every difference between
   the two renderings:
   - the map page's wordmark is that page's <h1>; About has a real h1 of
     its own ("About Shade Walker"), so its wordmark is a styled <p> —
     one h1 per page;
   - the tagline/instructions block is map-page-only;
   - the nav link is reciprocal: ABOUT on the map page, MAP on About —
     with two pages, the nav names the other destination. */
export function Header({ page }: { page: 'map' | 'about' }) {
  const onMap = page === 'map'
  const Wordmark: 'h1' | 'p' = onMap ? 'h1' : 'p'
  return (
    <header className={styles.header}>
      <Wordmark className={styles.title}>
        {/* The wordmark is a home link -- clicking it navigates to "/"
            (no query params), the app's default state, which clears any
            route. A full navigation, not an in-place clear, so it also
            resets the map center and zoom to default -- a true reset.
            Twin spans, not aria-label: VoiceOver reads "Shade_walker" as
            one mushed word, so the spoken form drops the underscore while
            the screen keeps it — and an aria-label here would become the
            name of the page's h1 ("Shade Walker, home" in every headings
            list). Text content names the link and the heading the same
            way. */}
        <a href="/" className={styles.homeLink}>
          <span aria-hidden="true">Shade_walker</span>
          <span className={styles.visuallyHidden}>Shade Walker</span>
          {/* Decorative terminal cursor — never announced. */}
          <span
            className={cursorShouldBlink(onMap) ? `${styles.cursor} ${styles.cursorBlink}` : styles.cursor}
            aria-hidden="true"
          />
        </a>
      </Wordmark>
      {/* Tagline + instructions ride beside the wordmark (three stacked
          lines of height for text that earns one row). Siblings of the
          wordmark, not inside it: the accessible heading stays just the
          wordmark. The whole block is map-page-only: About's header is
          just wordmark + nav. On the map the instructions line never
          toggles on route state -- hiding it when a route exists flickers
          on every click. */}
      {onMap && (
        <div className={styles.headerText}>
          <p className={styles.tagline}>
            {/* The prompt glyph is decoration -- unspoken, or every read
                starts with "greater than". */}
            <span aria-hidden="true">&#62; </span>find the shadiest walking route in NYC
          </p>
          <p className={styles.instructions}>
            <span aria-hidden="true">&#62; </span>tap the map to set a start and end point, or search two
            addresses below
          </p>
        </div>
      )}
      {/* A real navigation, not a bare link: a full page of its own
          deserves the landmark. margin-left auto rides the header's
          flex row to the right edge. Reciprocal: each page links to the
          other one. The map page renders both forms and CSS swaps them at
          720px: the ABOUT word on desktop, the ⓘ and its pop-up on phones
          (the word crowded the wordmark there: it wrapped below it at iOS
          110% zoom). Both are named "About Shade Walker", containing the
          visible "About" (WCAG 2.5.3). About's MAP link is a document's
          way back and stays text at every width. */}
      <nav className={styles.headerNav} aria-label="Site">
        {onMap ? (
          <>
            <a
              className={`${styles.navLink} ${styles.aboutWord}`}
              href="/about.html"
              aria-label="About Shade Walker"
            >
              {/* One run of text, as in the pop-up: the underline carries
                through the space to the arrow (a flex gap would break it). */}
              <span>
                ABOUT <span aria-hidden="true">→</span>
              </span>
            </a>
            <AboutPopover />
          </>
        ) : (
          <a className={styles.navLink} href="/">
            <span>
              MAP <span aria-hidden="true">→</span>
            </span>
          </a>
        )}
      </nav>
    </header>
  )
}
