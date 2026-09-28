import { describe, expect, it } from 'vitest'
import {
  dateInputValue,
  describeWalkTimeShort,
  formatWalkTime,
  fromInputs,
  nowInNewYork,
  parseWalkTime,
  sameWalkTime,
  timeInputValue,
} from './walkTime'

const SUNDAY_9AM = { year: 2026, month: 9, day: 27, hour: 9, minute: 0 }

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
})

describe('describeWalkTimeShort', () => {
  it('does not shift with the device timezone', () => {
    // Formatted in UTC from a New York wall clock: midnight stays midnight.
    const midnight = { year: 2026, month: 1, day: 1, hour: 0, minute: 0 }
    expect(describeWalkTimeShort(midnight).replace(/\s/g, ' ')).toBe('Thu 1/1, 12:00 AM')
  })

  it('keeps the weekday, with a number date, to fit a phone', () => {
    expect(
      describeWalkTimeShort({ year: 2026, month: 9, day: 27, hour: 10, minute: 22 }).replace(/\s/g, ' '),
    ).toBe('Sun 9/27, 10:22 AM')
  })
})

describe('sameWalkTime', () => {
  it('compares by value, with null as now', () => {
    expect(sameWalkTime(SUNDAY_9AM, { ...SUNDAY_9AM })).toBe(true)
    expect(sameWalkTime(SUNDAY_9AM, { ...SUNDAY_9AM, minute: 1 })).toBe(false)
    expect(sameWalkTime(null, null)).toBe(true)
    expect(sameWalkTime(null, SUNDAY_9AM)).toBe(false)
  })
})
