import { useId, useState, type FormEvent } from 'react'
import { geocode, type Point } from '../api'
import type { GeoPosition } from '../hooks/useGeolocation'
import styles from './Controls.module.css'

/** Turns a geocoded place name into the terminal-style slug the Greenhouse
 * design uses for status text ("250 Court St, Brooklyn" → "250_court_st_brooklyn"). */
function slugify(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
}

/** One labeled address field with its own submit + status. The keyboard /
 * screen-reader path for setting route points (map clicks are the pointer
 * path — WCAG requires both). */
function AddressField({
  label,
  placeholder,
  onResolve,
}: {
  label: string
  placeholder: string
  onResolve: (p: Point) => void
}) {
  // useId generates a unique, SSR-safe id so <label htmlFor> can point at
  // the input even when the component appears twice on the page.
  const id = useId()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'idle' | 'searching' | 'notfound' | 'found'>('idle')
  const [foundLabel, setFoundLabel] = useState('')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!query.trim()) return
    setStatus('searching')
    const result = await geocode(query)
    if (result) {
      setStatus('found')
      setFoundLabel(result.label)
      onResolve({ lat: result.lat, lon: result.lon })
    } else {
      setStatus('notfound')
    }
  }

  return (
    <form onSubmit={handleSubmit} className={styles.addressForm}>
      <label htmlFor={id}>{label}</label>
      <div className={styles.addressRow}>
        <input
          id={id}
          type="text"
          value={query}
          placeholder={placeholder}
          autoComplete="street-address"
          onChange={(e) => {
            setQuery(e.target.value)
            setStatus('idle')
          }}
        />
        <button type="submit" disabled={status === 'searching'}>
          {status === 'searching' ? '…' : 'LOCK'}
        </button>
      </div>
      {/* role="status" = a polite live region: screen readers announce the
          result without stealing focus. */}
      <p className={styles.addressStatus} role="status">
        {status === 'found' && `// LOCKED: ${slugify(foundLabel)}`}
        {status === 'notfound' && '// NOT_FOUND: try adding a borough'}
      </p>
    </form>
  )
}

/* The three routing intensities, calibrated against real routes: 5 only
 * takes near-free detours, 15 sits mid-plateau where short detours appear,
 * 40 is where the router trades serious distance for trees (+523 m for
 * +115 trees on one Gowanus test walk).
 *
 * `as const` freezes the array into a readonly tuple of literal types —
 * TypeScript then knows each value is exactly 5 | 15 | 40, not just
 * `number`, and will reject a typo like TREE_PRESETS[0].value = 6. */
export const TREE_PRESETS = [
  { value: 5, label: 'LOW', hint: 'greener only when it’s nearly free' },
  { value: 15, label: 'MED', hint: 'short detours for leafier blocks' },
  { value: 40, label: 'MAX', hint: 'longest, greenest route' },
] as const

export const DEFAULT_TREE_WEIGHT: number = TREE_PRESETS[1].value

/** Old bookmarked URLs carry any 0–40 slider value; snap it to the nearest
 * preset. `<=` makes ties go to the later (greener) option, so the old
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

interface ControlsProps {
  treeWeight: number
  onTreeWeightChange: (w: number) => void
  onSetStart: (p: Point) => void
  onSetEnd: (p: Point) => void
  onClear: () => void
  position: GeoPosition | null
  locationEnabled: boolean
  onEnableLocation: () => void
  hasRoute: boolean
}

export function Controls({
  treeWeight,
  onTreeWeightChange,
  onSetStart,
  onSetEnd,
  onClear,
  position,
  locationEnabled,
  onEnableLocation,
  hasRoute,
}: ControlsProps) {
  // Radios become one group (arrow keys move between them, only one can be
  // checked) by sharing a `name` — useId gives us one that's unique even if
  // this component ever renders twice.
  const groupName = useId()
  const selected = TREE_PRESETS.find((preset) => preset.value === treeWeight)
  return (
    <section aria-label="Plan a route">
      <AddressField label="Origin_node" placeholder="e.g. 250 Court St" onResolve={onSetStart} />
      <AddressField label="Dest_node" placeholder="e.g. 3rd St & 3rd Ave" onResolve={onSetEnd} />

      {/* Location is opt-in: first a button that *requests* it (triggering
          the browser permission prompt on a user gesture, never on load),
          which then becomes "use it" once a fix arrives. */}
      {!locationEnabled ? (
        <button type="button" className={styles.secondaryButton} onClick={onEnableLocation}>
          ACQUIRE_POSITION
        </button>
      ) : position ? (
        <button type="button" className={styles.secondaryButton} onClick={() => onSetStart(position)}>
          SET_ORIGIN_NODE
        </button>
      ) : (
        <p className={styles.addressStatus} role="status">
          ACQUIRING…
        </p>
      )}

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
        <p className={styles.presetHint}>
          &gt; mode: {selected?.label.toLowerCase()} // {selected?.hint}
        </p>
      </fieldset>

      {hasRoute && (
        <button type="button" className={styles.secondaryButton} onClick={onClear}>
          CLEAR_ROUTE
        </button>
      )}
    </section>
  )
}
