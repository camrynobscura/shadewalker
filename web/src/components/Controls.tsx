import { useEffect, useId } from 'react'
import type { Point, RouteFeature } from '../api'
import { formatDistance, spokenDistance } from '../format'
import type { LocationFillStatus } from '../hooks/useLocationFill'
import { useAddressField } from '../hooks/useAddressField'
import { AddressField } from './AddressField'
import { CrosshairIcon } from './icons'
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
   * pressing Enter on an address or tapping the map (the map sits directly
   * above this panel, not down near RouteStats where this used to live). */
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
        <span className={styles.visuallyHidden}>Location off:</span> allow location for this site in your
        browser settings
      </>
    ) : locationStatus === 'unavailable' ? (
      <>
        <span aria-hidden="true">{'// NO_LOCATION:'}</span>
        {/* No ⌖ glyph in copy — it's tofu in iOS's mono fallback (the
            whole reason icons.tsx exists). "Location", never "fix" — GPS
            jargon (user call 2026-09-02). */}
        <span className={styles.visuallyHidden}>No location:</span> couldn&#39;t find your location — tap the
        location button to retry
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
          <legend className={styles.presetLegend}>
            <span aria-hidden="true">Shade_priority</span>
            <span className={styles.visuallyHidden}>Shade priority</span>
          </legend>
          <div className={styles.segmented}>
            {TREE_PRESETS.map((preset) => (
              <label key={preset.value} className={styles.segment}>
                <input
                  type="radio"
                  name={groupName}
                  value={preset.value}
                  checked={treeWeight === preset.value}
                  onChange={() => onTreeWeightChange(preset.value)}
                  className={styles.segmentInput}
                />
                {/* The wrapping <label> names the radio, twin-span style:
                    the visible caps text is aria-hidden (VoiceOver spelled
                    L-O-W and read MED as a word) and the hidden span
                    speaks `preset.spoken` ("Medium"). One spoken name, from
                    native labelling — no aria-label on the input
                    (craftsmanship review 2026-09-09). */}
                <span className={styles.segmentText} aria-hidden="true">
                  {preset.label}
                </span>
                <span className={styles.visuallyHidden}>{preset.spoken}</span>
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
              <span className={styles.promptSymbol} aria-hidden="true">
                &gt;
              </span>
              <span className={styles.modeVal}>{selectedPreset?.spoken.toLowerCase()}</span>{' '}
              <span aria-hidden="true">//</span> {selectedPreset?.hint}
            </p>
            {comparison && (
              <p className={styles.comparisonLine}>
                <span aria-hidden="true">
                  <span className={styles.promptSymbol}>&gt;</span>
                  <span className={styles.plusSign}>+</span>
                  <span className={styles.numberHighlight}>{comparison.extraShadePct}</span>% shade
                  <span className={styles.sep}>|</span>
                  <span className={styles.plusSign}>+</span>
                  <span className={styles.numberHighlight}>{comparison.extraMinutes}</span> min
                  <span className={styles.sep}>|</span>
                  <span className={styles.plusSign}>+</span>
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
          <p className={lowShade ? styles.lowShadeNote : styles.lowShadeEmpty} aria-live="polite">
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
