import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { geocode, reverseGeocode, type GeocodeResult, type Point, type RouteFeature } from '../api'
import { formatCoords, formatDistance, spokenDistance } from '../format'
import type { GeoPosition } from '../hooks/useGeolocation'
import { useGeocodeSuggestions } from '../hooks/useGeocodeSuggestions'
import { compareRoutes, TREE_PRESETS } from '../presets'
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

type FieldStatus = 'idle' | 'searching' | 'notfound' | 'found'

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
function useAddressField(onResolve: (p: Point | null) => void, externalPoint: Point | null) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<FieldStatus>('idle')
  // Suggestions are wanted only while the current text is something the
  // user TYPED -- a suggestion pick, a submit, or a programmatic fill
  // (map click, reverse geocode) all turn this off, so the dropdown never
  // reopens over text this code wrote itself.
  const [suggestOn, setSuggestOn] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const shownPointRef = useRef<Point | null>(null)
  // Previous externalPoint, so the effect below can tell a point being
  // CLEARED (value -> null) from the steady "no point yet" state while
  // someone types a fresh address.
  const prevExternalRef = useRef<Point | null>(externalPoint)

  const suggestions = useGeocodeSuggestions(query, suggestOn)

  // A fresh suggestion list starts with nothing highlighted -- keeping an
  // old index would silently point Enter at whatever happens to occupy
  // that position now.
  useEffect(() => {
    setActiveIndex(-1)
  }, [suggestions])

  function onChange(value: string) {
    setQuery(value)
    setStatus('idle')
    setSuggestOn(true)
    shownPointRef.current = null // free-typed text no longer matches any known point
  }

  /** A picked suggestion already carries its point -- no second geocode
   * round trip on submit; the field behaves exactly as if resolve() had
   * just succeeded with this result. */
  function selectSuggestion(suggestion: GeocodeResult) {
    setSuggestOn(false)
    setQuery(suggestion.label)
    setStatus('found')
    shownPointRef.current = { lat: suggestion.lat, lon: suggestion.lon }
    onResolve({ lat: suggestion.lat, lon: suggestion.lon })
  }

  function closeSuggestions() {
    setSuggestOn(false)
  }

  /** Leaving an emptied field drops the point it stood for -- the marker
   * shouldn't outlive the text (user report 2026-08-31). Only on blur,
   * never per-keystroke, so retyping an address doesn't nuke the marker
   * mid-edit. Guarded on externalPoint so tabbing through an
   * already-empty field does nothing. */
  function onBlur() {
    setSuggestOn(false)
    if (query.trim() === '' && externalPoint) {
      shownPointRef.current = null
      onResolve(null)
    }
  }

  /** ArrowDown on a closed field re-opens it (ARIA combobox convention) --
   * the hook refetches for the unchanged text after its debounce. */
  function openSuggestions() {
    setSuggestOn(true)
  }

  // `status !== 'idle'` blocks a repeat: onChange resets status back to
  // 'idle' on every keystroke, so this only re-fires once there's actually
  // new text to resolve — not every time "find route" is pressed again.
  async function resolve() {
    setSuggestOn(false) // submitting is the end of the suggestion phase
    if (!query.trim() || status !== 'idle') return
    setStatus('searching')
    const result = await geocode(query)
    if (result) {
      setStatus('found')
      shownPointRef.current = { lat: result.lat, lon: result.lon }
      onResolve({ lat: result.lat, lon: result.lon })
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
      if (prev) {
        setQuery('')
        setStatus('idle')
        setSuggestOn(false)
        shownPointRef.current = null
      }
      return
    }
    const shown = shownPointRef.current
    if (shown && shown.lat === externalPoint.lat && shown.lon === externalPoint.lon) return

    shownPointRef.current = externalPoint
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
      if (label && shownPointRef.current === externalPoint) setQuery(label)
    })
  }, [externalPoint])

  return {
    query,
    status,
    suggestions,
    activeIndex,
    /** Whether the listbox is rendered: suggestions exist AND the text is
     * still in its typed phase. */
    open: suggestOn && suggestions.length > 0,
    onChange,
    resolve,
    selectSuggestion,
    setActiveIndex,
    closeSuggestions,
    openSuggestions,
    onBlur,
  }
}

/** One labeled address field, now an ARIA combobox: the input plus a
 * suggestion listbox driven by aria-activedescendant (focus never leaves
 * the input; arrows move a highlight instead). Presentational — Controls
 * owns the state through useAddressField, passed whole as `field`
 * because a combobox needs eight pieces of it and threading each as its
 * own prop obscured which field a given prop belonged to. No status
 * message once found: the address is already sitting right there in the
 * input, restating it back as text would just be duplicating what's on
 * screen. */
function AddressField({
  label,
  example,
  field,
}: {
  label: string
  example: string
  field: ReturnType<typeof useAddressField>
}) {
  // useId generates a unique, SSR-safe id so <label htmlFor> can point at
  // the input even when the component appears twice on the page.
  const id = useId()
  const listboxId = `${id}-listbox`
  const { suggestions, activeIndex, open } = field

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open) {
      if (e.key === 'ArrowDown') field.openSuggestions()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault() // keep the caret still; the arrow moves the highlight
      field.setActiveIndex((activeIndex + 1) % suggestions.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      field.setActiveIndex(activeIndex <= 0 ? suggestions.length - 1 : activeIndex - 1)
    } else if (e.key === 'Enter' && activeIndex >= 0) {
      e.preventDefault() // pick the highlighted option instead of submitting the form
      field.selectSuggestion(suggestions[activeIndex])
    } else if (e.key === 'Escape') {
      field.closeSuggestions()
    }
  }

  return (
    <div className={styles.addressField}>
      <label htmlFor={id}>{label}</label>
      <div className={styles.suggestWrap}>
        <input
          id={id}
          className={styles.addressInput}
          type="text"
          /* Spoken name drops the underscore ("Start point", not "Start
             underscore point") -- the terminal voice is visual chrome,
             not pronunciation (VoiceOver pass, 2026-08-31). Same split
             as the Shade_walker wordmark. */
          aria-label={label.replace(/_/g, ' ')}
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
          onBlur={field.onBlur}
        />
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
          <ul className={styles.suggestList} role="listbox" id={listboxId} aria-label={`${label.replace(/_/g, ' ')} suggestions`}>
            {suggestions.map((suggestion, i) => (
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
                onClick={() => field.selectSuggestion(suggestion)}
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
      <p className={styles.addressStatus} role="status">
        {field.status === 'notfound' && (
          <>
            <span aria-hidden="true">// NOT_FOUND:</span>
            <span className={styles.visuallyHidden}>NOT FOUND:</span>
            {' '}try adding a borough
          </>
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
  onClear: () => void
  position: GeoPosition | null
  locationEnabled: boolean
  onEnableLocation: () => void
  canClear: boolean
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
  onClear,
  position,
  locationEnabled,
  onEnableLocation,
  canClear,
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

  const start = useAddressField(onSetStart, startPoint)
  const end = useAddressField(onSetEnd, endPoint)
  const isSearching = start.status === 'searching' || end.status === 'searching'

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
          {error}
        </p>
        {/* One form for both fields, so Enter in either one — or the button —
            resolves whichever isn't already resolved. Each field's own
            resolve() no-ops on an empty or already-resolved query, so this
            is safe to fire even if only one field changed. */}
        <form
          className={styles.routeForm}
          onSubmit={(e) => {
            e.preventDefault()
            start.resolve()
            end.resolve()
          }}
        >
          <AddressField label="Start_point" example="250 Court St" field={start} />
          <AddressField label="End_point" example="3rd St & 3rd Ave" field={end} />
          <button
            type="submit"
            className={styles.primaryButton}
            disabled={isSearching}
            aria-label={isSearching ? 'FINDING' : 'FIND ROUTE'}
          >
            {isSearching ? 'FINDING…' : 'FIND_ROUTE'}
          </button>
        </form>

        {/* Location and Clear share a row — both are secondary, one-off
            actions, as opposed to Start/End (always needed) and Shade
            priority (a standing preference). Location is opt-in: first a
            button that *requests* it (triggering the browser permission
            prompt on a user gesture, never on load), which then becomes
            "use it" once a fix arrives. */}
        <div className={styles.buttonRow}>
          {!locationEnabled ? (
            <button type="button" className={styles.secondaryButton} onClick={onEnableLocation} aria-label="USE LOCATION">
              USE_LOCATION
            </button>
          ) : position ? (
            <button type="button" className={styles.secondaryButton} onClick={() => onSetStart(position)} aria-label="SET START POINT">
              SET_START_POINT
            </button>
          ) : (
            <p className={styles.addressStatus} role="status">
              ACQUIRING…
            </p>
          )}

          {canClear && (
            <button type="button" className={styles.secondaryButton} onClick={onClear} aria-label="CLEAR ROUTE">
              CLEAR_ROUTE
            </button>
          )}
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
              <span className={styles.promptSymbol} aria-hidden="true">&gt;</span> mode:{' '}
              <span aria-hidden="true">{selectedPreset?.label.toLowerCase()}</span>
              <span className={styles.visuallyHidden}>{selectedPreset?.spoken.toLowerCase()}</span>{' '}
              <span aria-hidden="true">//</span> {selectedPreset?.hint}
            </p>
            {comparison && (
              <p className={styles.comparisonLine}>
                <span aria-hidden="true">
                  <span className={styles.promptSymbol}>&gt;</span> +
                  <span className={styles.numberHighlight}>{comparison.extraShadePct}</span>% shade · +
                  <span className={styles.numberHighlight}>{comparison.extraMinutes}</span>{' '}
                  min · +{highlightNumber(formatDistance(comparison.extraLengthM))}
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
