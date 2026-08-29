import { useEffect, useId, useRef, useState } from 'react'
import { geocode, reverseGeocode, type Point, type RouteFeature } from '../api'
import { formatCoords, formatDistance } from '../format'
import { displayShade } from '../shade'
import type { GeoPosition } from '../hooks/useGeolocation'
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
function useAddressField(onResolve: (p: Point) => void, externalPoint: Point | null) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<FieldStatus>('idle')
  const shownPointRef = useRef<Point | null>(null)

  function onChange(value: string) {
    setQuery(value)
    setStatus('idle')
    shownPointRef.current = null // free-typed text no longer matches any known point
  }

  // `status !== 'idle'` blocks a repeat: onChange resets status back to
  // 'idle' on every keystroke, so this only re-fires once there's actually
  // new text to resolve — not every time "find route" is pressed again.
  async function resolve() {
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
    if (!externalPoint) return
    const shown = shownPointRef.current
    if (shown && shown.lat === externalPoint.lat && shown.lon === externalPoint.lon) return

    shownPointRef.current = externalPoint
    // Coordinates first, instantly -- reverse-geocoding is a real network
    // round trip (measured ~70-100ms once warm, up to ~1s on a session's
    // first call), and the field showing nothing while a marker's already
    // on the map would look broken. Also doubles as the fallback if the
    // lookup below fails outright (open water, Nominatim down).
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

  return { query, status, onChange, resolve }
}

/** One labeled address field. Purely presentational — Controls owns the
 * query/status/resolve logic (via useAddressField) so one submit button
 * can resolve both fields together. No status message once found: the
 * address is already sitting right there in the input, restating it back
 * as text would just be duplicating what's on screen. */
function AddressField({
  label,
  placeholder,
  query,
  status,
  onChange,
}: {
  label: string
  placeholder: string
  query: string
  status: FieldStatus
  onChange: (value: string) => void
}) {
  // useId generates a unique, SSR-safe id so <label htmlFor> can point at
  // the input even when the component appears twice on the page.
  const id = useId()
  return (
    <div className={styles.addressField}>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className={styles.addressInput}
        type="text"
        value={query}
        placeholder={placeholder}
        autoComplete="street-address"
        onChange={(e) => onChange(e.target.value)}
      />
      {/* role="status" = a polite live region: screen readers announce the
          result without stealing focus. Nothing shown for 'searching' —
          the Find_route button's own "FINDING…" label already covers that,
          and showing it here too just flickered on and off per field. */}
      <p className={styles.addressStatus} role="status">
        {status === 'notfound' && '// NOT_FOUND: try adding a borough'}
      </p>
    </div>
  )
}

/* Four routing intensities, calibrated against real routes: 0 is the plain
 * shortest path (no tree preference — the baseline the other three are
 * measured against), 5 only takes near-free detours, 15 sits mid-plateau
 * where short detours appear, 40 is where the router trades serious
 * distance for trees (+523 m for +115 trees on one Gowanus test walk).
 *
 * `as const` freezes the array into a readonly tuple of literal types —
 * TypeScript then knows each value is exactly 0 | 5 | 15 | 40, not just
 * `number`, and will reject a typo like TREE_PRESETS[0].value = 6. */
export const TREE_PRESETS = [
  { value: 0, label: 'NONE', hint: 'fastest route, no detours for shade' },
  { value: 5, label: 'LOW', hint: 'shadier only when it’s nearly free' },
  { value: 15, label: 'MED', hint: 'short detours for shadier blocks' },
  // "longest detours for the most shade": completes NONE→LOW→MED's
  // detour-size gradient, and — unlike the older "shadiest route, even if
  // it takes longer" — keeps the whole "> mode:" line under the ~52
  // monospace cells that fit one line in the panel (user call 2026-08-28).
  { value: 40, label: 'MAX', hint: 'longest detours for the most shade' },
] as const

// Looked up by label rather than array position — a moderate middle
// ground, not the first or last entry, so it shouldn't depend on where
// MED happens to sit in the list above.
export const DEFAULT_TREE_WEIGHT: number = TREE_PRESETS.find((preset) => preset.label === 'MED')!.value

/** Old bookmarked URLs carry any 0–40 slider value; snap it to the nearest
 * preset. `<=` makes ties go to the later (shadier) option, so the old
 * default of 10 — equidistant from 5 and 15 — lands on Medium. */
export function snapToPreset(weight: number): number {
  let nearest: number = TREE_PRESETS[0].value
  for (const preset of TREE_PRESETS) {
    if (Math.abs(preset.value - weight) <= Math.abs(nearest - weight)) {
      nearest = preset.value
    }
  }
  return nearest
}

export interface RouteComparison {
  extraMinutes: number
  extraShadePct: number
  extraLengthM: number
}

/** How much more shade the selected preset buys, and what it costs in time
 * and distance, relative to the plain-shortest (NONE) baseline. Computed
 * client-side -- /route returns every preset's full properties in one
 * response, so this is a pure subtraction over data the client already has.
 *
 * Deliberately no tree-count delta: the server guarantees shade_fraction is
 * monotonic in Shade_priority (see clamp_shade_monotonic), but tree_count
 * isn't -- a genuinely shadier route can pass fewer individual trees -- so a
 * "-3 trees" beside "+5% shade" would muddy the very thing this line is for.
 * Absolute tree_count still shows in RouteStats. After the clamp, all three
 * deltas here are guaranteed >= 0, which is why the template can hardcode a
 * leading "+".
 *
 * The shade delta subtracts DISPLAYED values (shade.ts), not raw
 * fractions: RouteStats shows curved numbers, and "+5% shade" must equal
 * the difference a user can check between two presets on screen.
 * displayShade is strictly monotone, so the clamp's >= 0 guarantee
 * carries through to the curved delta unchanged. */
export function compareRoutes(selected: RouteFeature, baseline: RouteFeature): RouteComparison {
  return {
    extraMinutes: Math.round(selected.properties.minutes - baseline.properties.minutes),
    extraShadePct: Math.round(
      (displayShade(selected.properties.shade_fraction) - displayShade(baseline.properties.shade_fraction)) * 100,
    ),
    extraLengthM: Math.round((selected.properties.length_m - baseline.properties.length_m) * 10) / 10,
  }
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
  onSetStart: (p: Point) => void
  onSetEnd: (p: Point) => void
  onClear: () => void
  position: GeoPosition | null
  locationEnabled: boolean
  onEnableLocation: () => void
  hasRoute: boolean
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
  hasRoute,
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

  const start = useAddressField(onSetStart, startPoint)
  const end = useAddressField(onSetEnd, endPoint)
  const isSearching = start.status === 'searching' || end.status === 'searching'

  return (
    <>
      {/* First of the panel's three top-level sections -- no divider above
          it (nothing to divide from but the panel's own top edge), unlike
          the two below. */}
      <div className={styles.addressGroup}>
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
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
          <AddressField
            label="Start_point"
            placeholder="e.g. 250 Court St"
            query={start.query}
            status={start.status}
            onChange={start.onChange}
          />
          <AddressField
            label="End_point"
            placeholder="e.g. 3rd St & 3rd Ave"
            query={end.query}
            status={end.status}
            onChange={end.onChange}
          />
          <button type="submit" className={styles.primaryButton} disabled={isSearching}>
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
            <button type="button" className={styles.secondaryButton} onClick={onEnableLocation}>
              USE_LOCATION
            </button>
          ) : position ? (
            <button type="button" className={styles.secondaryButton} onClick={() => onSetStart(position)}>
              SET_START_POINT
            </button>
          ) : (
            <p className={styles.addressStatus} role="status">
              ACQUIRING…
            </p>
          )}

          {hasRoute && (
            <button type="button" className={styles.secondaryButton} onClick={onClear}>
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
          <legend>Shade_priority</legend>
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
                <span className={styles.segmentText}>{preset.label}</span>
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
            <p className={styles.modeLine}>
              <span className={styles.promptSymbol}>&gt;</span> mode: {selectedPreset?.label.toLowerCase()} //{' '}
              {selectedPreset?.hint}
            </p>
            {comparison && (
              <p className={styles.comparisonLine}>
                <span className={styles.promptSymbol}>&gt;</span> +
                <span className={styles.numberHighlight}>{comparison.extraShadePct}</span>% shade · +
                <span className={styles.numberHighlight}>{comparison.extraMinutes}</span>{' '}
                min · +{highlightNumber(formatDistance(comparison.extraLengthM))}
              </p>
            )}
          </div>
        </fieldset>
      </div>
    </>
  )
}
