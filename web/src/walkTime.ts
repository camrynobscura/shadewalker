/** The walk's departure, as the time picker sets it: a wall-clock time in
 * New York, whatever timezone the device is in (the shade is New York's).
 * `null` wherever a WalkTime is expected means "now" -- the default, since
 * most walks are right now -- and then the server uses its own clock.
 *
 * The year only matters to the date picker and the label: the server's
 * shade table has no year, so /route gets month/day/hour/minute alone. */
export interface WalkTime {
  year: number
  month: number // 1-12
  day: number
  hour: number // 0-23
  minute: number
}

const NEW_YORK = 'America/New_York'

const pad = (n: number) => String(n).padStart(2, '0')

/** Now on New York's clock -- what the picker opens on when no time is
 * set, so someone in another timezone starts from the city's hour. */
export function nowInNewYork(at: Date = new Date()): WalkTime {
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
export function dateInputValue(t: WalkTime): string {
  return `${t.year}-${pad(t.month)}-${pad(t.day)}`
}

/** "09:05" -- a native time input's value. */
export function timeInputValue(t: WalkTime): string {
  return `${pad(t.hour)}:${pad(t.minute)}`
}

/** The two input values back to a WalkTime, or null when either is empty
 * or not a real moment (a cleared field, Feb 29 of a common year). */
export function fromInputs(date: string, time: string): WalkTime | null {
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

/** "2026-09-27T09:05" -- the link's `at` parameter. */
export function formatWalkTime(t: WalkTime): string {
  return `${dateInputValue(t)}T${timeInputValue(t)}`
}

/** A link's `at` parameter back to a WalkTime; anything malformed is
 * null, which reads as "now" -- a bad link still opens a route. */
export function parseWalkTime(value: string | null): WalkTime | null {
  if (!value) return null
  const [date, time, extra] = value.split('T')
  return extra === undefined && time !== undefined ? fromInputs(date, time) : null
}

/** "Sun 9/27, 10:22 AM" -- the time box and the trip line, where a phone
 * has room for about 25 characters (user, 2026-09-27: "Sun, Sep 27,
 * 10:22 AM" wrapped the time box to two lines). The weekday stays:
 * people plan by it. Formatted in UTC on purpose: the WalkTime already
 * IS New York's wall clock, and formatting it in the device's zone
 * would shift it by the difference. */
export function describeWalkTimeShort(t: WalkTime): string {
  const moment = new Date(Date.UTC(t.year, t.month - 1, t.day, t.hour, t.minute))
  const weekday = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short' }).format(moment)
  const time = new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC',
    hour: 'numeric',
    minute: '2-digit',
  }).format(moment)
  return `${weekday} ${t.month}/${t.day}, ${time}`
}

/** Same moment? Setting an equal time again shouldn't re-fetch the route. */
export function sameWalkTime(a: WalkTime | null, b: WalkTime | null): boolean {
  if (a === null || b === null) return a === b
  return (
    a.year === b.year && a.month === b.month && a.day === b.day && a.hour === b.hour && a.minute === b.minute
  )
}
