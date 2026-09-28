import { describe, expect, it } from 'vitest'
import {
  dateInputValue,
  describeClock,
  describeWalkTime,
  formatWalkTime,
  fromInputs,
  leaveTime,
  nowInNewYork,
  parseWalkTime,
  sameWalkTime,
  timeInputValue,
  walkTimeFromLink,
  walkTimeParam,
} from './walkTime'

const SUNDAY_9AM = { year: 2026, month: 9, day: 27, hour: 9, minute: 0 }
const DEPART_9AM = { ...SUNDAY_9AM, arrive: false }
const ARRIVE_9AM = { ...SUNDAY_9AM, arrive: true }

describe('nowInNewYork', () => {
  it("reads New York's clock in summer (UTC-4)", () => {
    expect(nowInNewYork(new Date('2026-07-15T16:30:00Z'))).toEqual({
      year: 2026,
      month: 7,
      day: 15,
      hour: 12,
      minute: 30,
    })
  })

  it('reads it in winter (UTC-5), where UTC is already the next day', () => {
    expect(nowInNewYork(new Date('2026-01-15T03:05:00Z'))).toEqual({
      year: 2026,
      month: 1,
      day: 14,
      hour: 22,
      minute: 5,
    })
  })

  it('calls midnight hour 0, not 24', () => {
    expect(nowInNewYork(new Date('2026-01-15T05:10:00Z')).hour).toBe(0)
  })
})

describe('the native inputs', () => {
  it('pads to the formats date and time inputs use', () => {
    const t = { year: 2026, month: 3, day: 5, hour: 7, minute: 4 }
    expect(dateInputValue(t)).toBe('2026-03-05')
    expect(timeInputValue(t)).toBe('07:04')
  })

  it('round-trips through fromInputs', () => {
    expect(fromInputs(dateInputValue(SUNDAY_9AM), timeInputValue(SUNDAY_9AM))).toEqual(SUNDAY_9AM)
  })

  it('refuses an empty field', () => {
    expect(fromInputs('', '09:00')).toBeNull()
    expect(fromInputs('2026-09-27', '')).toBeNull()
  })

  it('takes Feb 29 only in a leap year', () => {
    expect(fromInputs('2028-02-29', '12:00')).toEqual({ year: 2028, month: 2, day: 29, hour: 12, minute: 0 })
    expect(fromInputs('2026-02-29', '12:00')).toBeNull()
  })

  it('refuses days and hours that do not exist', () => {
    expect(fromInputs('2026-04-31', '12:00')).toBeNull()
    expect(fromInputs('2026-13-01', '12:00')).toBeNull()
    expect(fromInputs('2026-09-27', '24:00')).toBeNull()
    expect(fromInputs('2026-09-27', '12:60')).toBeNull()
  })
})

describe('the link parameter', () => {
  it('round-trips', () => {
    expect(formatWalkTime(SUNDAY_9AM)).toBe('2026-09-27T09:00')
    expect(parseWalkTime('2026-09-27T09:00')).toEqual(SUNDAY_9AM)
  })

  it('reads anything malformed as now (null)', () => {
    for (const bad of [
      null,
      '',
      'tomorrow',
      '2026-09-27',
      '2026-09-27T9:00',
      '2026-09-27T09:00T1',
      '2026-02-30T09:00',
    ]) {
      expect(parseWalkTime(bad)).toBeNull()
    }
  })

  it('carries a departure in `at` and an arrival in `arrive`', () => {
    expect(walkTimeParam(DEPART_9AM)).toBe('at')
    expect(walkTimeParam(ARRIVE_9AM)).toBe('arrive')
    expect(walkTimeFromLink(new URLSearchParams('at=2026-09-27T09:00'))).toEqual(DEPART_9AM)
    expect(walkTimeFromLink(new URLSearchParams('arrive=2026-09-27T09:00'))).toEqual(ARRIVE_9AM)
  })

  it('reads no time, or a malformed one, as now; both at once as the departure', () => {
    expect(walkTimeFromLink(new URLSearchParams(''))).toBeNull()
    expect(walkTimeFromLink(new URLSearchParams('arrive=tomorrow'))).toBeNull()
    expect(walkTimeFromLink(new URLSearchParams('at=2026-09-27T09:00&arrive=2026-09-27T10:00'))).toEqual(
      DEPART_9AM,
    )
  })
})

describe('leaveTime', () => {
  const ONE_PM = { year: 2026, month: 9, day: 28, hour: 13, minute: 0 }

  it('counts back the minutes the route row shows', () => {
    expect(leaveTime(ONE_PM, 38.2)).toEqual({ ...ONE_PM, hour: 12, minute: 22 })
    // the row rounds 38.5 up to "39 min" (Math.round), so the leave time does too
    expect(leaveTime(ONE_PM, 38.5)).toEqual({ ...ONE_PM, hour: 12, minute: 21 })
  })

  it('crosses midnight, a month and a year', () => {
    expect(leaveTime({ year: 2027, month: 1, day: 1, hour: 0, minute: 10 }, 20)).toEqual({
      year: 2026,
      month: 12,
      day: 31,
      hour: 23,
      minute: 50,
    })
  })

  it('knows its year: March 1 backs into Feb 29 only in a leap year', () => {
    expect(leaveTime({ year: 2028, month: 3, day: 1, hour: 0, minute: 5 }, 10).day).toBe(29)
    expect(leaveTime({ year: 2027, month: 3, day: 1, hour: 0, minute: 5 }, 10).day).toBe(28)
  })
})

describe('describeWalkTime', () => {
  // SUNDAY_9AM is Sun 9/27/2026; the pill's text depends on how far off it is.
  const on = (month: number, day: number) => ({ year: 2026, month, day, hour: 0, minute: 0 })
  const text = (t: Parameters<typeof describeWalkTime>[0], today: Parameters<typeof describeWalkTime>[1]) =>
    describeWalkTime(t, today).replace(/\s/g, ' ')

  it('says which end of the walk the time is', () => {
    expect(text(DEPART_9AM, on(9, 27))).toBe('Depart 9:00 AM')
    expect(text(ARRIVE_9AM, on(9, 27))).toBe('Arrive 9:00 AM')
  })

  it('is the time alone today, with the weekday this week, with the date beyond', () => {
    expect(text(ARRIVE_9AM, on(9, 26))).toBe('Arrive Sun 9:00 AM')
    expect(text(ARRIVE_9AM, on(9, 21))).toBe('Arrive Sun 9:00 AM') // six days ahead
    expect(text(ARRIVE_9AM, on(9, 20))).toBe('Arrive 9/27, 9:00 AM') // a week ahead: which Sunday?
    expect(text(ARRIVE_9AM, on(9, 28))).toBe('Arrive 9/27, 9:00 AM') // already past: an old link
  })

  it('counts days across a month and a year', () => {
    const newYear = { year: 2027, month: 1, day: 1, hour: 9, minute: 0, arrive: true }
    expect(text(newYear, { year: 2026, month: 12, day: 31, hour: 23, minute: 0 })).toBe('Arrive Fri 9:00 AM')
  })

  it('does not shift with the device timezone', () => {
    // Formatted in UTC from a New York wall clock: midnight stays midnight.
    const midnight = { year: 2026, month: 1, day: 1, hour: 0, minute: 0, arrive: false }
    expect(text(midnight, on(1, 1))).toBe('Depart 12:00 AM')
  })
})

describe('describeClock', () => {
  it('is the time alone, on the same UTC-formatted wall clock', () => {
    expect(describeClock({ ...SUNDAY_9AM, hour: 12, minute: 20 }).replace(/\s/g, ' ')).toBe('12:20 PM')
    expect(describeClock({ ...SUNDAY_9AM, hour: 0, minute: 5 }).replace(/\s/g, ' ')).toBe('12:05 AM')
  })
})

describe('sameWalkTime', () => {
  it('compares by value, with null as now', () => {
    expect(sameWalkTime(DEPART_9AM, { ...DEPART_9AM })).toBe(true)
    expect(sameWalkTime(DEPART_9AM, { ...DEPART_9AM, minute: 1 })).toBe(false)
    expect(sameWalkTime(null, null)).toBe(true)
    expect(sameWalkTime(null, DEPART_9AM)).toBe(false)
  })

  it('an arrival is not the departure at the same moment', () => {
    expect(sameWalkTime(DEPART_9AM, ARRIVE_9AM)).toBe(false)
  })
})
