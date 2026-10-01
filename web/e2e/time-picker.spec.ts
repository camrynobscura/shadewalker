import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, pauseClockAt, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// The time pill (#118): a small pill under the address fields that reads "Leave now"
// until a time is set, then "Depart <when>" or "Arrive <when>". It opens
// a small white menu under it (PillMenu):
// Leave now, Depart at or Arrive by, the last two with native date + time
// inputs. Everything applies as you go, on a phone too -- a pick at once,
// typing after a pause. Leave now closes the menu; so do a tap outside
// (which must never reach the map), Escape, Enter and tabbing away. A set
// time rides every /route request as month/day/hour/minute (plus
// arrive=true for an arrival) and sits in the link as `at` or `arrive`;
// "leave now" sends and stores nothing, so the server's own clock (pinned
// to July 15 noon for this suite) decides.
//
// This spec also runs in WebKit (playwright.config's `webkit` project):
// Safari doesn't focus a tapped radio -- focus goes to the panel -- and a
// menu that reads that as "focus left, close" loses the tap meant to
// pick. Chromium can't see that.

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

/** The open menu's choices (its contents exist only while it's open). */
function menu(page: Page) {
  return page.getByRole('group', { name: 'Start time' })
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
    // Opens on Leave now, focused, with no date or time fields yet.
    const leaveNow = page.getByRole('radio', { name: 'Leave now' })
    await expect(leaveNow).toBeChecked()
    await expect(leaveNow).toBeFocused()
    await expect(page.getByLabel('date', { exact: true })).toHaveCount(0)

    // A pick re-routes at once, for the time the fields show: now. The
    // menu stays open for the date and time.
    const picked = await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())
    expect(picked.slice(0, 4).every((part) => part !== null)).toBe(true)
    expect(picked[4]).toBeNull()
    await expect(page.getByLabel('date', { exact: true })).toBeVisible()

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

  test("a time's quick changes wait for a pause, then route once", async ({ page }) => {
    await mockGeocode(page)
    // Paused: only runFor() moves the pause's timer, so the test can stand
    // at 499ms and at 500ms exactly.
    await pauseClockAt(page, new Date('2026-07-15T12:00:00'))
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await timePill(page, 'Leave now').click()
    await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())

    const routes: string[] = []
    page.on('request', (r) => {
      if (r.url().includes('/route?')) routes.push(r.url())
    })
    // A time field changes value at every step: each key typed on desktop
    // (13:00 passes through 01:00), each notch of a phone's wheel. fill()
    // makes those changes the same way in every engine; what keys do to a
    // time field doesn't (Linux WebKit ignores them).
    const field = page.getByLabel('time (NYC)')
    for (const time of ['01:00', '13:00', '13:15']) {
      await field.fill(time)
      await page.clock.runFor(200)
    }
    await page.clock.runFor(299) // 499ms since the last change
    // A "nothing was sent" check needs a moment for a request to show up.
    await page.waitForTimeout(300)
    expect(routes).toHaveLength(0)

    const sent = await nextRouteTime(page, () => page.clock.runFor(1))
    expect(sent).toEqual(['7', '15', '13', '15', null])
    await page.waitForTimeout(300)
    expect(routes).toHaveLength(1)
  })

  test('closing the menu sends a time typed just before, without waiting for the pause', async ({ page }) => {
    await mockGeocode(page)
    // Paused: the pause's timer can't fire, so only the close can send it.
    await pauseClockAt(page, new Date('2026-07-15T12:00:00'))
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await timePill(page, 'Leave now').click()
    await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())
    await page.getByLabel('time (NYC)').fill('09:05')

    const sent = await nextRouteTime(page, () => page.keyboard.press('Escape'))
    expect(sent.slice(2)).toEqual(['9', '5', null])
    await expect(menu(page)).toBeHidden()
  })

  test('Leave now goes back to now and closes the menu', async ({ page }) => {
    await mockGeocode(page)
    const opened = await nextRouteTime(page, () =>
      page.goto(`${routeUrl(POINT_A, POINT_B)}&at=2026-09-27T09:05`),
    )
    expect(opened).toEqual(['9', '27', '9', '5', null])

    await timePill(page, /Depart 9\/27/).click()
    // Opens on Depart at with the set time, not on today.
    await expect(page.getByRole('radio', { name: 'Depart at' })).toBeChecked()
    await expect(page.getByLabel('date', { exact: true })).toHaveValue('2026-09-27')
    await expect(page.getByLabel('time (NYC)')).toHaveValue('09:05')

    // click, not check: the pick closes the menu, so there's no radio left
    // to confirm as checked.
    const sent = await nextRouteTime(page, () => page.getByRole('radio', { name: 'Leave now' }).click())
    expect(sent).toEqual([null, null, null, null, null])
    await expect(menu(page)).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeVisible()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)
  })

  test('Escape closes the menu and hands focus back to the pill', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').click()
    await expect(page.getByRole('radio', { name: 'Leave now' })).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(menu(page)).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeFocused()
    await expect(timePill(page, 'Leave now')).toHaveAttribute('aria-expanded', 'false')
  })

  test('from the keyboard: arrows move the pick and keep the menu open, Enter closes it', async ({
    page,
  }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await timePill(page, 'Leave now').focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('radio', { name: 'Leave now' })).toBeFocused()

    // An arrow is a pick (it re-routes), not the end of choosing.
    const sent = await nextRouteTime(page, () => page.keyboard.press('ArrowDown'))
    expect(sent[0]).not.toBeNull()
    await expect(page.getByRole('radio', { name: 'Depart at' })).toBeChecked()
    await expect(page.getByLabel('date', { exact: true })).toBeVisible()

    await page.keyboard.press('Enter')
    await expect(menu(page)).toBeHidden()
    await expect(timePill(page, /Depart/)).toBeFocused()
  })

  test('focus leaving the menu closes it', async ({ page }) => {
    // What Tab past the last field does. Not pressed here: Chrome's Tab
    // walks a time input's hour, minute and AM/PM first, and Safari's Tab
    // skips buttons and radios unless a system setting says otherwise.
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await timePill(page, 'Leave now').click()
    await nextRouteTime(page, () => page.getByRole('radio', { name: 'Depart at' }).check())
    await page.getByLabel('time (NYC)').focus()
    await page.getByRole('combobox', { name: 'End point' }).focus()
    await expect(menu(page)).toBeHidden()
    await expect(page.getByRole('combobox', { name: 'End point' })).toBeFocused()
  })

  test('a tap outside closes the menu and never reaches the map', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').click()
    await expect(menu(page)).toBeVisible()
    const map = (await page.locator('.leaflet-container').boundingBox())!
    await page.mouse.click(map.x + map.width / 2, map.y + map.height / 2)
    await expect(menu(page)).toBeHidden()
    // A map tap sets the start point; this one only closed the menu.
    expect(new URL(page.url()).searchParams.has('from')).toBe(false)
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
    // This request's own reply: the Arrive by pick's (for now) may land later.
    const body = await (await (await request).response())!.json()
    const noneMinutes = body.routes.find(
      (r: { properties: { tree_weight: number } }) => r.properties.tree_weight === 0,
    ).properties.minutes
    const leave = 13 * 60 - Math.round(noneMinutes)
    expect(body.hour * 60 + body.minute).toBe(leave)
    const clock = `${((Math.floor(leave / 60) + 11) % 12) + 1}:${String(leave % 60).padStart(2, '0')}`
    // The menu floats over the rows; close it to read them.
    await page.keyboard.press('Escape')
    await expect(
      page.getByRole('radio', { name: new RegExp(`^None: leave by ${clock}\\s[AP]M, `) }),
    ).toHaveCount(1)
    for (const preset of ['Low', 'Medium', 'Maximum']) {
      await expect(
        page.getByRole('radio', { name: new RegExp(`^${preset}: leave by \\d{1,2}:\\d{2}\\s[AP]M, `) }),
      ).toHaveCount(1)
    }

    // A reload keeps the arrival, and the menu reopens on it.
    const resent = page.waitForRequest((r) => r.url().includes('/route?'))
    await page.reload()
    expect(new URL((await resent).url()).searchParams.get('arrive')).toBe('true')
    await timePill(page, /Arrive 7\/15/).click()
    await expect(page.getByRole('radio', { name: 'Arrive by' })).toBeChecked()
    await expect(page.getByLabel('time (NYC)')).toHaveValue('13:00')
  })

  test('a night departure reaches the real backend', async ({ page }) => {
    // Nothing staged: the suite's backend clock says noon, so the after-dark
    // line can only come from the time the menu sent.
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
    // A pill, not a third field: narrower and shorter.
    expect(pill!.width).toBeLessThan(endField!.width / 2)
    expect(pill!.height).toBeLessThan(endField!.height)
  })
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test('the menu opens under the pill, a tap picks, and it has no accessibility violations', async ({
    page,
  }) => {
    await page.goto('/')
    const pill = timePill(page, 'Leave now')
    await pill.tap()
    // A menu under the pill, not the whole screen.
    const pillBox = (await pill.boundingBox())!
    const box = (await menu(page).boundingBox())!
    expect(box.y).toBeGreaterThan(pillBox.y + pillBox.height)
    expect(box.width).toBeLessThan(390 - 2 * 16)

    // The tap picks and the menu stays open -- Safari's focus rules would
    // otherwise close it.
    await page.getByRole('radio', { name: 'Depart at' }).tap()
    await expect(page.getByLabel('date', { exact: true })).toBeVisible()
    await expect(timePill(page, /Depart/)).toBeVisible()
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })

  test('Escape closes the menu and hands focus back to the pill', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').tap()
    await expect(menu(page)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(menu(page)).toBeHidden()
    await expect(timePill(page, 'Leave now')).toBeFocused()
  })

  test('a tap outside closes the menu and never reaches the map', async ({ page }) => {
    await page.goto('/')
    await timePill(page, 'Leave now').tap()
    await expect(menu(page)).toBeVisible()
    const map = (await page.locator('.leaflet-container').boundingBox())!
    await page.touchscreen.tap(map.x + map.width / 2, map.y + map.height / 2)
    await expect(menu(page)).toBeHidden()
    expect(new URL(page.url()).searchParams.has('from')).toBe(false)
  })

  test('a shared arrival shows on the pill', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(`${routeUrl(POINT_A, POINT_B)}&arrive=2026-07-15T13:00`)
    await expect(routeDrawn(page)).toBeVisible()
    await expect(timePill(page, /Arrive 7\/15, 1:00\sPM$/)).toBeVisible()
  })

  test('a time change re-routes as you go', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await timePill(page, 'Leave now').tap()
    // No DONE: a pick re-routes at once...
    const picked = await nextRouteTime(page, () => page.getByRole('radio', { name: 'Arrive by' }).tap())
    expect(picked[4]).toBe('true')
    // ...and typed fields after the pause, the way Shade_priority is instant.
    const sent = await nextRouteTime(page, () => fillWhen(page, '2026-07-15', '13:00'))
    expect(sent).toEqual(['7', '15', '13', '0', 'true'])
    await expect(timePill(page, /Arrive 7\/15/)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('radio', { name: /^Medium: leave by / })).toBeVisible()
  })
})
