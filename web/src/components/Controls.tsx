import { useId, useState, type FormEvent } from 'react'
import { geocode, type Point } from '../api'
import type { GeoPosition } from '../hooks/useGeolocation'
import styles from './Controls.module.css'

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
          {status === 'searching' ? '…' : 'Set'}
        </button>
      </div>
      {/* role="status" = a polite live region: screen readers announce the
          result without stealing focus. */}
      <p className={styles.addressStatus} role="status">
        {status === 'found' && `✓ ${foundLabel}`}
        {status === 'notfound' && 'No match found in NYC — try adding a borough.'}
      </p>
    </form>
  )
}

function weightDescription(weight: number): string {
  if (weight === 0) return 'shortest path, trees ignored'
  if (weight <= 12) return 'takes free green detours'
  if (weight <= 25) return 'moderate detours for trees'
  return 'maximum greenery, long detours allowed'
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
  const sliderId = useId()
  return (
    <section aria-label="Plan a route">
      <AddressField label="Start address" placeholder="e.g. 250 Court St" onResolve={onSetStart} />
      <AddressField label="End address" placeholder="e.g. 3rd St & 3rd Ave" onResolve={onSetEnd} />

      {/* Location is opt-in: first a button that *requests* it (triggering
          the browser permission prompt on a user gesture, never on load),
          which then becomes "use it" once a fix arrives. */}
      {!locationEnabled ? (
        <button type="button" className={styles.secondaryButton} onClick={onEnableLocation}>
          Show my location on the map
        </button>
      ) : position ? (
        <button type="button" className={styles.secondaryButton} onClick={() => onSetStart(position)}>
          Use my location as start
        </button>
      ) : (
        <p className={styles.addressStatus} role="status">
          Locating…
        </p>
      )}

      <div className={styles.sliderBlock}>
        <label htmlFor={sliderId}>
          Tree preference: <strong>{treeWeight}</strong>
          <span className={styles.sliderHint}> — {weightDescription(treeWeight)}</span>
        </label>
        <input
          id={sliderId}
          type="range"
          min={0}
          max={40}
          step={1}
          value={treeWeight}
          aria-valuetext={`${treeWeight} — ${weightDescription(treeWeight)}`}
          onChange={(e) => onTreeWeightChange(Number(e.target.value))}
        />
      </div>

      {hasRoute && (
        <button type="button" className={styles.secondaryButton} onClick={onClear}>
          Clear route
        </button>
      )}
    </section>
  )
}
