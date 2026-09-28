import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type SyntheticEvent,
} from 'react'
import { MOBILE_LAYOUT_QUERY, useMediaQuery } from '../hooks/useMediaQuery'
import {
  dateInputValue,
  describeWalkTimeShort,
  fromInputs,
  nowInNewYork,
  timeInputValue,
  type WalkTime,
} from '../walkTime'
import { ChevronDownIcon, ClockIcon } from './icons'
import styles from './TimeControl.module.css'

/** The picker's choices. `arrive` (arrive by) is the next PR's. */
type Mode = 'now' | 'depart'

const MODES: { value: Mode; label: string; spoken: string }[] = [
  { value: 'now', label: 'LEAVE NOW', spoken: 'Leave now' },
  { value: 'depart', label: 'DEPART AT', spoken: 'Depart at' },
]

interface TimeControlProps {
  /** The picked departure, or null for "leave now". */
  walkTime: WalkTime | null
  onChange: (time: WalkTime | null) => void
}

/** The walk's time (PLAN `phone-space`): a third box under the two
 * address boxes, styled like them (user, 2026-09-27 -- after trying it
 * on the map and as a pill: on the phone's plan screen there's room for
 * a real box). It reads
 * "Leave now", or "Depart Sun, Sep 27, 9:05 AM" once a time is set: the
 * box IS the sign a route isn't for right now, which matters most in a
 * shared link. It opens a picker: LEAVE NOW or DEPART AT, and for DEPART
 * AT the device's own date and time inputs. On desktop that's a card in
 * the panel under the line; on a phone it's the whole screen, the way
 * address search takes it (user: a card looked cramped there). DONE
 * applies; CANCEL (phone), Escape or the line again walk away without
 * applying. */
export function TimeControl({ walkTime, onChange }: TimeControlProps) {
  const isMobile = useMediaQuery(MOBILE_LAYOUT_QUERY)
  const [open, setOpen] = useState(false)
  // Drafts: nothing applies until DONE, so flipping modes or half-picked
  // values never fire a route request.
  const [mode, setMode] = useState<Mode>('now')
  const [date, setDate] = useState('')
  const [time, setTime] = useState('')
  const buttonRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const modeGroupRef = useRef<HTMLFieldSetElement>(null)
  const dialogId = useId()
  const labelId = useId()
  const valueId = useId()
  const titleId = useId()
  const modeName = useId()
  const dateId = useId()
  const timeId = useId()

  /* A native <dialog>, opened two ways. On a phone, showModal(): the
     browser puts it above everything and makes the page behind it inert,
     so Tab can't wander onto controls the full screen hides, and it
     focuses the first control (CANCEL). On desktop, show(): a plain card
     in the panel under the line, page still usable, focus sent by hand
     to the checked mode (arrow keys then move between modes). */
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) {
      if (isMobile) {
        dialog.showModal()
      } else {
        dialog.show()
        modeGroupRef.current?.querySelector<HTMLInputElement>('input:checked')?.focus()
      }
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [open, isMobile])

  function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    // DEPART AT's fields start on the set time, or on New York's now --
    // not the device's clock, which for a visitor planning from elsewhere
    // is the wrong city.
    const shown = walkTime ?? nowInNewYork()
    setMode(walkTime ? 'depart' : 'now')
    setDate(dateInputValue(shown))
    setTime(timeInputValue(shown))
    setOpen(true)
  }

  function close() {
    setOpen(false)
    buttonRef.current?.focus()
  }

  function apply(e: FormEvent) {
    e.preventDefault()
    if (mode === 'now') {
      onChange(null)
    } else {
      // `required` inputs mean the form can't submit with an empty field.
      const picked = fromInputs(date, time)
      if (picked) onChange(picked)
    }
    close()
  }

  // Escape closes the desktop card too (a modal dialog handles its own,
  // below; a show()n one doesn't).
  function onKeyDown(e: KeyboardEvent) {
    if (open && e.key === 'Escape') close()
  }

  // A modal dialog also closes ITSELF on Escape (its cancel event); keep
  // that from happening behind React's back, so `open` stays the truth.
  function onCancel(e: SyntheticEvent) {
    e.preventDefault()
    close()
  }

  return (
    <div className={styles.whenField} onKeyDown={onKeyDown}>
      {/* Built from the address field's own parts (Controls.module.css),
          so the three boxes match: a visible Start_time label on desktop,
          and on a phone the label hides and a clock chip takes the spot
          the A and B chips hold. */}
      <div className={styles.whenHead}>
        <span id={labelId} className={styles.whenLabel}>
          <span aria-hidden="true">Start_time</span>
          <span className={styles.visuallyHidden}>Start time</span>
        </span>
      </div>
      <div className={styles.whenWrap}>
        <span className={styles.whenMarker} aria-hidden="true">
          <ClockIcon />
        </span>
        {/* Named by the label AND the value ("Start time Leave now"): a
            <label for> would replace the button's text as its name. */}
        <button
          ref={buttonRef}
          type="button"
          className={styles.whenBox}
          aria-labelledby={`${labelId} ${valueId}`}
          aria-expanded={open}
          aria-controls={dialogId}
          onClick={toggle}
        >
          <span id={valueId}>{walkTime ? `Depart ${describeWalkTimeShort(walkTime)}` : 'Leave now'}</span>
        </button>
        <span className={styles.whenChevron} aria-hidden="true">
          <ChevronDownIcon />
        </span>
      </div>
      <dialog
        ref={dialogRef}
        id={dialogId}
        className={styles.picker}
        aria-labelledby={titleId}
        onCancel={onCancel}
      >
        <form className={styles.form} onSubmit={apply}>
          <div className={styles.head}>
            {/* Twin spans, the app's terminal-voice technique: the screen
                shows Start_time, a screen reader says "Start time". It
                names the dialog. */}
            <h2 id={titleId} className={styles.title}>
              <span aria-hidden="true">Start_time</span>
              <span className={styles.visuallyHidden}>Start time</span>
            </h2>
            {/* Phone only (CSS): the full screen covers the line that
                opened it, so it needs its own way out. Same word as
                address search. */}
            <button type="button" className={styles.cancel} onClick={close}>
              CANCEL
            </button>
          </div>
          {/* Segmented radios with twin-span labels (VoiceOver spells out
              caps: L-O-W). */}
          <fieldset ref={modeGroupRef} className={styles.modes}>
            <legend className={styles.visuallyHidden}>When you leave</legend>
            <div className={styles.segmented}>
              {MODES.map((m) => (
                <label key={m.value} className={styles.segment}>
                  <input
                    type="radio"
                    name={modeName}
                    value={m.value}
                    checked={mode === m.value}
                    onChange={() => setMode(m.value)}
                    className={styles.segmentInput}
                  />
                  <span className={styles.segmentText} aria-hidden="true">
                    {m.label}
                  </span>
                  <span className={styles.visuallyHidden}>{m.spoken}</span>
                </label>
              ))}
            </div>
          </fieldset>
          {mode === 'depart' && (
            <>
              <div className={styles.field}>
                <label htmlFor={dateId} className={styles.fieldLabel}>
                  date
                </label>
                <input
                  id={dateId}
                  type="date"
                  required
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  className={styles.input}
                />
              </div>
              <div className={styles.field}>
                <label htmlFor={timeId} className={styles.fieldLabel}>
                  time
                </label>
                <input
                  id={timeId}
                  type="time"
                  required
                  value={time}
                  onChange={(e) => setTime(e.target.value)}
                  className={styles.input}
                />
              </div>
              <p className={styles.caption}>New York time</p>
            </>
          )}
          <div className={styles.actions}>
            <button type="submit" className={styles.done}>
              DONE
            </button>
          </div>
        </form>
      </dialog>
    </div>
  )
}
