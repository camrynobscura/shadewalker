import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { flushSync } from 'react-dom'
import { geocode, GeocodeUnavailableError, reverseGeocode, type GeocodeResult, type Point, type RouteFeature } from '../api'
import { formatCoords, formatDistance, spokenDistance } from '../format'
import type { LocationFillStatus } from '../hooks/useLocationFill'
import { useGeocodeSuggestions } from '../hooks/useGeocodeSuggestions'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from '../hooks/useMediaQuery'
import { ClearIcon, CrosshairIcon } from './icons'
import { compareRoutes, TREE_PRESETS } from '../presets'
import { loadRecents, recordRecent } from '../recents'
import { displayShade, LOW_SHADE_FRACTION } from '../shade'
import styles from './Controls.module.css'

/** Splits a formatted distance ("0.2 mi", "524 ft") into its leading
 * number, highlighted, and its trailing unit, left plain — only the
 * comparison line's actual values get the bold/green treatment, not
 * their units. formatDistance() itself stays a single string everywhere
 * else it's used; this split is local to that one line. */
function highlightNumber(text: string) {
  const match = text.match(/^(-?[\d.]+)(.*)$/)
  if (!match) return text
  return (
    <>
      <span className={styles.numberHighlight}>{match[1]}</span>
      {match[2]}
    </>
  )
}

/** 'unavailable' = the search itself failed (proxy/Photon down, no
 * network) — a different sentence from 'notfound', which is the search
 * working and finding nothing. */
type FieldStatus = 'idle' | 'searching' | 'notfound' | 'unavailable' | 'found'

/** Owns one address field's query/status and how to resolve it. A hook,
 * not a component, because Controls needs two independent copies (start,
 * end) that a single shared "find route" submit can resolve together.
 *
 * `externalPoint` is the other direction: a point that landed in `start`/
 * `end` from outside this field's own resolve() -- a map click, or the
 * USE_LOCATION button -- which this field still needs to show *something*
 * for, even though no address text ever got typed. `shownPointRef` is
 * what tells those two directions apart: it's the point (if any) that the
 * current `query` text already represents, kept in a ref rather than
 * state since updating it must never itself trigger a render. Compared by
 * value against `externalPoint` (not object identity) because a point
 * this field resolved itself round-trips back down through the parent as
 * a new object with the same lat/lon -- reference equality would treat
 * that as "a new point," and redundantly reverse-geocode text that's
 * already better than anything reverse-geocoding would produce. */
function useAddressField(
  onResolve: (p: Point | null) => void,
  externalPoint: Point | null,
  /** Reports the display text whenever it settles into representing the
   * resolved point — a picked suggestion's label, resolved typed text, a
   * reverse-geocode name. App mirrors it to the URL beside the point, so
   * a reload shows the SAME text instead of re-deriving a (often
   * different) name from the bare coordinate. */
  onLabel: (label: string) => void,
  /** The label a reload restored for `externalPoint` (from the URL) —
   * read once, at mount: with it, the field starts out already showing
   * the stored text and the reverse-geocode round trip never happens. */
  initialLabel: string | null,
) {
  const restored = initialLabel !== null && externalPoint !== null
  const [query, setQuery] = useState(restored ? initialLabel : '')
  const [status, setStatus] = useState<FieldStatus>(restored ? 'found' : 'idle')
  // The dropdown is wanted only while the current text is something the
  // user TYPED (suggestions), or while a focused field is EMPTY (recents)
  // -- a suggestion pick, a submit, or a programmatic fill (map click,
  // reverse geocode) all turn this off, so the dropdown never reopens
  // over text this code wrote itself.
  const [suggestOn, setSuggestOn] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  // Read fresh each time the dropdown opens (openSuggestions), so a pick
  // made in the OTHER field is already in this one's list.
  const [recents, setRecents] = useState<GeocodeResult[]>([])
  // Starts as the restored point when a label came back from the URL —
  // that's what stops the mount effect below from reverse-geocoding over
  // the restored text.
  const shownPointRef = useRef<Point | null>(restored ? externalPoint : null)
  // The deliberate entry (suggestion pick / resolved typed text) behind
  // this field's CURRENT point, held until a route completes —
  // commitRecent() records it then. Recents mean "addresses from real
  // routes", not everything ever typed (user call 2026-09-03); nulled
  // whenever the point it described is cleared or replaced from outside.
  // Not set on mount for URL-restored labels: a deliberate one was
  // already recorded when its route first drew.
  const pendingRecentRef = useRef<GeocodeResult | null>(null)
  // Bumped whenever the text an in-flight resolve() was about stops
  // being current (cleared, retyped) — the response is then stale and
  // gets dropped instead of refilling the field (the ✕-mid-lookup
  // resurrection, caught 2026-09-03).
  const resolveSeqRef = useRef(0)
  // Previous externalPoint, so the effect below can tell a point being
  // CLEARED (value -> null) from the steady "no point yet" state while
  // someone types a fresh address.
  const prevExternalRef = useRef<Point | null>(externalPoint)

  const suggestions = useGeocodeSuggestions(query, suggestOn)

  // What the listbox holds right now: recents while the text is empty
  // (the slot that used to show nothing), live suggestions once there's
  // typed text. One list at a time — the keyboard/highlight machinery
  // below only ever sees `options`.
  const showingRecents = query.trim() === ''
  const options = showingRecents ? recents : suggestions

  // A fresh option list starts with nothing highlighted -- keeping an
  // old index would silently point Enter at whatever happens to occupy
  // that position now.
  useEffect(() => {
    setActiveIndex(-1)
  }, [options])

  function onChange(value: string) {
    setQuery(value)
    setStatus('idle')
    setSuggestOn(true)
    shownPointRef.current = null // free-typed text no longer matches any known point
    resolveSeqRef.current++ // any in-flight lookup is about older text now
  }

  /** A picked suggestion already carries its point -- no second geocode
   * round trip on submit; the field behaves exactly as if resolve() had
   * just succeeded with this result. Also serves picking a RECENT (a
   * recent is a stored GeocodeResult) — re-committing one just bumps it
   * back to the front of the list. */
  function selectSuggestion(suggestion: GeocodeResult) {
    setSuggestOn(false)
    setQuery(suggestion.label)
    setStatus('found')
    shownPointRef.current = { lat: suggestion.lat, lon: suggestion.lon }
    pendingRecentRef.current = suggestion
    onResolve({ lat: suggestion.lat, lon: suggestion.lon })
    onLabel(suggestion.label)
  }

  /** Records the pending deliberate entry, if any. Called by Controls
   * the moment a route exists — never before, so a lone entry in one
   * field (or an abandoned one) doesn't reach the recents. One-shot:
   * nulled after recording, so preset switches (which swap `selected`
   * without a new geocode) can't re-record. */
  function commitRecent() {
    if (pendingRecentRef.current) {
      recordRecent(pendingRecentRef.current)
      pendingRecentRef.current = null
    }
  }

  function closeSuggestions() {
    setSuggestOn(false)
  }

  /** Leaving an emptied field drops the point it stood for -- the marker
   * shouldn't outlive the text (user report 2026-08-31). Only on blur,
   * never per-keystroke, so retyping an address doesn't nuke the marker
   * mid-edit. Guarded on externalPoint so tabbing through an
   * already-empty field does nothing.
   *
   * Blur is also where typed text gets geocoded since 2026-09-02 — the
   * FIND_ROUTE button's old job, moved to the moment attention leaves
   * the field (the button was dead weight once suggestion picks and map
   * taps auto-routed). `abandon` skips that: CANCEL and Escape end the
   * mobile search WITHOUT acting on half-typed text. */
  function onBlur(abandon = false) {
    setSuggestOn(false)
    if (query.trim() === '' && externalPoint) {
      shownPointRef.current = null
      onResolve(null)
      return
    }
    if (!abandon) void resolve()
  }

  /** The per-field ✕: text, point, and marker drop together — one field's
   * worth of the old CLEAR_ROUTE (removed 2026-09-02; clearing both is
   * two taps, or the wordmark's full reset). */
  function clearField() {
    setQuery('')
    setStatus('idle')
    setSuggestOn(false)
    shownPointRef.current = null
    pendingRecentRef.current = null
    resolveSeqRef.current++ // a lookup still in flight is for cleared text — drop its answer
    onResolve(null)
  }

  /** Opens the dropdown: ArrowDown on a closed field (ARIA combobox
   * convention — the hook refetches for the unchanged text after its
   * debounce), and AddressField's empty-while-focused effect (recents).
   * Reloads recents each time so the list is fresh however it opens —
   * including a pick just made in the OTHER field. */
  function openSuggestions() {
    setRecents(loadRecents())
    setSuggestOn(true)
  }

  // The status guard blocks a repeat: onChange resets status back to
  // 'idle' on every keystroke, so this only re-fires once there's actually
  // new text to resolve — not every time Enter is pressed again. The one
  // exception is 'unavailable': an outage is worth retrying on the same
  // text, so Enter (or a blur) tries again without retyping.
  async function resolve() {
    setSuggestOn(false) // submitting is the end of the suggestion phase
    if (!query.trim() || (status !== 'idle' && status !== 'unavailable')) return
    setStatus('searching')
    const seq = resolveSeqRef.current
    let result: GeocodeResult | null
    try {
      result = await geocode(query)
    } catch (err) {
      if (!(err instanceof GeocodeUnavailableError)) throw err
      // Same staleness checks as the success path below: an answer about
      // text that's gone, or a point that landed meanwhile, changes nothing.
      if (seq !== resolveSeqRef.current || shownPointRef.current) return
      setStatus('unavailable')
      return
    }
    // The text this lookup was about is gone (✕, retyped, cleared from
    // outside) — the late answer must not refill the field it was
    // cleared out of.
    if (seq !== resolveSeqRef.current) return
    // A point that landed while the lookup was in flight — a map tap, a
    // picked suggestion, USE_LOCATION — supersedes the typed text this
    // resolve started from; drop the response instead of stomping it.
    // (Those paths already set query/status, so bailing leaves the field
    // consistent.) Likelier now that blur triggers resolve (2026-09-02).
    if (shownPointRef.current) return
    if (result) {
      setStatus('found')
      shownPointRef.current = { lat: result.lat, lon: result.lon }
      // The typed text is the pending recent's label too (not the
      // geocoder's), matching what the field keeps showing and the URL
      // restores.
      pendingRecentRef.current = { label: query, lat: result.lat, lon: result.lon }
      onResolve({ lat: result.lat, lon: result.lon })
      // The field keeps showing the TYPED text after a resolve (not the
      // geocoder's label), so that text is what the URL must restore.
      onLabel(query)
    } else {
      setStatus('notfound')
    }
  }

  useEffect(() => {
    const prev = prevExternalRef.current
    prevExternalRef.current = externalPoint

    if (!externalPoint) {
      // Point cleared from outside (CLEAR_ROUTE, or this field emptied and
      // blurred) -- empty the text so field and map never disagree (user
      // report 2026-08-31: CLEAR_ROUTE left the addresses behind). Guarded
      // on `prev` so it fires only on the value->null transition, never on
      // the steady no-point state while a fresh address is being typed.
      // suggestOn is left alone: a blurred field already has it off, and
      // on a still-focused one (the ✕) forcing it off here would close
      // the recents that AddressField's emptied-while-focused effect
      // just opened — this effect runs a render behind it.
      if (prev) {
        setQuery('')
        setStatus('idle')
        shownPointRef.current = null
        pendingRecentRef.current = null
        resolveSeqRef.current++ // any in-flight lookup is for text that just got cleared
      }
      return
    }
    const shown = shownPointRef.current
    if (shown && shown.lat === externalPoint.lat && shown.lon === externalPoint.lon) return

    shownPointRef.current = externalPoint
    pendingRecentRef.current = null // this point wasn't typed or picked — it must not become a recent
    setSuggestOn(false) // the text below is generated, not typed
    // Coordinates first, instantly -- reverse-geocoding is a real network
    // round trip (measured ~70-100ms once warm, up to ~1s on a session's
    // first call), and the field showing nothing while a marker's already
    // on the map would look broken. Also doubles as the fallback if the
    // lookup below fails outright (open water, the geocoder down).
    setQuery(formatCoords(externalPoint))
    setStatus('found')
    reverseGeocode(externalPoint).then((label) => {
      // Bail if a newer point (another click) or free-typed text has since
      // superseded this one -- shownPointRef.current would no longer be
      // this exact object in either case. Without this check, a slow
      // response landing late could stomp on something newer.
      if (label && shownPointRef.current === externalPoint) {
        setQuery(label)
        // Into the URL too: a reload then restores THIS name instantly
        // instead of re-asking the geocoder, whose nearest-thing answer
        // isn't stable call to call (and whose failure mode is showing
        // raw coordinates).
        onLabel(label)
      }
    })
  }, [externalPoint, onLabel])

  return {
    query,
    status,
    options,
    /** Whether `options` is the recents list (empty text) rather than
     * live suggestions — drives the listbox's header row and name. */
    showingRecents,
    activeIndex,
    /** Whether the listbox is rendered: options exist AND the dropdown
     * phase is on (typed text, or a focused empty field). */
    open: suggestOn && options.length > 0,
    onChange,
    resolve,
    selectSuggestion,
    commitRecent,
    setActiveIndex,
    closeSuggestions,
    openSuggestions,
    onBlur,
    clearField,
  }
}

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
function AddressField({
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
   * labels are dropped (user call 2026-09-03, reclaiming panel height)
   * the chip is the field's identity, echoing the map's A/B markers so
   * field and marker read as the same object. Desktop keeps the labels
   * and hides the chip. aria-hidden: the input's aria-label speaks the
   * name at every width. */
  marker: string
  example: string
  field: ReturnType<typeof useAddressField>
  /** Icon button seated inside the input's right edge WHILE THE FIELD IS
   * EMPTY — the start field's ⌖ (2026-09-02, replacing the button row).
   * Once there's text the slot is the per-field ✕, rendered here rather
   * than composed by Controls because clearing has to hand focus back to
   * the input, and the input's ref lives here. */
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
     into view, exposing bare canvas below the one-screen-tall app (the
     "green box", 2026-09-02). Expanding the focused field to a fixed
     full-screen layer puts the input at the TOP of the screen, so Safari
     has nothing to scroll for — and the suggestion list gets real room,
     which the squeezed mobile panel never had. Same DOM node, same
     combobox semantics, just repositioned: focus never moves, so
     `expanded` can simply BE "focused while mobile" — any blur (keyboard
     Done, CANCEL, tabbing away) collapses it, which is also why it needs
     no dialog role or focus trap. */
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const [focused, setFocused] = useState(false)
  const expanded = focused && isMobile
  const inputRef = useRef<HTMLInputElement>(null)

  /* While the overlay is open the correct window scroll is EXACTLY 0 —
     the input is pinned to the top by design, and nothing at the document
     level legitimately scrolls (the suggestion list scrolls itself). But
     Safari queues its keyboard scroll-into-view against the field's
     PRE-expansion position and lands it asynchronously, after both the
     re-layout and any one-shot reset — a field tapped low in a scrolled
     panel left the whole overlay shoved out of view that way (phone,
     2026-09-02). So pin for the overlay's whole lifetime: any scroll that
     appears while it's open gets put back, whenever it lands. */
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

  /* Recents open whenever the field is FOCUSED AND EMPTY, however it got
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

  /* Whether the NEXT blur should skip acting on typed text (CANCEL,
     Escape) — a ref, not state: it's consumed by the blur handler in the
     same interaction, never rendered. */
  const abandonRef = useRef(false)

  /** Collapse the overlay (and the on-screen keyboard with it).
   * `abandon` marks the blur as a walk-away, so half-typed text isn't
   * geocoded on the way out. */
  function collapse(abandon = false) {
    abandonRef.current = abandon
    inputRef.current?.blur()
  }

  /* Expand BEFORE focus, not in response to it. On focus, Safari computes
     its keyboard scroll-into-view against the field's position at that
     instant — and depending on iOS version it delivers that move as a
     window scroll (the pin above catches it) or as a pure visual-viewport
     pan that no script can undo (a scrolled-down panel left the overlay
     shoved out of view on the phone, 2026-09-02, while a newer-iOS
     simulator behaved). Beating both: on touchstart — which fires before
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
    // here, not via form submission: with FIND_ROUTE gone the form has no
    // submit button, and a form with two text inputs and no submit button
    // suppresses implicit submission entirely — Enter (and iOS's Go key,
    // which arrives as Enter) would silently do nothing. In full-screen
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
      className={expanded ? `${styles.addressField} ${styles.fieldExpanded}` : styles.addressField}
      /* While expanded, a press on the overlay's DEAD SPACE must not
         steal focus and collapse the session — the same preventDefault
         the options and CANCEL use, widened to the container. The input
         itself is exempted so its own mousedown still places the caret.
         This also absorbs the browser's synthesized mouse events that
         trail a touch tap and land at pre-expansion coordinates (they
         collapsed the overlay the instant it opened under Playwright's
         tap, 2026-09-02). */
      onMouseDown={(e) => {
        if (expanded && e.target !== inputRef.current) e.preventDefault()
      }}
    >
      {/* display:contents when collapsed, so the label lays out exactly as
          it always did as a direct flex child; as a flex row only when
          expanded, to seat CANCEL beside it. */}
      <div className={expanded ? styles.expandedHead : styles.fieldHead}>
        <label htmlFor={id}>{label}</label>
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
          /* Spoken name drops the underscore ("Start point", not "Start
             underscore point") -- the terminal voice is visual chrome,
             not pronunciation (VoiceOver pass, 2026-08-31). Same split
             as the Shade_walker wordmark. */
          aria-label={spokenLabel}
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
          /* The in-field ✕ (2026-09-02, replacing CLEAR_ROUTE): clears one
             field — and with it that field's point and marker. mousedown
             preventDefault so a tap neither steals focus nor, on mobile,
             reads as a reason to expand or collapse the search — clearing
             is an edit, not a session boundary. That same preventDefault
             means only a KEYBOARD activation ever has the button itself
             focused, and that's the case that needs help: the button
             unmounts as it clears, which dropped focus to <body> (audit
             2026-09-09). Hand it back to the input; pointer users never
             had it there, so nothing moves for them. */
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
        {/* A FAKE placeholder: a real one is announced in the value slot
            before the label (skipping into the panel said "e.g. 250 Court
            St" instead of "Start_point"), and the user wants the example
            visible but entirely unspoken (VoiceOver pass, 2026-08-31).
            aria-hidden + pointer-events:none makes it pure decoration;
            rendered only while the field is empty, same as the real
            thing. */}
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
            {options.map((suggestion, i) => (
              <li
                key={`${suggestion.label}-${i}`}
                id={`${id}-opt-${i}`}
                role="option"
                aria-selected={i === activeIndex}
                className={i === activeIndex ? `${styles.suggestOption} ${styles.suggestActive}` : styles.suggestOption}
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
      </div>
      {/* role="status" = a polite live region: screen readers announce the
          result without stealing focus. Nothing shown for 'searching' —
          the Find_route button's own "FINDING…" label already covers that,
          and showing it here too just flickered on and off per field. */}
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
            <span className={styles.visuallyHidden}>NOT FOUND:</span>
            {' '}try adding a borough
          </>
        ) : field.status === 'unavailable' ? (
          <>
            <span aria-hidden="true">// SEARCH_DOWN:</span>
            <span className={styles.visuallyHidden}>Search down:</span>
            {' '}address search is temporarily unavailable — tap the map instead
          </>
        ) : (
          notice
        )}
      </p>
    </div>
  )
}

interface ControlsProps {
  treeWeight: number
  onTreeWeightChange: (w: number) => void
  /** Current start/end, purely so their address fields can reverse-geocode
   * a point that arrived from outside this component (a map click, or
   * USE_LOCATION) -- not used to control the fields; typed text is still
   * this component's own state. */
  start: Point | null
  end: Point | null
  onSetStart: (p: Point | null) => void
  onSetEnd: (p: Point | null) => void
  /** URL-restored display text for start/end — read once, at mount (the
   * fields own their text after that); see useAddressField's
   * initialLabel. */
  initialStartLabel: string | null
  initialEndLabel: string | null
  /** Report the display text that now represents the start/end point, for
   * the URL mirror. */
  onStartLabel: (label: string) => void
  onEndLabel: (label: string) => void
  /** The ⌖ accessory's whole world: what state to draw, and the one thing
   * a tap does (App owns the arming/gating — see useLocationFill). */
  locationStatus: LocationFillStatus
  onUseLocation: () => void
  /** The currently selected Shade_priority preset's route. */
  selected: RouteFeature | null
  /** The NONE (tree_weight=0) route -- the baseline `selected` is compared
   * against in the comparison line below. */
  baseline: RouteFeature | null
  /** A rejected route (out of coverage, no path found, server down) --
   * shown right above the address fields since that's what it's actually
   * about, and it's where a user's attention already is right after
   * submitting Find_route or tapping the map (the map sits directly above
   * this panel, not down near RouteStats where this used to live). */
  error: string | null
}

export function Controls({
  treeWeight,
  onTreeWeightChange,
  start: startPoint,
  end: endPoint,
  onSetStart,
  onSetEnd,
  initialStartLabel,
  initialEndLabel,
  onStartLabel,
  onEndLabel,
  locationStatus,
  onUseLocation,
  selected,
  baseline,
  error,
}: ControlsProps) {
  // Radios become one group (arrow keys move between them, only one can be
  // checked) by sharing a `name` — useId gives us one that's unique even if
  // this component ever renders twice.
  const groupName = useId()
  const selectedPreset = TREE_PRESETS.find((preset) => preset.value === treeWeight)
  const comparison = selected && baseline ? compareRoutes(selected, baseline) : null
  // The low-shade warning lives HERE, not with the route stats, since
  // 2026-08-31 (user call): Shade_priority is where the remedy is -- turn
  // the dial up and watch whether the warning goes away.
  const lowShade = selected !== null && selected.properties.shade_fraction < LOW_SHADE_FRACTION

  const start = useAddressField(onSetStart, startPoint, onStartLabel, initialStartLabel)
  const end = useAddressField(onSetEnd, endPoint, onEndLabel, initialEndLabel)

  /* Recents are recorded HERE, on route arrival — not at resolve time.
     Only the endpoints of a route that actually drew count as recent
     addresses (user call 2026-09-03): a lone entry used to surface in
     the OTHER field's recents before any route existed, and abandoned
     one-field entries polluted the list. Each field's commit is
     one-shot, so preset switches swapping `selected` re-record nothing.
     commitRecent's identity changes per render and reads refs — deps on
     it would just refire the effect uselessly. */
  useEffect(() => {
    if (!selected) return
    start.commitRecent()
    end.commitRecent()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected])

  /* The start field's empty-state accessory is the location control
     (2026-09-02, replacing the USE_LOCATION button row): "use my location"
     is a start-point affordance, so it lives in the start field — same
     slot AddressField's own ✕ takes over once there's text to clear. One
     tap does the
     whole job since the same day's use-location-ux pass: enable, wait for
     a fix that clears the accuracy gate, fill start, recenter the map —
     the old enable-then-tap-again dance is gone (App's useLocationFill
     owns all of that; this button just reports the tap). Disabled only
     while a fill is actually pending; an error state stays tappable —
     that's the retry — with the explanation in the field's status line
     below. Same ⌖ glyph as the map's own locate button. */
  const locationAccessory =
    locationStatus === 'acquiring' ? (
      <button type="button" className={styles.fieldAccessory} aria-label="Acquiring location" disabled>
        <CrosshairIcon />
      </button>
    ) : (
      <button
        type="button"
        className={styles.fieldAccessory}
        aria-label={locationStatus === 'ready' ? 'Set start point to my location' : 'Use location'}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onUseLocation}
      >
        <CrosshairIcon />
      </button>
    )

  /* The failure half of the location story (useGeolocation swallowed
     every error into a stuck "acquiring" until 2026-09-02). Rendered in
     the start field's status line — the same live region NOT_FOUND uses,
     right under the ⌖ the answer is about. Lives and dies WITH the ⌖:
     once the field has text, the ✕ has taken the slot and location advice
     is stale noise (user call 2026-09-02 — "once you input an address,
     that error should go away"); clearing the field brings both back. */
  const locationNotice =
    start.query !== '' ? null : locationStatus === 'acquiring' ? (
      /* The accuracy gate can wait up to 10s for a location worth
         trusting, and the only other signal is the ⌖ greying out — this
         line makes the silence read as progress, not a hang (the "nothing
         seemed to happen" report, 2026-09-02). */
      <>
        <span aria-hidden="true">{'// ACQUIRING:'}</span>
        <span className={styles.visuallyHidden}>Acquiring:</span> pinpointing your location…
      </>
    ) : locationStatus === 'denied' ? (
      <>
        <span aria-hidden="true">{'// LOCATION_OFF:'}</span>
        <span className={styles.visuallyHidden}>Location off:</span> allow location for this site in
        your browser settings
      </>
    ) : locationStatus === 'unavailable' ? (
      <>
        <span aria-hidden="true">{'// NO_LOCATION:'}</span>
        {/* No ⌖ glyph in copy — it's tofu in iOS's mono fallback (the
            whole reason icons.tsx exists). "Location", never "fix" — GPS
            jargon (user call 2026-09-02). */}
        <span className={styles.visuallyHidden}>No location:</span> couldn&#39;t find your location —
        tap the location button to retry
      </>
    ) : null

  return (
    <>
      {/* First of the panel's three top-level sections -- no divider above
          it (nothing to divide from but the panel's own top edge), unlike
          the two below. */}
      <div className={styles.addressGroup}>
        {/* The alert REGION stays mounted; only its text is conditional.
            role="alert" (assertive) only announces content appearing in a
            live region that already existed -- mounting the whole <p> on
            error, as this used to, meant VoiceOver never caught it,
            worst on a URL-loaded out-of-coverage route (the region was
            inserted already-populated, so there was no observed change
            to announce). Same fix + reasoning as RouteStats' wrapper.
            The empty <p> collapses to zero height, so no dead space. */}
        <p className={error ? styles.error : styles.errorEmpty} role="alert">
          {error && (
            <>
              {/* Same "// TITLE:" prefix as the low-shade note (aria-hidden
                  glyph + sr-only clean words), so this alert reads in the
                  app's own voice (user call 2026-09-01). Covers every message
                  in this slot: out-of-coverage, same start/end, no route,
                  server down. */}
              <strong>
                <span aria-hidden="true">// ERROR:</span>
                <span className={styles.visuallyHidden}>Error:</span>
              </strong>{' '}
              {error}
            </>
          )}
        </p>
        {/* No FIND_ROUTE button and no form since 2026-09-02: routes
            auto-compute the moment both points exist (suggestion picks,
            map taps), typed text resolves on Enter (handled in
            AddressField's keydown — a two-input form with no submit
            button gets no implicit submission) and on blur. A plain div:
            keeping a <form> that can never submit would be lying to
            assistive tech. */}
        <div className={styles.addressFields}>
          {/* Both examples verified against /geocode (2026-09-01): each
              resolves to the right spot in the Village, inside the landing
              view. Tempting alternatives fail silently -- "45 Charles St"
              lands in Alden Manor, "99 Perry St" on Staten Island. */}
          <AddressField
            label="Start_point"
            marker="A"
            example="Washington Square Park"
            field={start}
            emptyAccessory={locationAccessory}
            notice={locationNotice}
          />
          <AddressField label="End_point" marker="B" example="24 East 7th St" field={end} />
        </div>
      </div>

      {/* Second of the panel's three sections. Divider lives on this
          wrapper, not the fieldset below -- a fieldset with its own border
          makes browsers render <legend> straddling that border instead of
          sitting below it. */}
      <div className={styles.sectionDivider}>
        {/* <fieldset> + <legend> is the native way to give a radio group its
            label: screen readers announce "Shade priority" alongside whichever
            option is focused (the underscore in the visible text is read
            aloud too — a deliberate terminal-copy choice, traded off against
            a slightly odd screen-reader pronunciation). No ARIA needed — the
            built-in semantics do it. */}
        <fieldset className={styles.presetGroup}>
          <legend>
            <span aria-hidden="true">Shade_priority</span>
            <span className={styles.visuallyHidden}>Shade priority</span>
          </legend>
          <div className={styles.segmented}>
            {TREE_PRESETS.map((preset) => (
              <label key={preset.value} className={styles.segment}>
                <input
                  type="radio"
                  name={groupName}
                  aria-label={preset.spoken}
                  value={preset.value}
                  checked={treeWeight === preset.value}
                  onChange={() => onTreeWeightChange(preset.value)}
                  className={styles.segmentInput}
                />
                {/* aria-hidden: the radio's aria-label ("Medium") is the one
                    spoken name -- without this, VoiceOver ALSO read the
                    visible caps text, spelling L-O-W and doubling MED. */}
                <span className={styles.segmentText} aria-hidden="true">{preset.label}</span>
              </label>
            ))}
          </div>
          {/* One box for both the mode description and (once a route exists)
              its actual cost — a walker weighs them together when deciding
              whether a shadier route is worth taking, so they read as one
              unit instead of a plain label above a separately-boxed number
              line. Visible from first load (mode line alone) so the box
              doesn't only appear once results are in. aria-live: this
              text changes every time Shade_priority changes (mode line) or
              a new route resolves (comparison line), and unlike RouteStats'
              stats it's the only place the *delta* numbers appear, so it
              needs its own live region rather than piggybacking on that
              one. aria-atomic re-reads the whole box on any change instead
              of just the changed line, since the two lines are meant to be
              read together as one unit, same as they're meant to be read
              together visually. */}
          <div className={styles.comparisonHint} aria-live="polite" aria-atomic="true">
            {/* Spoken-only prefix: aria-atomic re-reads this whole box on
                every route arrival and preset change, and without a name
                the stream arrived as context-free "mode medium..."
                (VoiceOver pass, 2026-08-31). Every announcement now opens
                with which section is talking. */}
            <span className={styles.visuallyHidden}>Shade priority: </span>
            <p className={styles.modeLine}>
              <span className={styles.promptSymbol} aria-hidden="true">&gt;</span>
              <span className={styles.modeVal}>{selectedPreset?.spoken.toLowerCase()}</span>{' '}
              <span aria-hidden="true">//</span> {selectedPreset?.hint}
            </p>
            {comparison && (
              <p className={styles.comparisonLine}>
                <span aria-hidden="true">
                  <span className={styles.promptSymbol}>&gt;</span><span className={styles.plusSign}>+</span>
                  <span className={styles.numberHighlight}>{comparison.extraShadePct}</span>% shade
                  <span className={styles.sep}>|</span><span className={styles.plusSign}>+</span>
                  <span className={styles.numberHighlight}>{comparison.extraMinutes}</span> min
                  <span className={styles.sep}>|</span><span className={styles.plusSign}>+</span>
                  {highlightNumber(formatDistance(comparison.extraLengthM))}
                </span>
                {/* Spoken twin: full words, no glyph soup (VoiceOver pass). */}
                {/* One string, not adjacent nodes: a pluralizing "s" as its
                    own text node gets read as the letter S ("minute, S") --
                    user report 2026-08-31. */}
                <span className={styles.visuallyHidden}>
                  {`plus ${comparison.extraShadePct} percent shade, plus ` +
                    `${comparison.extraMinutes} ${comparison.extraMinutes === 1 ? 'minute' : 'minutes'}, plus ` +
                    spokenDistance(comparison.extraLengthM)}
                </span>
              </p>
            )}
          </div>
          {/* Always mounted so its aria-live can announce the first
              appearance (same reasoning as RouteStats' wrapper); the
              class swap keeps the empty slot at zero height. */}
          <p
            className={lowShade ? styles.lowShadeNote : styles.lowShadeEmpty}
            aria-live="polite"
          >
            {lowShade && selected && (
              <>
                <strong>
                  <span aria-hidden="true">// LOW_SHADE:</span>
                  <span className={styles.visuallyHidden}>LOW SHADE:</span>
                </strong>{' '}
                {Math.round(displayShade(selected.properties.shade_fraction) * 100)}% shaded over{' '}
                {formatDistance(selected.properties.length_m)} — expect mostly direct sun
              </>
            )}
          </p>
        </fieldset>
      </div>
    </>
  )
}
