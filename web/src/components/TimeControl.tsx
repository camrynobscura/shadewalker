import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
  type SyntheticEvent,
} from 'react'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from '../hooks/useMediaQuery'
import {
  dateInputValue,
  describeWalkTime,
  fromInputs,
  nowInNewYork,
  timeInputValue,
  type Moment,
  type WalkTime,
} from '../walkTime'
import { ChevronDownIcon, ClockIcon } from './icons'
import styles from './TimeControl.module.css'

/** The picker's choices. */
type Mode = 'now' | 'depart' | 'arrive'

const MODES: { value: Mode; label: string; spoken: string }[] = [
  { value: 'now', label: 'LEAVE NOW', spoken: 'Leave now' },
  { value: 'depart', label: 'DEPART AT', spoken: 'Depart at' },
  { value: 'arrive', label: 'ARRIVE BY', spoken: 'Arrive by' },
]

/** How long a desktop date or time field waits after its last change
 * before it re-routes (user, 2026-09-28): a time input changes value on
 * every keystroke, so typing 13:00 passes through 01:00 on the way. */
const TYPING_PAUSE_MS = 500

function modeOf(t: WalkTime | null): Mode {
  return t ? (t.arrive ? 'arrive' : 'depart') : 'now'
}

/** The two native inputs' values. */
interface Fields {
  date: string
  time: string
}

function fieldsOf(t: Moment): Fields {
  return { date: dateInputValue(t), time: timeInputValue(t) }
}

interface TimeControlProps {
  /** The picked departure or arrival, or null for "leave now". */
  walkTime: WalkTime | null
  onChange: (time: WalkTime | null) => void
}

/** The walk's time as a small pill (PLAN `time-and-layers`, user
 * 2026-09-28: most walks are right now, so it shouldn't weigh as much as
 * the addresses). It reads "Leave now", or the set time ("Arrive 1:00
 * PM") -- the words alone say a shared link isn't for now.
 * Tapping it opens the picker -- LEAVE NOW, DEPART AT or ARRIVE BY, the
 * last two with the device's own date and time inputs:
 *
 * - Phone: the whole screen, the way address search takes it; DONE
 *   applies, CANCEL or Escape walk away.
 * - Desktop: a card under the pill that applies as you go -- a pick at
 *   once, a typed date or time after a short pause -- since desktop
 *   routes by itself. The pill again or Escape closes it.
 *
 * The caller places it (a pill row) and decides what a change does:
 * Controls mounts one under the addresses and, on a phone, one on the
 * route screen that re-routes. */
export function TimeControl({ walkTime, onChange }: TimeControlProps) {
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const [open, setOpen] = useState(false)
  const pillRef = useRef<HTMLButtonElement>(null)
  const pickerId = useId()

  function close() {
    setOpen(false)
    pillRef.current?.focus()
  }

  return (
    <>
      <button
        ref={pillRef}
        type="button"
        className={styles.pill}
        aria-expanded={open}
        aria-haspopup={isMobile ? 'dialog' : undefined}
        // Only while open: the picker isn't in the DOM otherwise.
        aria-controls={open ? pickerId : undefined}
        onClick={() => setOpen(!open)}
      >
        <ClockIcon />
        <span className={styles.visuallyHidden}>Start time: </span>
        {walkTime ? describeWalkTime(walkTime) : 'Leave now'}
        <ChevronDownIcon />
      </button>
      {open &&
        (isMobile ? (
          <PhonePicker id={pickerId} walkTime={walkTime} onChange={onChange} onClose={close} />
        ) : (
          <DesktopCard id={pickerId} walkTime={walkTime} onChange={onChange} onClose={close} />
        ))}
    </>
  )
}

interface PickerProps extends TimeControlProps {
  id: string
  onClose: () => void
}

/* ── phone: the whole screen ──────────────────────────────────────────── */

function PhonePicker({ id, walkTime, onChange, onClose }: PickerProps) {
  // Drafts, from the set time or New York's now (not the device's clock,
  // the wrong city for a visitor planning from elsewhere): nothing
  // applies until DONE, so flipping modes never fires a route request.
  const [mode, setMode] = useState<Mode>(() => modeOf(walkTime))
  const [fields, setFields] = useState(() => fieldsOf(walkTime ?? nowInNewYork()))
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  /* showModal() puts it above everything and makes the page behind it
     inert, so Tab can't wander onto controls the full screen hides; it
     focuses the first control (CANCEL). Mounted only while open. The
     close() on the way out is what hands focus back to the pill: the
     browser returns it to whatever opened a modal when it CLOSES, but
     not when it's just removed. A layout effect, because its cleanup runs
     while the dialog is still in the document. */
  useLayoutEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])

  function apply(e: FormEvent) {
    e.preventDefault()
    if (mode === 'now') {
      onChange(null)
    } else {
      // `required` inputs mean the form can't submit with an empty field.
      const picked = fromInputs(fields.date, fields.time)
      if (picked) onChange({ ...picked, arrive: mode === 'arrive' })
    }
    onClose()
  }

  // A modal dialog closes ITSELF on Escape (its cancel event); keep that
  // from happening behind React's back.
  function onCancel(e: SyntheticEvent) {
    e.preventDefault()
    onClose()
  }

  return (
    <dialog ref={dialogRef} id={id} className={styles.picker} aria-labelledby={titleId} onCancel={onCancel}>
      <form className={styles.form} onSubmit={apply}>
        <div className={styles.head}>
          {/* Names the dialog; twin spans: Start_time on screen, "Start
              time" spoken. */}
          <h2 id={titleId} className={styles.title}>
            <span aria-hidden="true">Start_time</span>
            <span className={styles.visuallyHidden}>Start time</span>
          </h2>
          {/* The full screen covers the pill that opened it, so it needs
              its own way out. Same word as address search. */}
          <button type="button" className={styles.cancel} onClick={onClose}>
            CANCEL
          </button>
        </div>
        <ModePicker mode={mode} onMode={setMode} />
        {mode !== 'now' && <WhenFields fields={fields} onFields={setFields} />}
        <button type="submit" className={styles.done}>
          DONE
        </button>
      </form>
    </dialog>
  )
}

/* ── desktop: a card that applies as you go ───────────────────────────── */

function DesktopCard({ id, walkTime, onChange, onClose }: PickerProps) {
  const mode = modeOf(walkTime)
  const [fields, setFields] = useState(() => fieldsOf(walkTime ?? nowInNewYork()))
  const cardRef = useRef<HTMLDivElement>(null)
  const pending = useRef<number | undefined>(undefined)

  // Focus the checked mode, so arrow keys move between them at once.
  useEffect(() => {
    cardRef.current?.querySelector<HTMLInputElement>('input:checked')?.focus()
  }, [])
  // A typed change still waiting when the card closes is dropped.
  useEffect(() => () => window.clearTimeout(pending.current), [])

  function apply(next: Fields, nextMode: Mode) {
    const picked = fromInputs(next.date, next.time)
    if (picked) onChange({ ...picked, arrive: nextMode === 'arrive' })
  }

  // A pick re-routes at once, for the time the fields show (user,
  // 2026-09-28). Coming from LEAVE NOW -- or from a field left empty --
  // they start again at New York's now; between DEPART AT and ARRIVE BY
  // they keep their time.
  function pickMode(next: Mode) {
    window.clearTimeout(pending.current)
    if (next === 'now') {
      onChange(null)
      return
    }
    const shown = mode === 'now' || !fromInputs(fields.date, fields.time) ? fieldsOf(nowInNewYork()) : fields
    setFields(shown)
    apply(shown, next)
  }

  function editFields(next: Fields) {
    setFields(next)
    window.clearTimeout(pending.current)
    pending.current = window.setTimeout(() => apply(next, mode), TYPING_PAUSE_MS)
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose()
  }

  return (
    <div ref={cardRef} id={id} className={styles.card} onKeyDown={onKeyDown}>
      <ModePicker mode={mode} onMode={pickMode} />
      {mode !== 'now' && <WhenFields fields={fields} onFields={editFields} />}
    </div>
  )
}

/* ── the shared parts ─────────────────────────────────────────────────── */

interface ModePickerProps {
  mode: Mode
  onMode: (mode: Mode) => void
}

/** LEAVE NOW / DEPART AT / ARRIVE BY: one native radio group (arrow keys
 * move between them), each word over a faint track like the route rows'
 * meters, with a jade bar that slides to the pick. */
function ModePicker({ mode, onMode }: ModePickerProps) {
  const name = useId()
  const picked = MODES.findIndex((m) => m.value === mode)
  // The CSS sizes each track, and the bar, from its word's letter count
  // (the type is monospace), and slides the bar to the picked third.
  const bar = {
    '--count': MODES.length,
    '--pick': picked,
    '--pick-chars': MODES[picked].label.length,
  } as CSSProperties
  return (
    <fieldset className={styles.modes}>
      <legend className={styles.visuallyHidden}>Start time</legend>
      <div className={styles.tabs} style={bar}>
        {MODES.map((m) => (
          <label key={m.value} className={styles.tab} style={{ '--chars': m.label.length } as CSSProperties}>
            <input
              type="radio"
              name={name}
              value={m.value}
              checked={mode === m.value}
              onChange={() => onMode(m.value)}
              className={styles.tabInput}
            />
            {/* VoiceOver spells out caps (L-O-W), so the spoken twin. */}
            <span className={styles.tabText} aria-hidden="true">
              {m.label}
            </span>
            <span className={styles.visuallyHidden}>{m.spoken}</span>
          </label>
        ))}
        <span className={styles.tabBar} aria-hidden="true" />
      </div>
    </fieldset>
  )
}

interface WhenFieldsProps {
  fields: Fields
  onFields: (fields: Fields) => void
}

/** Date and time side by side (user, 2026-09-28). The time's label says
 * whose clock: the shade is New York's, whatever zone the device is in
 * ("NYC", not "EST", which is wrong from March to November). */
function WhenFields({ fields, onFields }: WhenFieldsProps) {
  const dateId = useId()
  const timeId = useId()
  return (
    <div className={styles.fields}>
      <div className={styles.field}>
        <label htmlFor={dateId} className={styles.fieldLabel}>
          date
        </label>
        <input
          id={dateId}
          type="date"
          required
          value={fields.date}
          onChange={(e) => onFields({ ...fields, date: e.target.value })}
          className={styles.input}
        />
      </div>
      <div className={styles.field}>
        <label htmlFor={timeId} className={styles.fieldLabel}>
          time (NYC)
        </label>
        <input
          id={timeId}
          type="time"
          required
          value={fields.time}
          onChange={(e) => onFields({ ...fields, time: e.target.value })}
          className={styles.input}
        />
      </div>
    </div>
  )
}
