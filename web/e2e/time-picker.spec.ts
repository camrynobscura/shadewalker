import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// The time line under the address fields (PLAN `time-and-layers`),
// Google Maps style: it reads "Leave now" until a departure is set, then
// "Depart <when>". It opens a picker -- LEAVE NOW or DEPART AT (with native date +
// time inputs) -- and DONE applies. A set time rides every /route request
// as month/day/hour/minute and sits in the link as `at`; "leave now"
// sends and stores nothing, so the server's own clock (pinned to July 15
// noon for this suite) decides.

const NIGHT_LINE = 'after dark // the whole city is in shade'

/** The next /route request's time parameters, [month, day, hour, minute];
 * nulls when the request carries no time ("leave now"). */
async function nextRouteTime(page: Page, action: () => Promise<unknown>): Promise<(string | null)[]> {
  const request = page.waitForRequest((r) => r.url().includes('/route?'))
  await action()
  const params = new URL((await request).url()).searchParams
  return ['month', 'day', 'hour', 'minute'].map((key) => params.get(key))
}

function picker(page: Page) {
  return page.getByRole('dialog', { name: 'Start time' })
}

/** Opens the picker from "Leave now" and fills a departure (not applied). */
async function pickDeparture(page: Page, date: string, time: string) {
  await page.getByRole('button', { name: 'Leave now' }).click()
  await picker(page).getByRole('radio', { name: 'Depart at' }).check()
  await picker(page).getByLabel('date', { exact: true }).fill(date)
  await picker(page).getByLabel('time', { exact: true }).fill(time)
}

test('a departure time goes out with the route, shows on the button, and survives a reload', async ({
  page,
}) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(routeDrawn(page)).toBeVisible()

  await page.getByRole('button', { name: 'Leave now' }).click()
  // Opens on LEAVE NOW, focused, with no date or time fields yet.
  const leaveNow = picker(page).getByRole('radio', { name: 'Leave now' })
  await expect(leaveNow).toBeChecked()
  await expect(leaveNow).toBeFocused()
  await expect(picker(page).getByLabel('date', { exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')

  await pickDeparture(page, '2026-09-27', '09:05')
  // Desktop routes by itself: DONE's new time re-routes at once.
  const sent = await nextRouteTime(page, () => page.getByRole('button', { name: 'DONE' }).click())
  expect(sent).toEqual(['9', '27', '9', '5'])

  // The button is the sign this route isn't for right now.
  const button = page.getByRole('button', { name: /^Start time Depart Sun 9\/27, 9:05\sAM$/ })
  await expect(button).toBeVisible()
  await expect(picker(page)).toBeHidden()
  expect(new URL(page.url()).searchParams.get('at')).toBe('2026-09-27T09:05')

  const resent = await nextRouteTime(page, () => page.reload())
  expect(resent).toEqual(['9', '27', '9', '5'])
  await expect(button).toBeVisible()
})

test('LEAVE NOW goes back to now: no time in the request, the link, or on the button', async ({ page }) => {
  await mockGeocode(page)
  const opened = await nextRouteTime(page, () =>
    page.goto(`${routeUrl(POINT_A, POINT_B)}&at=2026-09-27T09:05`),
  )
  expect(opened).toEqual(['9', '27', '9', '5'])

  await page.getByRole('button', { name: /Depart Sun 9\/27/ }).click()
  // Opens on DEPART AT with the set time, not on today.
  await expect(picker(page).getByRole('radio', { name: 'Depart at' })).toBeChecked()
  await expect(picker(page).getByLabel('date', { exact: true })).toHaveValue('2026-09-27')
  await expect(picker(page).getByLabel('time', { exact: true })).toHaveValue('09:05')

  await picker(page).getByRole('radio', { name: 'Leave now' }).check()
  const sent = await nextRouteTime(page, () => page.getByRole('button', { name: 'DONE' }).click())
  expect(sent).toEqual([null, null, null, null])
  await expect(page.getByRole('button', { name: 'Leave now' })).toBeFocused()
  expect(new URL(page.url()).searchParams.has('at')).toBe(false)
})

test('a night departure reaches the real backend', async ({ page }) => {
  // Nothing staged: the suite's backend clock says noon, so the after-dark
  // line can only come from the time the picker sent.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  // The route first: while it loads, the MED row shows this same
  // description in place of its numbers.
  await expect(routeDrawn(page)).toBeVisible()
  await expect(page.getByText('short detours for shadier blocks')).toBeVisible()

  await pickDeparture(page, '2026-07-15', '02:00')
  await page.getByRole('button', { name: 'DONE' }).click()
  await expect(page.getByText(NIGHT_LINE)).toBeVisible()
})

test('a malformed time in a link is ignored: the route is for now', async ({ page }) => {
  await mockGeocode(page)
  const opened = await nextRouteTime(page, () => page.goto(`${routeUrl(POINT_A, POINT_B)}&at=tomorrow`))
  expect(opened).toEqual([null, null, null, null])
  await expect(page.getByRole('button', { name: 'Leave now' })).toBeVisible()
})

test('the time box sits under the address fields, the same width', async ({ page }) => {
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'Route controls and details' })
  const endField = await page.getByRole('combobox', { name: 'End point' }).boundingBox()
  const time = await panel.getByRole('button', { name: 'Leave now' }).boundingBox()
  expect(endField && time).toBeTruthy()
  expect(time!.y).toBeGreaterThan(endField!.y + endField!.height)
  // The third field, not a chip.
  expect(Math.abs(time!.width - endField!.width)).toBeLessThan(2)
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test('the picker takes the whole screen, and CANCEL walks away without applying', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Leave now' }).tap()
    // Like address search: the phone's picker is the screen, not a card.
    const box = await picker(page).boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(389)
    expect(box!.height).toBeGreaterThanOrEqual(843)

    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await picker(page).getByLabel('time', { exact: true }).fill('02:00')
    await picker(page).getByRole('button', { name: 'CANCEL' }).tap()
    await expect(picker(page)).toBeHidden()
    await expect(page.getByRole('button', { name: 'Leave now' })).toBeFocused()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)
  })

  test('the full-screen picker has no accessibility violations', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Leave now' }).tap()
    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await expect(picker(page).getByLabel('date', { exact: true })).toBeVisible()
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })

  test('Escape closes the picker without applying', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Leave now' }).tap()
    await picker(page).getByRole('radio', { name: 'Depart at' }).check()
    await picker(page).getByLabel('time', { exact: true }).fill('02:00')
    await page.keyboard.press('Escape')

    await expect(picker(page)).toBeHidden()
    await expect(page.getByRole('button', { name: 'Leave now' })).toBeFocused()
    expect(new URL(page.url()).searchParams.has('at')).toBe(false)
  })
})
