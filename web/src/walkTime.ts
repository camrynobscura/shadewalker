/** A wall-clock time in New York, whatever timezone the device is in (the
 * shade is New York's): what the date and time inputs hold and a link
 * carries.
 *
 * The year only matters to the date picker and the label: the server's
 * shade table has no year, so /route gets month/day/hour/minute alone. */
export interface Moment {
  year: number
  month: number // 1-12
  day: number
  hour: number // 0-23
  minute: number
}

/** The walk's time as the picker sets it: a moment, and which end of the
 * walk it is -- when it starts (DEPART AT) or when it has to end (ARRIVE
 * BY). `null` wherever a WalkTime is expected means "leave now" -- the
 * default, since most walks are right now -- and then the server uses its
 * own clock. */
export interface WalkTime extends Moment {
  arrive: boolean
}

const NEW_YORK = 'America/New_York'

const pad = (n: number) => String(n).padStart(2, '0')

/** Now on New York's clock -- what the picker opens on when no time is
 * set, so someone in another timezone starts from the city's hour. */
export function nowInNewYork(at: Date = new Date()): Moment {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: NEW_YORK,
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    hourCycle: 'h23',
  }).formatToParts(at)
  const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((p) => p.type === type)?.value)
  return {
    year: part('year'),
    month: part('month'),
    day: part('day'),
    hour: part('hour'),
    minute: part('minute'),
  }
}

/** "2026-09-27" -- a native date input's value. */
export function dateInputValue(t: Moment): string {
  return `${t.year}-${pad(t.month)}-${pad(t.day)}`
}

/** "09:05" -- a native time input's value. */
export function timeInputValue(t: Moment): string {
  return `${pad(t.hour)}:${pad(t.minute)}`
}

/** The two input values back to a Moment, or null when either is empty
 * or not a real moment (a cleared field, Feb 29 of a common year). */
export function fromInputs(date: string, time: string): Moment | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  const t = /^(\d{2}):(\d{2})$/.exec(time)
  if (!d || !t) return null
  const [year, month, day, hour, minute] = [d[1], d[2], d[3], t[1], t[2]].map(Number)
  if (hour > 23 || minute > 59) return null
  // Date.UTC rolls an impossible day into the next month (Feb 30 -> Mar 2);
  // a round trip that doesn't come back unchanged was never a real date.
  const probe = new Date(Date.UTC(year, month - 1, day))
  if (probe.getUTCFullYear() !== year || probe.getUTCMonth() !== month - 1 || probe.getUTCDate() !== day) {
    return null
  }
  return { year, month, day, hour, minute }
}

/** "2026-09-27T09:05" -- the link's `at` or `arrive` parameter. */
export function formatWalkTime(t: Moment): string {
  return `${dateInputValue(t)}T${timeInputValue(t)}`
}

/** One link parameter back to a Moment; anything malformed is null. */
export function parseWalkTime(value: string | null): Moment | null {
  if (!value) return null
  const [date, time, extra] = value.split('T')
  return extra === undefined && time !== undefined ? fromInputs(date, time) : null
}

/** Which link parameter a set time rides in: `at` for a departure,
 * `arrive` for an arrival. */
export function walkTimeParam(t: WalkTime): 'at' | 'arrive' {
  return t.arrive ? 'arrive' : 'at'
}

/** A link's time back to a WalkTime. Anything malformed is null, which
 * reads as "now" -- a bad link still opens a route; a link carrying both
 * (only by hand) is a departure. */
export function walkTimeFromLink(params: URLSearchParams): WalkTime | null {
  const departure = parseWalkTime(params.get('at'))
  if (departure) return { ...departure, arrive: false }
  const arrival = parseWalkTime(params.get('arrive'))
  return arrival && { ...arrival, arrive: true }
}

/** A Moment as the Date that formats it. UTC on purpose: the Moment
 * already is New York's wall clock, and formatting it in the device's
 * zone would shift it by the difference. */
function utcDate(t: Moment): Date {
  return new Date(Date.UTC(t.year, t.month - 1, t.day, t.hour, t.minute))
}

/** "10:22 AM" -- a route row's leave time. */
export function describeClock(t: Moment): string {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', hour: 'numeric', minute: '2-digit' }).format(
    utcDate(t),
  )
}

const DAY_MS = 86_400_000

/** The time pill's text, as short as it can be and still say when:
 * "Arrive 1:00 PM" today, "Arrive Tue 1:00 PM" in
 * the next six days -- people plan by the weekday -- and "Arrive 10/5,
 * 1:00 PM" further out, or for a day already past (an old link). "Depart
 * …" the same way; the verb keeps an arrival from passing for one. */
export function describeWalkTime(t: WalkTime, today: Moment = nowInNewYork()): string {
  const days = Math.round(
    (Date.UTC(t.year, t.month - 1, t.day) - Date.UTC(today.year, today.month - 1, today.day)) / DAY_MS,
  )
  const clock = describeClock(t)
  let when = `${t.month}/${t.day}, ${clock}`
  if (days === 0) {
    when = clock
  } else if (days > 0 && days < 7) {
    const weekday = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short' }).format(utcDate(t))
    when = `${weekday} ${clock}`
  }
  return `${t.arrive ? 'Arrive' : 'Depart'} ${when}`
}

/** When to leave to arrive at `arrival` after a walk of `minutes`. The
 * minutes are rounded the way the route row shows them (formatEtaParts'
 * Math.round), so a row's leave time and its minutes add up to the
 * arrival -- and the NONE row's is the moment the server scored every
 * route for (server/app.py's _leave_time rounds the same way). */
export function leaveTime(arrival: Moment, minutes: number): Moment {
  const leave = new Date(utcDate(arrival).getTime() - Math.round(minutes) * 60_000)
  return {
    year: leave.getUTCFullYear(),
    month: leave.getUTCMonth() + 1,
    day: leave.getUTCDate(),
    hour: leave.getUTCHours(),
    minute: leave.getUTCMinutes(),
  }
}

/** Same time? Setting an equal time again shouldn't re-fetch the route. */
export function sameWalkTime(a: WalkTime | null, b: WalkTime | null): boolean {
  if (a === null || b === null) return a === b
  return (
    a.arrive === b.arrive &&
    a.year === b.year &&
    a.month === b.month &&
    a.day === b.day &&
    a.hour === b.hour &&
    a.minute === b.minute
  )
}
