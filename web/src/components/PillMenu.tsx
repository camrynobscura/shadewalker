import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type FocusEvent,
  type ReactNode,
  type ToggleEvent,
} from 'react'
import { CheckIcon, ChevronDownIcon } from './icons'
import styles from './PillMenu.module.css'

/** Closes the open menu: for a pick that finishes the choice (MenuChoices). */
const CloseMenu = createContext<() => void>(() => {})

/** Between the pill and its menu. */
const GAP_PX = 6
/** The least room left between the menu and the top or bottom of the screen. */
const EDGE_PX = 16

/** Under the pill; above it when there's no room below but there is
 * above. Left and width are the CSS's (clamped inside the screen), so
 * this only needs the pill's left edge and the menu's top. */
function place(pill: HTMLElement | null, card: HTMLElement | null) {
  if (!pill || !card) return
  const r = pill.getBoundingClientRect()
  const height = card.offsetHeight // 0 until it shows
  let top = r.bottom + GAP_PX
  if (height && top + height > window.innerHeight - EDGE_PX && r.top - GAP_PX - height >= EDGE_PX) {
    top = r.top - GAP_PX - height
  }
  card.style.setProperty('--pill-left', `${r.left}px`)
  card.style.setProperty('--menu-top', `${top}px`)
}

interface PillMenuProps {
  /** What the pill says: an icon, a visually hidden name ("Start time: ")
   * and the words. The chevron is added here. */
  pill: ReactNode
  /** The menu card's own class: its width (--menu-width). */
  menuClassName: string
  /** Just before it opens: start any drafts from the current value. */
  onOpen?: () => void
  /** Once it has closed, however it closed. */
  onClose?: () => void
  /** The menu's content. */
  children: ReactNode
}

/** A trip-option pill and the small white menu it opens (#120). The
 * menu floats under the pill, over whatever is below it, and is round
 * like the pill.
 *
 * It is a native popover, so the browser draws it above everything --
 * out of the phone's pill row, which scrolls sideways and would clip it
 * -- and closes it on Escape. The popover itself covers the whole screen,
 * invisibly, with the white card inside: a tap anywhere else lands on
 * that cover and closes the menu, instead of going through to the map
 * and dropping a route point (the ⓘ pop-up's backdrop, for the same
 * reason). Tabbing out of the menu closes it too. Where the card sits is
 * ours: under the pill, inside the screen, above the pill when there's
 * no room below, following it if the page scrolls. */
export function PillMenu({ pill, menuClassName, onOpen, onClose, children }: PillMenuProps) {
  const [open, setOpen] = useState(false)
  const pillRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    const follow = () => place(pillRef.current, cardRef.current)
    // Capture: a scroll inside the panel doesn't bubble to the window.
    window.addEventListener('scroll', follow, { capture: true, passive: true })
    window.addEventListener('resize', follow)
    return () => {
      window.removeEventListener('scroll', follow, { capture: true })
      window.removeEventListener('resize', follow)
    }
  }, [open])

  // The contents mount only while open (a phone has two time pills, and a
  // closed menu shouldn't hold a hidden copy of the fields). React renders
  // this event's update before the browser paints the popover -- toggle
  // events are discrete -- so it never shows empty.
  function onBeforeToggle(e: ToggleEvent<HTMLDivElement>) {
    if (e.newState !== 'open') return
    onOpen?.()
    setOpen(true)
    // Before it shows, so it never paints where the browser puts a popover
    // by default (the middle of the screen).
    place(pillRef.current, cardRef.current)
  }

  function onToggle(e: ToggleEvent<HTMLDivElement>) {
    if (e.newState === 'open') {
      // Measurable now: above the pill if it doesn't fit below.
      place(pillRef.current, cardRef.current)
      // Onto the pick, so arrow keys move between the choices at once.
      const card = cardRef.current
      ;(
        card?.querySelector<HTMLElement>('input:checked') ?? card?.querySelector<HTMLElement>('input')
      )?.focus()
      return
    }
    setOpen(false)
    onClose?.()
    // Back to the pill, unless focus went somewhere on purpose (a tapped
    // field, a Tab). Chrome returns it to the pill itself; Safari returns it
    // to whatever had it before the menu opened -- the panel, since Safari
    // doesn't focus a clicked button -- or to nothing.
    const focused = document.activeElement
    if (!focused || focused.contains(popoverRef.current)) pillRef.current?.focus()
  }

  function close() {
    if (popoverRef.current?.matches(':popover-open')) popoverRef.current.hidePopover()
  }

  function onBlur(e: FocusEvent<HTMLDivElement>) {
    const to = e.relatedTarget
    if (!to || e.currentTarget.contains(to)) return
    // Safari doesn't focus a tapped radio: focus goes to the nearest
    // focusable ancestor (the panel), which Tab never reaches. Closing
    // then would lose the tap to the route rows underneath.
    if (to.contains(e.currentTarget)) return
    // The pill's own click closes the menu (popoverTarget).
    if (to !== pillRef.current) close()
  }

  return (
    <>
      <button
        ref={pillRef}
        type="button"
        className={styles.pill}
        popoverTarget={menuId}
        aria-expanded={open}
        aria-controls={menuId}
      >
        {pill}
        <ChevronDownIcon />
      </button>
      <div
        ref={popoverRef}
        id={menuId}
        popover="auto"
        className={styles.popover}
        onBeforeToggle={onBeforeToggle}
        onToggle={onToggle}
        onBlur={onBlur}
      >
        <div className={styles.cover} aria-hidden="true" onClick={close} />
        <div ref={cardRef} className={`${styles.card} ${menuClassName}`}>
          {open && <CloseMenu value={close}>{children}</CloseMenu>}
        </div>
      </div>
    </>
  )
}

interface MenuChoicesProps<T extends string> {
  /** The group's spoken name; the pill already shows it. */
  legend: string
  options: readonly { value: T; label: string }[]
  value: T
  /** Every change of pick: a tap, or an arrow key moving through them. */
  onChange: (value: T) => void
  /** Whether finishing with this pick closes the menu. Finishing is a tap
   * on a line (even the one already picked) or Enter; arrow keys only
   * move the pick. Every pick closes it unless this says otherwise. */
  closesOn?: (value: T, byKey: boolean) => boolean
}

/** A pill menu's choices: one native radio group (arrow keys move the
 * pick), each line a whole-width tap target with a tick on the pick. */
export function MenuChoices<T extends string>({
  legend,
  options,
  value,
  onChange,
  closesOn = () => true,
}: MenuChoicesProps<T>) {
  const name = useId()
  const close = useContext(CloseMenu)
  return (
    <fieldset className={styles.choices}>
      <legend className={styles.visuallyHidden}>{legend}</legend>
      {options.map((option) => (
        <label key={option.value} className={styles.choice}>
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
            // A pointer's click has a click count; the one an arrow key or
            // Space fires has none (detail 0) -- those only move the pick.
            onClick={(e) => {
              if (e.detail > 0 && closesOn(option.value, false)) close()
            }}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' || !closesOn(option.value, true)) return
              // Or this Enter's keypress lands on the pill, where focus has
              // just gone back, and opens the menu again.
              e.preventDefault()
              close()
            }}
            className={styles.choiceInput}
          />
          <CheckIcon />
          {option.label}
        </label>
      ))}
    </fieldset>
  )
}
