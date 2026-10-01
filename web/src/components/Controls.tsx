import { useEffect, useId } from 'react'
import type { Point, RouteFeature } from '../api'
import { formatDistance, formatEtaParts, spokenDistance, spokenEta } from '../format'
import type { LocationFillStatus } from '../hooks/useLocationFill'
import { useAddressField } from '../hooks/useAddressField'
import { AddressField } from './AddressField'
import { CrosshairIcon } from './icons'
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
   * above this panel). */
  error: string | null
  /** The walk's departure or arrival time, or null for "leave now". */
  walkTime: WalkTime | null
  onWalkTimeChange: (time: WalkTime | null) => void
  /** The shade pill's pick: which kinds of shade the routes are scored by. */
  layers: ShadeLayers
  onLayersChange: (layers: ShadeLayers) => void
  /** Every preset's route for the current trip, for the rows' numbers. */
  routes: RouteFeature[] | null
  /** The walk time `routes` were fetched for: for an arrival, each row's
   * leave time counts back from it. */
  routeWalkTime: WalkTime | null
  /** The shade `routes` were scored by (RouteResponse.layers). */
  routeLayers: ShadeLayers
  /** A route request is in flight: the rows and lines hold back their
   * numbers rather than show the previous trip's. */
  loading: boolean
}

/* The rows run shadiest first (MAX on top): shade is the point of the
   app. */
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
}: ControlsProps) {
  // Radios become one group (arrow keys move between them, only one can be
  // checked) by sharing a `name` — useId gives us one that's unique even if
  // this component ever renders twice.
  const groupName = useId()
  const selectedPreset = TREE_PRESETS.find((preset) => preset.value === treeWeight)
  // Nothing about the chosen route while a new one is on its way: the
  // numbers would be the previous trip's.
  const shown = loading ? null : selected
  // The low-shade warning lives here, with Shade_priority, because that is
  // where the remedy is: turn the dial up and watch whether the warning
  // goes away. Only for all shade: with one kind off, "expect mostly
  // direct sun" could be false on a street the other kind shades.
  // The box's last row shows when it has something to say.
  const hasHint = night || shown !== null
  const lowShade =
    shown !== null && routeLayers === 'both' && shown.properties.shade_fraction < LOW_SHADE_FRACTION
  // The tree count: a line under the rows (it doesn't fit in them on a
  // phone), hidden by the park-canopy rule.
  const treeCount =
    shown && shown.properties.park_canopy_share < CANOPY_SHARE_HIDES_TREE_COUNT
      ? shown.properties.tree_count
      : null

  const start = useAddressField(onSetStart, startPoint, onStartLabel, initialStartLabel)
  const end = useAddressField(onSetEnd, endPoint, onEndLabel, initialEndLabel)

  /* Recents are recorded here, on route arrival — not at resolve time.
     Only the endpoints of a route that actually drew count as recent
     addresses: otherwise a lone entry surfaces in the other field's
     recents before any route exists, and abandoned one-field entries
     pollute the list. Each field's commit is
     one-shot, so preset switches swapping `selected` re-record nothing.
     commitRecent's identity changes per render and reads refs — deps on
     it would just refire the effect uselessly. */
  useEffect(() => {
    if (!selected) return
    start.commitRecent()
    end.commitRecent()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected])

  /* The start field's empty-state accessory is the location control:
     "use my location" is a start-point affordance, so it lives in the
     start field — same slot AddressField's own ✕ takes over once there's
     text to clear. One tap does the whole job: enable, wait for a fix
     that clears the accuracy gate, fill start, recenter the map (App's
     useLocationFill owns all of that; this button just reports the tap).
     Disabled only
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

  /* The failure half of the location story. Rendered in the start
     field's status line — the same live region NOT_FOUND uses, right
     under the ⌖ the answer is about. Lives and dies with the ⌖: once the
     field has text, the ✕ has taken the slot and location advice is
     stale noise; clearing the field brings both back. */
  const locationNotice =
    start.query !== '' ? null : locationStatus === 'acquiring' ? (
      /* The accuracy gate can wait up to 10s for a location worth
         trusting, and the only other signal is the ⌖ greying out — this
         line makes the silence read as progress, not a hang. */
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
            whole reason icons.tsx exists). "Location", never "fix", which
            is GPS jargon. */}
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
        {/* The alert region stays mounted; only its text is conditional.
            role="alert" (assertive) only announces content appearing in a
            live region that already existed -- mounting the whole <p> on
            error would mean VoiceOver never catches it, worst on a
            URL-loaded out-of-coverage route (the region would be inserted
            already-populated, so there is no observed change to announce).
            Same reasoning as RouteStats' wrapper. The empty <p> collapses
            to zero height, so no dead space. */}
        <p className={error ? styles.error : styles.errorEmpty} role="alert">
          {error && (
            <>
              {/* Same "// TITLE:" prefix as the low-shade note (aria-hidden
                  glyph + sr-only clean words), so this alert reads in the
                  app's own voice. Covers every message in this slot:
                  out-of-coverage, same start/end, no route, server down. */}
              <strong>
                <span aria-hidden="true">// ERROR:</span>
                <span className={styles.visuallyHidden}>Error:</span>
              </strong>{' '}
              {error}
            </>
          )}
        </p>
        {/* Typed text resolves on Enter (handled in AddressField's
            keydown) and on blur; the route follows the moment both points
            exist. */}
        <div className={styles.addressFields}>
          {/* Both examples verified against /geocode: each resolves to
              the right spot in the Village, inside the landing view.
              Tempting alternatives fail silently -- "45 Charles St" lands
              in Alden Manor, "99 Perry St" on Staten Island. */}
          <AddressField
            label="Start_point"
            marker="A"
            example="Washington Square Park"
            field={start}
            emptyAccessory={locationAccessory}
            notice={locationNotice}
          />
          <AddressField label="End_point" marker="B" example="24 East 7th St" field={end} />
          {/* The trip's options, right under where (Google Maps' order),
              as small pills: most walks are right now and by all shade,
              so they shouldn't weigh what the addresses do. A change
              re-routes at once. */}
          <div className={styles.tripOptions}>
            <TimeControl walkTime={walkTime} onChange={onWalkTimeChange} />
            <ShadeControl layers={layers} onChange={onLayersChange} />
          </div>
        </div>
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
          {/* The four routes as rows (#118): each row is its route's
              numbers -- minutes, distance, shade -- so every option's cost
              shows at once instead of one comparison at a time. (The tree
              count wraps the rows to two lines on a phone, so it's a line
              under them instead.) Still one native radio group -- a tap or
              arrow key picks a row and the map shows it. */}
          <div className={styles.routeRows}>
            {PRESETS_SHADIEST_FIRST.map((preset) => {
              const feature = loading
                ? null
                : (routes?.find((r) => r.properties.tree_weight === preset.value) ?? null)
              const numbers = feature?.properties ?? null
              const shadePct = numbers ? Math.round(displayShade(numbers.shade_fraction) * 100) : 0
              // Arrive by: each route's own leave time, first in its row
              // (bare: the time pill already says Arrive).
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
                      name (twin spans): VoiceOver spells L-O-W and reads
                      "min" and "mi" as words. */}
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
                green, inside the box (loose lines under it look tacked on).
                aria-live: they change with Shade_priority and when a route
                arrives; aria-atomic re-reads the row as one unit. Always
                mounted, so it can announce; empty, it takes no room and
                draws no line. */}
            <div
              className={hasHint ? styles.routeHint : styles.routeHintEmpty}
              aria-live="polite"
              aria-atomic="true"
            >
              {/* Spoken-only prefix: aria-atomic re-reads this whole box on
                  every route arrival and preset change, and without a name
                  the stream arrives as context-free "mode medium...", so
                  every announcement opens with which section is talking. */}
              <span className={styles.visuallyHidden}>Shade priority: </span>
              {night ? (
                /* After dark (#112) every preset is the same fastest route
                   at 100%: the mode hint would promise detours that don't
                   happen, so one line replaces it. The buttons stay live --
                   they just agree. */
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
    </>
  )
}
