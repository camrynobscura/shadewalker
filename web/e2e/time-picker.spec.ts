import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// The time pill (PLAN `time-and-layers`): a small pill under the address
// fields -- and on a phone's route screen, under the trip line -- that
// reads "Leave now" until a time is set, then "Depart <when>" or "Arrive
// <when>". It opens the picker: LEAVE NOW, DEPART AT or ARRIVE BY (the
// last two with native date + time inputs). On desktop that's a card that
// applies as you go (a pick at once, typing after a pause); on a phone
// it's the whole screen and DONE applies. A set time rides every /route
// request as month/day/hour/minute (plus arrive=true for an arrival) and
// sits in the link as `at` or `arrive`; "leave now" sends and stores
// nothing, so the server's own clock (pinned to July 15 noon for this
// suite) decides.

const NIGHT_LINE = 'after dark // the whole city is in shade'
const TIME_PARAMS = ['month', 'day', 'hour', 'minute', 'arrive']

/** The next /route request's time parameters, [month, day, hour, minute,
 * arrive]; nulls for what it doesn't carry ("leave now" carries none). */
async function nextRouteTime(page: Page, action: () => Promise<unknown>): Promise<(string | null)[]> {
  const request = page.waitForRequest((r) => r.url().includes('/route?'))
  await action()
  const params = new URL((await request).url()).searchParams
  return TIME_PARAMS.map((key) => params.get(key))
}

/** The visible time pill (a phone has one per screen; only one shows). */
function timePill(page: Page, text: string | RegExp = /.*/) {
  const name = typeof text === 'string' ? `Start time: ${text}` : new RegExp(`^Start time: ${text.source}`)
  return page.getByRole('button', { name })
}

function picker(page: Page) {
  return page.getByRole('dialog', { name: 'Start time' })
}

/** The date + time fields, filled at once: one re-route after the pause. */
async function fillWhen(page: Page, date: string, time: string) {
  await page.getByLabel('date', { exact: true }).fill(date)
  await page.getByLabel('time (NYC)').fill(time)
}

test.describe('on desktop', () => {
  test('a departure goes out with the route, shows on the pill, and survives a reload', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await timePill(page, 'Leave now').click()
    // Opens on LEAVE NOW, focused, with no date or time fields yet.
    const leaveNow = page.getByRole('radio', { name: 'Leave now' })
    await expect(leaveNow).toBeChecked()
    await expect(leaveNow).toBeFocused()
    await expect(page.getByLabel('date', { exact: true })).toHaveCount(0)

    // A pick re-routes at once, for the time the fields show: now.
    const picked = await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())
    expect(picked.slice(0, 4).every((part) => part !== null)).toBe(true)
    expect(picked[4]).toBeNull()

    // Typed date and time go out together, after the pause.
    const sent = await nextRouteTime(page, () => fillWhen(page, '2026-09-27', '09:05'))
    expect(sent).toEqual(['9', '27', '9', '5', null])

    // The pill is the sign this route isn't for right now (a past day
    // shows its date).
    await expect(timePill(page, /Depart 9\/27, 9:05\sAM$/)).toBeVisible()
    expect(new URL(page.url()).searchParams.get('at')).toBe('2026-09-27T09:05')

    const resent = await nextRouteTime(page, () => page.reload())
    expect(resent).toEqual(['9', '27', '9', '5', null])
    await expect(timePill(page, /Depart 9\/27, 9:05\sAM$/)).toBeVisible()
  })

  test('typing a time waits for a pause, then routes once', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await timePill(page, 'Leave now').click()
    await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())

    const routes: string[] = []
    page.on('request', (r) => {
      if (r.url().includes('/route?')) routes.push(r.url())
    })
    // Key by key, a time input passes through 01:00 on the way to 13:00.
    await page.getByLabel('time (NYC)').click()
    await page.keyboard.type('0315PM', { delay: 50 })
    expect(routes).toHaveLength(0)
    await expect.poll(() => routes.length).toBe(1)
    const params = new URL(routes[0]).searchParams
    expect([params.get('hour'), params.get('minute')]).toEqual(['15', '15'])
  })

  test('LEAVE NOW goes back to now: no time in the request, the link, or on the pill', async ({ page }) => {
    await mockGeocode(page)
    const opened = await nextRouteTime(page, () =>
      page.goto(`${routeUrl(POINT_A, POINT_B)}&at=2026-09-27T09:05`),
    )
    expect(opened).toEqual(['9', '27', '9', '5', null])

    await timePill(page, /Depart 9\/27/).click()
    // Opens on DEPART AT with the set time, not on today.
    await expect(page.getByRole('radio', { name: 'Depart at' })).toBeChecked()
    await expect(page.getByLabel('date', { exact: true })).toHaveValue('2026-09-27')
    await expect(page.getByLabel('time (NYC)')).toHaveValue('09:05')

    const sent = await nextRouteTime(page, () => page.getByRole('radio', { name: 'Leave now' }).check())
    expect(sent).toEqual([null, null, null, null, null])
    await expect(timePill(page, 'Leave now')).toBeVisible()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)

    // Escape closes the card, back onto the pill.
    await page.keyboard.press('Escape')
    await expect(page.getByRole('radio', { name: 'Leave now' })).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeFocused()
  })

  test('an arrival goes out marked, rides the link as `arrive`, and every row shows its own leave time', async ({
    page,
  }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await timePill(page, 'Leave now').click()
    await nextRouteTime(page, () => page.getByRole('radio', { name: 'Arrive by' }).check())
    const request = page.waitForRequest((r) => r.url().includes('/route?'))
    await fillWhen(page, '2026-07-15', '13:00')
    const sent = new URL((await request).url()).searchParams
    expect(TIME_PARAMS.map((key) => sent.get(key))).toEqual(['7', '15', '13', '0', 'true'])

    await expect(timePill(page, /Arrive 7\/15, 1:00\sPM$/)).toBeVisible()
    const link = new URL(page.url()).searchParams
    expect(link.get('arrive')).toBe('2026-07-15T13:00')
    expect(link.has('at')).toBe(false)

    // The NONE row's leave time is the arrival minus its minutes, AND the
    // moment the server scored every route for: the two round alike.
    // This request's own reply: the ARRIVE BY pick's (for now) may land later.
    const body = await (await (await request).response())!.json()
    const noneMinutes = body.routes.find(
      (r: { properties: { tree_weight: number } }) => r.properties.tree_weight === 0,
    ).properties.minutes
    const leave = 13 * 60 - Math.round(noneMinutes)
    expect(body.hour * 60 + body.minute).toBe(leave)
    const clock = `${((Math.floor(leave / 60) + 11) % 12) + 1}:${String(leave % 60).padStart(2, '0')}`
    await expect(
      page.getByRole('radio', { name: new RegExp(`^None: leave by ${clock}\\s[AP]M, `) }),
    ).toHaveCount(1)
    for (const preset of ['Low', 'Medium', 'Maximum']) {
      await expect(
        page.getByRole('radio', { name: new RegExp(`^${preset}: leave by \\d{1,2}:\\d{2}\\s[AP]M, `) }),
      ).toHaveCount(1)
    }

    // A reload keeps the arrival, and the card reopens on it.
    const resent = page.waitForRequest((r) => r.url().includes('/route?'))
    await page.reload()
    expect(new URL((await resent).url()).searchParams.get('arrive')).toBe('true')
    await timePill(page, /Arrive 7\/15/).click()
    await expect(page.getByRole('radio', { name: 'Arrive by' })).toBeChecked()
    await expect(page.getByLabel('time (NYC)')).toHaveValue('13:00')
  })

  test('a night departure reaches the real backend', async ({ page }) => {
    // Nothing staged: the suite's backend clock says noon, so the after-dark
    // line can only come from the time the card sent.
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    // The route first: while it loads, the MED row shows this same
    // description in place of its numbers.
    await expect(routeDrawn(page)).toBeVisible()
    await expect(page.getByText('short detours for shadier blocks')).toBeVisible()

    await timePill(page, 'Leave now').click()
    await page.getByRole('radio', { name: 'Depart at' }).check()
    await fillWhen(page, '2026-07-15', '02:00')
    await expect(page.getByText(NIGHT_LINE)).toBeVisible()
  })

  test('a malformed time in a link is ignored: the route is for now', async ({ page }) => {
    await mockGeocode(page)
    const opened = await nextRouteTime(page, () => page.goto(`${routeUrl(POINT_A, POINT_B)}&at=tomorrow`))
    expect(opened).toEqual([null, null, null, null, null])
    await expect(timePill(page, 'Leave now')).toBeVisible()
  })

  test('the time pill sits under the address fields, smaller than a field', async ({ page }) => {
    await page.goto('/')
    const endField = await page.getByRole('combobox', { name: 'End point' }).boundingBox()
    const pill = await timePill(page, 'Leave now').boundingBox()
    expect(endField && pill).toBeTruthy()
    expect(pill!.y).toBeGreaterThan(endField!.y + endField!.height)
    // A pill, not a third field (user, 2026-09-28): narrower and shorter.
    expect(pill!.width).toBeLessThan(endField!.width / 2)
    expect(pill!.height).toBeLessThan(endField!.height)
  })
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test('the picker takes the whole screen, and CANCEL walks away without applying', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').tap()
    // Like address search: the phone's picker is the screen, not a card.
    const box = await picker(page).boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(389)
    expect(box!.height).toBeGreaterThanOrEqual(843)

    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await picker(page).getByLabel('time (NYC)').fill('02:00')
    await picker(page).getByRole('button', { name: 'CANCEL' }).tap()
    await expect(picker(page)).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeFocused()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)
  })

  test('the full-screen picker has no accessibility violations', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').tap()
    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await expect(picker(page).getByLabel('date', { exact: true })).toBeVisible()
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })

  test('Escape closes the picker without applying', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').tap()
    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await picker(page).getByLabel('time (NYC)').fill('02:00')
    await page.keyboard.press('Escape')

    await expect(picker(page)).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeFocused()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)
  })

  test('a shared arrival shows on the route screen, under the trip line', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(`${routeUrl(POINT_A, POINT_B)}&arrive=2026-07-15T13:00`)
    await expect(routeDrawn(page)).toBeVisible()
    // The pill says when, so the trip line says only where.
    await expect(timePill(page, /Arrive 7\/15, 1:00\sPM$/)).toBeVisible()
    await expect(page.getByRole('button', { name: /^Change trip:/ })).toHaveAccessibleName(/to 3rd Ave$/)
  })

  test('on the route screen a time change re-routes at once', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await timePill(page, 'Leave now').tap()
    await picker(page).getByRole('radio', { name: 'Arrive by' }).check()
    await picker(page).getByLabel('date', { exact: true }).fill('2026-07-15')
    await picker(page).getByLabel('time (NYC)').fill('13:00')
    // No FIND_ROUTE here: DONE is enough, the way Shade_priority is instant.
    const sent = await nextRouteTime(page, () => picker(page).getByRole('button', { name: 'DONE' }).tap())
    expect(sent).toEqual(['7', '15', '13', '0', 'true'])
    await expect(timePill(page, /Arrive 7\/15/)).toBeVisible()
    await expect(page.getByRole('radio', { name: /^Medium: leave by / })).toBeVisible()
  })

  test('on the plan screen a time change waits for FIND_ROUTE, like the addresses', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await page.getByRole('button', { name: /^Change trip:/ }).tap()

    const routes: string[] = []
    page.on('request', (r) => {
      if (r.url().includes('/route?')) routes.push(r.url())
    })
    await timePill(page, 'Leave now').tap()
    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await picker(page).getByLabel('date', { exact: true }).fill('2026-07-15')
    await picker(page).getByLabel('time (NYC)').fill('09:05')
    await picker(page).getByRole('button', { name: 'DONE' }).tap()
    await expect(timePill(page, /Depart 7\/15, 9:05\sAM$/)).toBeVisible()
    // A "nothing happened" check needs a window to happen in.
    await page.waitForTimeout(500)
    expect(routes).toHaveLength(0)

    const sent = await nextRouteTime(page, () => page.getByRole('button', { name: 'Find route' }).tap())
    expect(sent).toEqual(['7', '15', '9', '5', null])
  })
})
