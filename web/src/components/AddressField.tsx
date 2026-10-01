import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { flushSync } from 'react-dom'
import { useAddressField } from '../hooks/useAddressField'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from '../hooks/useMediaQuery'
import { ClearIcon } from './icons'
import styles from './Controls.module.css'

/** One labeled address field, now an ARIA combobox: the input plus a
 * listbox — typed-text suggestions, or recent addresses while the
 * focused field is empty — driven by aria-activedescendant (focus never leaves
 * the input; arrows move a highlight instead). Presentational — Controls
 * owns the state through useAddressField, passed whole as `field`
 * because a combobox needs eight pieces of it and threading each as its
 * own prop obscured which field a given prop belonged to. No status
 * message once found: the address is already sitting right there in the
 * input, restating it back as text would just be duplicating what's on
 * screen. */
export function AddressField({
  label,
  marker,
  example,
  field,
  emptyAccessory,
  notice,
}: {
  label: string
  /** The field's map-marker letter ("A" start, "B" end). Rendered as a
   * chip inside the input's left edge, mobile only — where the visible
   * labels are dropped to reclaim panel height, the chip is the field's
   * identity, echoing the map's A/B markers so field and marker read as
   * the same object. Desktop keeps the labels and hides the chip.
   * aria-hidden: the <label> names the input at every width (visually
   * hidden on phones, never display:none). */
  marker: string
  example: string
  field: ReturnType<typeof useAddressField>
  /** Icon button seated inside the input's right edge while the field is
   * empty — the start field's ⌖. Once there's text the slot is the
   * per-field ✕, rendered here rather than composed by Controls because
   * clearing has to hand focus back to the input, and the input's ref
   * lives here. */
  emptyAccessory?: ReactNode
  /** Extra content for the status line — the location error, composed by
   * Controls. The field's own NOT_FOUND wins when both apply: it's the
   * answer to the more recent action (typing beats a parked error). */
  notice?: ReactNode
}) {
  // useId generates a unique, SSR-safe id so <label htmlFor> can point at
  // the input even when the component appears twice on the page.
  const id = useId()
  const listboxId = `${id}-listbox`
  const { options, showingRecents, activeIndex, open } = field
  const spokenLabel = label.replace(/_/g, ' ')

  /* Full-screen search mode (mobile only). The fields sit mid-screen on
     the stacked mobile layout — below where the iOS keyboard's top edge
     lands — so Safari scrolled the whole window to lift a focused field
     into view, exposing bare canvas below the one-screen-tall app.
     Expanding the focused field to a fixed full-screen layer puts the
     input at the top of the screen, so Safari has nothing to scroll for
     — and the suggestion list gets real room, which the squeezed mobile
     panel never had. Same DOM node, same combobox semantics, just
     repositioned: focus never moves, so `expanded` can simply be
     "focused while mobile" — any blur (keyboard
     Done, CANCEL, tabbing away) collapses it, which is also why it needs
     no dialog role or focus trap. */
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const [focused, setFocused] = useState(false)
  const expanded = focused && isMobile
  const inputRef = useRef<HTMLInputElement>(null)
  const fieldRef = useRef<HTMLDivElement>(null)

  /* While the overlay is open the correct window scroll is exactly 0 —
     the input is pinned to the top by design, and nothing at the document
     level legitimately scrolls (the suggestion list scrolls itself). But
     Safari queues its keyboard scroll-into-view against the field's
     pre-expansion position and lands it asynchronously, after both the
     re-layout and any one-shot reset — a field tapped low in a scrolled
     panel leaves the whole overlay shoved out of view that way. So pin
     for the overlay's whole lifetime: any scroll that appears while it's
     open gets put back, whenever it lands. */
  useEffect(() => {
    if (!expanded) return
    const pin = () => {
      if (window.scrollY !== 0 || window.scrollX !== 0) window.scrollTo(0, 0)
    }
    pin()
    window.addEventListener('scroll', pin)
    // Keyboard-avoidance can also move the visual viewport without a
    // window scroll event; its own scroll/resize events catch that path.
    const vv = window.visualViewport
    vv?.addEventListener('scroll', pin)
    vv?.addEventListener('resize', pin)
    return () => {
      window.removeEventListener('scroll', pin)
      vv?.removeEventListener('scroll', pin)
      vv?.removeEventListener('resize', pin)
    }
  }, [expanded])

  /* Recents open whenever the field is focused and empty, however it got
     that way — a fresh focus, the ✕ (whose mousedown preventDefault
     keeps focus in the field), select-all-delete. State-driven rather
     than hung off the focus event so every emptying path behaves the
     same; on mobile it's what keeps the overlay from stranding an
     emptied field with no list and no ArrowDown key to reopen one. With
     nothing stored this renders nothing (`open` needs options), and
     Escape still dismisses: closing changes neither dep, so the effect
     doesn't refire. openSuggestions deliberately not a dep — its
     identity changes per render, and refiring on it would reload recents
     into fresh state every render. */
  useEffect(() => {
    if (focused && field.query === '') field.openSuggestions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focused, field.query])

  /* Whether the next blur should skip acting on typed text (CANCEL,
     Escape) — a ref, not state: it's consumed by the blur handler in the
     same interaction, never rendered. */
  const abandonRef = useRef(false)

  /** Collapse the overlay (and the on-screen keyboard with it).
   * `abandon` marks the blur as a walk-away, so half-typed text isn't
   * geocoded on the way out.
   *
   * The blur is what closes the keyboard, so focus can't go back to the
   * input — that would reopen both. It goes to the field's own box
   * instead (tabIndex -1: script-focusable, never a tab stop): a div
   * summons no keyboard, so the reading/tab position stays at the field
   * just edited rather than dropping to <body>, where the next Tab
   * restarts from the top of the page and VoiceOver loses its place.
   * preventScroll: the panel must not jump as the overlay leaves. */
  function collapse(abandon = false) {
    abandonRef.current = abandon
    inputRef.current?.blur()
    fieldRef.current?.focus({ preventScroll: true })
  }

  /* Expand before focus, not in response to it. On focus, Safari computes
     its keyboard scroll-into-view against the field's position at that
     instant — and depending on iOS version it delivers that move as a
     window scroll (the pin above catches it) or as a pure visual-viewport
     pan that no script can undo. Beating both: on touchstart — which fires before
     any focus — flushSync the expanded layout in, then focus the input
     synchronously (still inside the user gesture, so the keyboard still
     opens). By the time Safari measures, the input is already at the top
     of the screen and there is nothing to avoid. */
  function onTouchStart() {
    if (!isMobile || focused) return
    flushSync(() => setFocused(true))
    inputRef.current?.focus()
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    // Enter with no highlighted option resolves the typed text. Handled
    // here, not via form submission: the fields sit in no <form> (see
    // Controls), so Enter (and iOS's Go key, which arrives as Enter)
    // would otherwise silently do nothing. In full-screen
    // mode also close the keyboard, so the route appears on a fully
    // visible map instead of behind the overlay.
    if (e.key === 'Enter' && activeIndex < 0) {
      e.preventDefault()
      void field.resolve()
      if (expanded) collapse()
      return
    }
    if (!open) {
      if (e.key === 'ArrowDown') field.openSuggestions()
      if (e.key === 'Escape' && expanded) collapse(true) // walk away; don't geocode leftovers
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault() // keep the caret still; the arrow moves the highlight
      field.setActiveIndex((activeIndex + 1) % options.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      field.setActiveIndex(activeIndex <= 0 ? options.length - 1 : activeIndex - 1)
    } else if (e.key === 'Enter' && activeIndex >= 0) {
      e.preventDefault() // pick the highlighted option instead of submitting the form
      field.selectSuggestion(options[activeIndex])
      if (expanded) collapse() // a picked address ends the search session
    } else if (e.key === 'Escape') {
      field.closeSuggestions()
    }
  }

  return (
    <div
      ref={fieldRef}
      tabIndex={-1} /* collapse() parks focus here — see it for why */
      className={expanded ? `${styles.addressField} ${styles.fieldExpanded}` : styles.addressField}
      /* While expanded, a press on the overlay's dead space must not
         steal focus and collapse the session — the same preventDefault
         the options and CANCEL use, widened to the container. The input
         itself is exempted so its own mousedown still places the caret.
         This also absorbs the browser's synthesized mouse events that
         trail a touch tap and land at pre-expansion coordinates (they
         would collapse the overlay the instant it opened). */
      onMouseDown={(e) => {
        if (expanded && e.target !== inputRef.current) e.preventDefault()
      }}
    >
      {/* display:contents when collapsed, so the label lays out exactly as
          it always did as a direct flex child; as a flex row only when
          expanded, to seat CANCEL beside it. */}
      <div className={expanded ? styles.expandedHead : styles.fieldHead}>
        {/* The label is the input's accessible name (native <label>, no
            aria-label on the input). Twin spans, the app's one technique
            for the terminal voice: the screen shows "Start_point", the
            spoken form drops the underscore ("Start underscore point"
            otherwise). Same split as the Shade_walker wordmark and the
            Shade_priority legend. */}
        <label htmlFor={id} className={styles.fieldLabel}>
          <span aria-hidden="true">{label}</span>
          <span className={styles.visuallyHidden}>{spokenLabel}</span>
        </label>
        {expanded && (
          /* mousedown preventDefault: same trick as the options below —
             keep the tap from blurring the input first, so this click is
             the one deliberate collapse, not a blur race. */
          <button
            type="button"
            className={styles.cancelSearch}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => collapse(true)}
          >
            CANCEL
          </button>
        )}
      </div>
      <div className={styles.suggestWrap}>
        {/* Before the input in DOM order but painted over it (absolute) —
            see .fieldMarker for the mobile-only visibility. */}
        <span className={styles.fieldMarker} aria-hidden="true">
          {marker}
        </span>
        <input
          id={id}
          ref={inputRef}
          className={styles.addressInput}
          type="text"
          value={field.query}
          // Off, not "street-address": the browser's own autofill dropdown
          // would paint directly over our listbox, and the ARIA combobox
          // pattern expects native autocomplete disabled.
          autoComplete="off"
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          // Both only while open: axe flags aria-controls/-activedescendant
          // ids that don't resolve to a rendered element.
          aria-controls={open ? listboxId : undefined}
          aria-activedescendant={open && activeIndex >= 0 ? `${id}-opt-${activeIndex}` : undefined}
          onChange={(e) => field.onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onTouchStart={onTouchStart}
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false)
            const abandoned = abandonRef.current
            abandonRef.current = false
            field.onBlur(abandoned)
          }}
        />
        {field.query !== '' ? (
          /* The in-field ✕: clears one field — and with it that field's
             point and marker. mousedown preventDefault so a tap neither
             steals focus nor, on mobile, reads as a reason to expand or
             collapse the search — clearing is an edit, not a session
             boundary. That same preventDefault means only a keyboard
             activation ever has the button itself focused, and that's
             the case that needs help: the button unmounts as it clears,
             which would drop focus to <body>. Hand it back to the input;
             pointer users never had it there, so nothing moves for
             them. */
          <button
            type="button"
            className={styles.fieldAccessory}
            aria-label={`Clear ${spokenLabel.toLowerCase()}`}
            onMouseDown={(e) => e.preventDefault()}
            onClick={(e) => {
              const viaKeyboard = document.activeElement === e.currentTarget
              field.clearField()
              if (viaKeyboard) inputRef.current?.focus()
            }}
          >
            <ClearIcon />
          </button>
        ) : (
          emptyAccessory
        )}
        {/* A fake placeholder: a real one is announced in the value slot
            before the label (skipping into the panel would say "e.g. 250
            Court St" instead of "Start_point"), and the example should be
            visible but entirely unspoken. aria-hidden +
            pointer-events:none makes it pure decoration; rendered only
            while the field is empty, same as the real thing. */}
        {field.query === '' && (
          <span className={styles.fakePlaceholder} aria-hidden="true">
            e.g. {example}
          </span>
        )}
        {open && (
          <ul
            className={styles.suggestList}
            role="listbox"
            id={listboxId}
            aria-label={showingRecents ? `${spokenLabel} recent addresses` : `${spokenLabel} suggestions`}
          >
            {/* role="presentation" + aria-hidden: a listbox may only hold
                options, so this header is visual-only — the listbox's
                aria-label carries "recent addresses" instead. */}
            {showingRecents && (
              <li className={styles.recentHeader} role="presentation" aria-hidden="true">
                // RECENT
              </li>
            )}
            {field.searching && (
              <li className={styles.searchingRow} role="presentation" aria-hidden="true">
                // SEARCHING…
              </li>
            )}
            {options.map((suggestion, i) => (
              <li
                key={`${suggestion.label}-${i}`}
                id={`${id}-opt-${i}`}
                role="option"
                aria-selected={i === activeIndex}
                className={
                  i === activeIndex ? `${styles.suggestOption} ${styles.suggestActive}` : styles.suggestOption
                }
                // mousedown fires before the input's blur — preventing it
                // keeps focus in the field, so blur can't close the list
                // out from under the click that's about to land.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  field.selectSuggestion(suggestion)
                  if (expanded) collapse() // a picked address ends the search session
                }}
                onMouseMove={() => field.setActiveIndex(i)}
              >
                {suggestion.label}
              </li>
            ))}
          </ul>
        )}
        {/* The first answer can take a second or more, so the wait gets a
            line where the list will appear. Not a listbox and unspoken:
            the status line below tells screen readers. */}
        {!open && field.searching && focused && (
          <div className={styles.suggestList} aria-hidden="true">
            <div className={styles.searchingRow}>// SEARCHING…</div>
          </div>
        )}
      </div>
      {/* role="status" = a polite live region: screen readers announce the
          result without stealing focus. While suggestions load it holds
          only a spoken "Searching", so the panel's layout stays still. */}
      <p
        className={
          field.status === 'notfound' || field.status === 'unavailable' || notice
            ? styles.addressStatus
            : styles.addressStatusEmpty
        }
        role="status"
      >
        {field.status === 'notfound' ? (
          <>
            <span aria-hidden="true">// NOT_FOUND:</span>
            <span className={styles.visuallyHidden}>NOT FOUND:</span> try adding a borough
          </>
        ) : field.status === 'unavailable' ? (
          <>
            <span aria-hidden="true">// SEARCH_DOWN:</span>
            <span className={styles.visuallyHidden}>Search down:</span> address search is temporarily
            unavailable — tap the map instead
          </>
        ) : (
          (notice ?? (field.searching && <span className={styles.visuallyHidden}>Searching…</span>))
        )}
      </p>
    </div>
  )
}
