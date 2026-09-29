import { useEffect, useId, useRef } from 'react'
import type { Point, RouteFeature } from '../api'
import { formatDistance, formatEtaParts, spokenDistance, spokenEta } from '../format'
import type { LocationFillStatus } from '../hooks/useLocationFill'
import { useAddressField } from '../hooks/useAddressField'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from '../hooks/useMediaQuery'
import { AddressField } from './AddressField'
import { BackIcon, CrosshairIcon } from './icons'
import { TREE_PRESETS } from '../presets'
import { CANOPY_SHARE_HIDES_TREE_COUNT, displayShade, LOW_SHADE_FRACTION } from '../shade'
import type { ShadeLayers } from '../shadeLayers'
import { describeClock, leaveTime, type WalkTime } from '../walkTime'
import { ShadeControl } from './ShadeControl'
import { TimeControl } from './TimeControl'
import styles from './Controls.module.css'

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
  /** The route's moment is dark (RouteResponse.night): every preset is the
   * same fastest route, and the box below says so in one line. */
  night: boolean
  /** A rejected route (out of coverage, no path found, server down) --
   * shown right above the address fields since that's what it's actually
   * about, and it's where a user's attention already is right after
   * pressing Enter on an address or tapping the map (the map sits directly
   * above this panel, not down near RouteStats where this used to live). */
  error: string | null
  /** The walk's departure or arrival time, or null for "leave now". */
  walkTime: WalkTime | null
  onWalkTimeChange: (time: WalkTime | null) => void
  /** The shade pill's pick: which kinds of shade the routes are scored by. */
  layers: ShadeLayers
  onLayersChange: (layers: ShadeLayers) => void
  /** Every preset's route from the last FIND_ROUTE, for the rows' numbers. */
  routes: RouteFeature[] | null
  /** The walk time `routes` were fetched for: for an arrival, each row's
   * leave time counts back from it. */
  routeWalkTime: WalkTime | null
  /** The shade `routes` were scored by (RouteResponse.layers). */
  routeLayers: ShadeLayers
  /** A route request is in flight: the rows and lines hold back their
   * numbers rather than show the previous trip's. */
  loading: boolean
  /** Which phone screen shows (App's `view`): the plan (fields, time,
   * FIND_ROUTE) or the route (trip summary, rows). CSS hides the other
   * one on phones; desktop shows both. */
  view: 'plan' | 'route'
  /** FIND_ROUTE, once the fields have finished resolving typed text. */
  onFindRoute: () => void
  /** The trip summary: back to the plan screen to change anything. */
  onBack: () => void
}

/* The rows run shadiest first (MAX on top, user default 2026-09-27):
   shade is the point of the app. */
const PRESETS_SHADIEST_FIRST = [...TREE_PRESETS].reverse()

/** "15 min" / "1 hr 5 min", flat -- the row's compact eta. */
function etaText(minutes: number): string {
  return formatEtaParts(minutes)
    .map((part) => `${part.value} ${part.unit}`)
    .join(' ')
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
  night,
  error,
  walkTime,
  onWalkTimeChange,
  layers,
  onLayersChange,
  routes,
  routeWalkTime,
  routeLayers,
  loading,
  view,
  onFindRoute,
  onBack,
}: ControlsProps) {
  // Radios become one group (arrow keys move between them, only one can be
  // checked) by sharing a `name` — useId gives us one that's unique even if
  // this component ever renders twice.
  const groupName = useId()
  const selectedPreset = TREE_PRESETS.find((preset) => preset.value === treeWeight)
  // Nothing about the chosen route while a new one is on its way: the
  // numbers would be the previous trip's.
  const shown = loading ? null : selected
  // The low-shade warning lives HERE, not with the route stats, since
  // 2026-08-31 (user call): Shade_priority is where the remedy is -- turn
  // the dial up and watch whether the warning goes away.
  // Only for all shade (user, 2026-09-29): with one kind off, "expect
  // mostly direct sun" could be false on a street the other kind shades.
  // The box's last row shows when it has something to say.
  const hasHint = night || shown !== null
  const lowShade =
    shown !== null && routeLayers === 'both' && shown.properties.shade_fraction < LOW_SHADE_FRACTION
  // The tree count, flavour the user wanted kept (2026-09-27): a line
  // under the rows (it didn't fit IN them on a phone), hidden by the same
  // park-canopy rule the old stat had.
  const treeCount =
    shown && shown.properties.park_canopy_share < CANOPY_SHARE_HIDES_TREE_COUNT
      ? shown.properties.tree_count
      : null

  const start = useAddressField(onSetStart, startPoint, onStartLabel, initialStartLabel)
  const end = useAddressField(onSetEnd, endPoint, onEndLabel, initialEndLabel)

  /* FIND_ROUTE is live once each field has a point OR typed text to look
     up. A tap first lets both fields finish resolving -- including the
     lookup the tap's own blur just started -- and only then asks App to
     route, so a typed-but-unconfirmed address counts. */
  const canFind = Boolean((startPoint || start.query.trim()) && (endPoint || end.query.trim()))
  async function findRoute() {
    try {
      await Promise.all([start.resolve(), end.resolve()])
    } catch {
      return // the lookup itself broke; the field's status line says so
    }
    onFindRoute()
  }

  /* Where focus goes when the phone swaps screens: onto the trip summary
     going forward (the top of what just appeared), onto FIND_ROUTE coming
     back, so a keyboard or screen-reader user lands where the change is
     instead of on <body> (the control they used just vanished). Only on
     a CHANGE of screen, never on mount: a shared link opening on the
     route screen shouldn't grab focus. */
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const tripRef = useRef<HTMLButtonElement>(null)
  const findRef = useRef<HTMLButtonElement>(null)
  const shownViewRef = useRef(view)
  useEffect(() => {
    const previous = shownViewRef.current
    shownViewRef.current = view
    if (!isMobile || previous === view) return
    if (view === 'route') tripRef.current?.focus()
    else findRef.current?.focus()
  }, [view, isMobile])

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
        {/* Typed text resolves on Enter (handled in AddressField's
            keydown) and on blur. On a phone the route then waits for
            FIND_ROUTE below (back since 2026-09-27 -- it went in the
            2026-09-02 mobile pass to save room, and the phone's two
            screens gave the room back; it's the checkpoint where a wrong
            address gets caught, and the step to the route screen). Desktop
            has no second screen, so no button (CSS): it routes the moment
            both points exist, as before. A plain div, not a <form>:
            FIND_ROUTE has to wait for the fields' lookups, which a submit
            event can't. */}
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
          {/* Enter here is FIND_ROUTE too: the last field, the natural
              "done" (iOS's Go key arrives as Enter). */}
          <AddressField
            label="End_point"
            marker="B"
            example="24 East 7th St"
            field={end}
            onSubmit={() => void findRoute()}
          />
          {/* The trip's options, right under where (Google Maps' order),
              as small pills: most walks are right now and by all shade,
              so they shouldn't weigh what the addresses do (user,
              2026-09-28). Here a change waits for FIND_ROUTE, like the
              addresses; on desktop it routes by itself. */}
          <div className={styles.tripOptions}>
            <TimeControl walkTime={walkTime} onChange={onWalkTimeChange} />
            <ShadeControl layers={layers} onChange={onLayersChange} />
          </div>
        </div>
        <button
          ref={findRef}
          type="button"
          className={styles.findRoute}
          disabled={!canFind}
          onClick={() => void findRoute()}
        >
          <span aria-hidden="true">FIND_ROUTE</span>
          <span className={styles.visuallyHidden}>Find route</span>
        </button>
      </div>

      {/* The route screen on a phone (hidden until FIND_ROUTE; see App's
          `view`); on desktop simply the panel's second section. */}
      <div className={styles.routeGroup}>
        {/* Phone only (CSS): the two address boxes boiled down to one
            line, and the way back to change them. */}
        <button ref={tripRef} type="button" className={styles.trip} onClick={onBack}>
          <BackIcon />
          <span className={styles.tripWhere}>
            <span className={styles.visuallyHidden}>Change trip: </span>
            {start.query}
            <span className={styles.tripArrow} aria-hidden="true">
              →
            </span>
            <span className={styles.visuallyHidden}> to </span>
            {end.query}
          </span>
        </button>
        {/* Phone only (CSS): the same pills under the trip line, so the
            time shows beside the route it's for -- a route for 9 am can't
            pass for "now" -- and a change here re-routes at once, the way
            Shade_priority is instant (user, 2026-09-28). Desktop has the
            one row under the addresses. */}
        <div className={styles.routeTripOptions}>
          <TimeControl
            walkTime={walkTime}
            onChange={(time) => {
              onWalkTimeChange(time)
              onFindRoute()
            }}
          />
          <ShadeControl
            layers={layers}
            onChange={(picked) => {
              onLayersChange(picked)
              onFindRoute()
            }}
          />
        </div>

        {/* Divider lives on this wrapper, not the fieldset below -- a
            fieldset with its own border makes browsers render <legend>
            straddling that border instead of sitting below it. */}
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
            {/* The four routes as rows (PLAN `phone-space`, user
              2026-09-27): each row IS its route's numbers -- minutes,
              distance, shade -- so every option's cost shows at once
              instead of one comparison at a time. (The tree count was
              tried in the rows too and wrapped them to two lines on a
              phone; it's a line under them instead.) Still one native radio group -- a tap or arrow key
              picks a row and the map shows it, as the old bar did. */}
            <div className={styles.routeRows}>
              {PRESETS_SHADIEST_FIRST.map((preset) => {
                const feature = loading
                  ? null
                  : (routes?.find((r) => r.properties.tree_weight === preset.value) ?? null)
                const numbers = feature?.properties ?? null
                const shadePct = numbers ? Math.round(displayShade(numbers.shade_fraction) * 100) : 0
                // Arrive by: each route's own leave time, first in its row
                // (user, 2026-09-28 -- bare: the time box and the phone's
                // trip line already say Arrive).
                const leave =
                  numbers && routeWalkTime?.arrive
                    ? describeClock(leaveTime(routeWalkTime, numbers.minutes))
                    : null
                const checked = treeWeight === preset.value
                return (
                  <label key={preset.value} className={styles.routeRow}>
                    <input
                      type="radio"
                      name={groupName}
                      value={preset.value}
                      checked={checked}
                      onChange={() => onTreeWeightChange(preset.value)}
                      className={styles.routeInput}
                    />
                    {/* Visible row aria-hidden, one spoken sentence as the
                      name (twin spans, as before): VoiceOver spelled L-O-W
                      and would read "min" and "mi" as words. */}
                    <span className={styles.routeFace} aria-hidden="true">
                      <span className={styles.routeName}>{preset.label}</span>
                      {numbers ? (
                        <>
                          <span className={styles.routeMeta}>
                            {leave && `${leave} · `}
                            {etaText(numbers.minutes)} · {formatDistance(numbers.length_m)}
                          </span>
                          <span className={styles.routeShade}>
                            <strong>{shadePct}%</strong> shaded
                          </span>
                        </>
                      ) : (
                        <span className={styles.routeMeta}>{preset.hint}</span>
                      )}
                      {/* The empty track shows before there are numbers too.
                        Out of 100, not scaled to the shadiest route: a
                        low-shade trip honestly shows short bars. */}
                      <span className={styles.routeMeter}>
                        {numbers && (
                          <span className={styles.routeMeterFill} style={{ width: `${shadePct}%` }} />
                        )}
                      </span>
                    </span>
                    <span className={styles.visuallyHidden}>
                      {numbers
                        ? `${preset.spoken}: ${leave ? `leave by ${leave}, ` : ''}${spokenEta(numbers.minutes)}, ${spokenDistance(numbers.length_m)}, ${shadePct} percent shaded`
                        : `${preset.spoken}: ${preset.hint}`}
                    </span>
                  </label>
                )
              })}
              {/* The box's last row, once a route exists: what the chosen
                preset does, and the tree count -- a footnote in the muted
                green (user, 2026-09-29: two loose lines under the box looked
                tacked on). (A "+31% shade | +3 min" line compared it with NONE
                until the rows showed every route's numbers side by side --
                user, 2026-09-28.) aria-live: they change with Shade_priority
                and when a route arrives; aria-atomic re-reads the row as one
                unit. Always mounted, so it can announce; empty, it takes no
                room and draws no line. */}
              <div
                className={hasHint ? styles.routeHint : styles.routeHintEmpty}
                aria-live="polite"
                aria-atomic="true"
              >
                {/* Spoken-only prefix: aria-atomic re-reads this whole box on
                  every route arrival and preset change, and without a name
                  the stream arrived as context-free "mode medium..."
                  (VoiceOver pass, 2026-08-31). Every announcement now opens
                  with which section is talking. */}
                <span className={styles.visuallyHidden}>Shade priority: </span>
                {night ? (
                  /* After dark (PLAN `night-shade`) every preset is the same
                   fastest route at 100%: the mode hint would promise detours
                   that don't happen, so one line replaces it (copy: user,
                   2026-09-26). The buttons stay live -- they just agree. */
                  <p className={styles.modeLine}>
                    <span aria-hidden="true">
                      <span className={styles.promptSymbol}>&gt;</span>
                      <span className={styles.modeVal}>after dark</span> // the whole city is in shade
                    </span>
                    <span className={styles.visuallyHidden}>
                      After dark. The whole city is in shade, so every setting gives the fastest route.
                    </span>
                  </p>
                ) : (
                  /* Waits for a route: until one arrives (and while the next
                   loads) the rows show these same descriptions themselves. */
                  shown && (
                    <p className={styles.modeLine}>
                      <span className={styles.promptSymbol} aria-hidden="true">
                        &gt;
                      </span>
                      <span className={styles.modeVal}>{selectedPreset?.spoken.toLowerCase()}</span>{' '}
                      <span aria-hidden="true">//</span> {selectedPreset?.hint}
                    </p>
                  )
                )}
                {treeCount !== null && (
                  <p className={styles.treeLine}>
                    <span aria-hidden="true">
                      <span className={styles.promptSymbol}>&gt;</span>
                      <span className={styles.numberHighlight}>{treeCount}</span>{' '}
                      {treeCount === 1 ? 'tree' : 'trees'} along the way
                    </span>
                    <span className={styles.visuallyHidden}>
                      {`${treeCount} ${treeCount === 1 ? 'tree' : 'trees'} along the way`}
                    </span>
                  </p>
                )}
              </div>
            </div>
            {/* Always mounted so its aria-live can announce the first
              appearance (same reasoning as RouteStats' wrapper); the
              class swap keeps the empty slot at zero height. */}
            <p className={lowShade ? styles.lowShadeNote : styles.lowShadeEmpty} aria-live="polite">
              {lowShade && shown && (
                <>
                  <strong>
                    <span aria-hidden="true">// LOW_SHADE:</span>
                    <span className={styles.visuallyHidden}>LOW SHADE:</span>
                  </strong>{' '}
                  {Math.round(displayShade(shown.properties.shade_fraction) * 100)}% shaded over{' '}
                  {formatDistance(shown.properties.length_m)} — expect mostly direct sun
                </>
              )}
            </p>
          </fieldset>
        </div>
      </div>
    </>
  )
}
