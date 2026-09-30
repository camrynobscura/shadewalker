import { useEffect, useId, useRef, useState } from 'react'
import {
  dateInputValue,
  describeWalkTime,
  fromInputs,
  nowInNewYork,
  timeInputValue,
  type Moment,
  type WalkTime,
} from '../walkTime'
import { ClockIcon } from './icons'
import { MenuChoices, PillMenu } from './PillMenu'
import styles from './TimeControl.module.css'

/** The menu's choices. */
type Mode = 'now' | 'depart' | 'arrive'

const MODES = [
  { value: 'now', label: 'Leave now' },
  { value: 'depart', label: 'Depart at' },
  { value: 'arrive', label: 'Arrive by' },
] as const satisfies readonly { value: Mode; label: string }[]

/** How long a date or time field waits after its last change before it
 * re-routes: a time input changes value on every keystroke, so typing
 * 13:00 passes through 01:00 on the way. */
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

/** The walk's time as a small pill (#118; most walks are right now, so
 * it shouldn't weigh as much as the addresses). It reads "Leave now", or
 * the set time ("Arrive 1:00 PM") -- the words alone say a shared link
 * isn't for now.
 *
 * Tapping it opens its menu (PillMenu): Leave now, Depart at or Arrive
 * by, the last two with the device's own date and time inputs. Every
 * change applies as it's made -- a pick at once, a typed date or time
 * after a short pause -- on a phone too (no full-screen picker, no
 * DONE). Tapping Leave now closes the menu; Depart at and
 * Arrive by keep it open for the date and time.
 *
 * The caller places it (a pill row) and decides what a change does:
 * Controls mounts one under the addresses and, on a phone, one on the
 * route screen that re-routes. */
export function TimeControl({ walkTime, onChange }: TimeControlProps) {
  const mode = modeOf(walkTime)
  const [fields, setFields] = useState(() => fieldsOf(walkTime ?? nowInNewYork()))
  const typing = useRef<{ timer: number; apply: () => void } | null>(null)

  useEffect(() => () => window.clearTimeout(typing.current?.timer), [])

  function apply(next: Fields, nextMode: Mode) {
    const picked = fromInputs(next.date, next.time)
    if (picked) onChange({ ...picked, arrive: nextMode === 'arrive' })
  }

  /** A typed change still waiting for its pause: run it now (the menu is
   * closing -- a quick tap outside mustn't lose it) or drop it (a pick
   * replaces it). */
  function settleTyping(run: boolean) {
    const waiting = typing.current
    if (!waiting) return
    typing.current = null
    window.clearTimeout(waiting.timer)
    if (run) waiting.apply()
  }

  // A pick re-routes at once, for the time the fields show. Coming from
  // Leave now -- or from a field left empty --
  // they start again at New York's now; between Depart at and Arrive by
  // they keep their time.
  function pickMode(next: Mode) {
    settleTyping(false)
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
    settleTyping(false)
    const run = () => {
      typing.current = null
      apply(next, mode)
    }
    typing.current = { timer: window.setTimeout(run, TYPING_PAUSE_MS), apply: run }
  }

  return (
    <PillMenu
      pill={
        <>
          <ClockIcon />
          <span className={styles.visuallyHidden}>Start time: </span>
          {walkTime ? describeWalkTime(walkTime) : 'Leave now'}
        </>
      }
      menuClassName={styles.menu}
      // From the set time, or New York's now (not the device's clock, the
      // wrong city for a visitor planning from elsewhere).
      onOpen={() => setFields(fieldsOf(walkTime ?? nowInNewYork()))}
      onClose={() => settleTyping(true)}
    >
      <MenuChoices
        legend="Start time"
        options={MODES}
        value={mode}
        onChange={pickMode}
        // Leave now is a whole answer; the other two need a date and time
        // -- unless Enter says done.
        closesOn={(picked, byKey) => picked === 'now' || byKey}
      />
      {mode !== 'now' && <WhenFields fields={fields} onFields={editFields} />}
    </PillMenu>
  )
}

interface WhenFieldsProps {
  fields: Fields
  onFields: (fields: Fields) => void
}

/** Date and time side by side. The time's label says
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
