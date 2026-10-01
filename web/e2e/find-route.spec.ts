import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import {
  findRoute,
  mockGeocode,
  POINT_A,
  POINT_B,
  POINT_OUTSIDE_COVERAGE,
  routeDrawn,
  routeUrl,
} from './fixtures'

// FIND_ROUTE and the phone's two screens (#118). On a phone, typed
// addresses and map taps only set points;
// the route waits for FIND_ROUTE, the checkpoint where a wrong address
// is caught and the step to the route screen: a one-line trip box, the
// four route rows, directions; the trip box goes back. Desktop has no
// second screen, so no button: it routes as soon as both points exist.
// A link with both points is a trip already chosen, so it routes at
// once everywhere and a phone opens on the route screen.

/** Counts /route requests from here on. */
function countRouteRequests(page: Page): () => number {
  let count = 0
  page.on('request', (r) => {
    if (r.url().includes('/route?')) count++
  })
  return () => count
}

test('desktop routes by itself: no FIND_ROUTE, a route once both points are in', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Find route' })).toBeHidden()
  // No route yet: the rows show each preset's description, so the line
  // under them that repeats the chosen one (MAX, the default) waits for a route.
  const modeLine = page.getByText(/maximum\s*\/\/\s*longest detours/)
  await expect(page.getByRole('radio', { name: 'Maximum: longest detours for most shade' })).toBeChecked()
  await expect(modeLine).toHaveCount(0)

  await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('combobox', { name: 'End point' }).fill('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')
  await expect(routeDrawn(page)).toBeVisible()
  await expect(modeLine).toBeVisible()
})

test('desktop re-routes on its own when an address changes', async ({ page }) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(routeDrawn(page)).toBeVisible()
  const routeRequests = countRouteRequests(page)

  await page.getByRole('combobox', { name: 'End point' }).fill('baltic')
  await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
  await expect.poll(routeRequests).toBe(1)
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  const tripBox = (page: Page) => page.getByRole('button', { name: /^Change trip:/ })
  const drawnLines = (page: Page) => page.locator('.leaflet-overlay-pane path')

  test('FIND_ROUTE waits for both addresses, and a typed one counts without Enter', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')
    const find = page.getByRole('button', { name: 'Find route' })
    await expect(find).toBeDisabled()

    await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
    await page.keyboard.press('Enter')
    await expect(find).toBeDisabled()
    // Typed, never confirmed with Enter: FIND_ROUTE waits for its lookup.
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('3rd St & 3rd Ave')
    await end.blur()
    await expect(find).toBeEnabled()
    await find.tap()

    await expect(tripBox(page)).toBeVisible()
    await expect(routeDrawn(page)).toBeVisible()
  })

  test('editing an address keeps the drawn route until FIND_ROUTE', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await tripBox(page).tap()
    const routeRequests = countRouteRequests(page)

    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('baltic')
    await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
    await expect(end).toHaveValue('Court St & Baltic St, Brooklyn')
    await page.waitForTimeout(300) // room for a (wrong) request to go out
    expect(routeRequests()).toBe(0)
    await expect(drawnLines(page).first()).toBeAttached()

    await findRoute(page)
    await expect.poll(routeRequests).toBe(1)
    await expect(tripBox(page)).toBeVisible()
  })

  test('an address that finds nothing routes nothing, and nothing jumps ahead later', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')
    const routeRequests = countRouteRequests(page)

    await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
    await page.keyboard.press('Enter')
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('Nowhere at all')
    await end.blur()
    await findRoute(page)
    await expect(page.getByText('NOT_FOUND', { exact: false }).first()).toBeVisible()
    await expect(tripBox(page)).toBeHidden()

    // Fixing the address later is just an edit: no route until asked.
    await end.fill('baltic')
    await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
    await page.waitForTimeout(300)
    expect(routeRequests()).toBe(0)
  })

  test('the route screen and the plan screen swap, and the route stays drawn going back', async ({
    page,
  }) => {
    await mockGeocode(page)
    // A shared link: the trip is chosen, so it opens on the route screen.
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    // Where only: the time pill under it says when.
    await expect(tripBox(page)).toHaveAccessibleName(/250 Court St to 3rd Ave$/)
    await expect(page.getByRole('radio', { name: /^Medium:/ })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Start point' })).toBeHidden()

    await tripBox(page).tap()
    await expect(page.getByRole('combobox', { name: 'Start point' })).toBeVisible()
    await expect(page.getByRole('radio', { name: /^Medium:/ })).toBeHidden()
    await expect(tripBox(page)).toBeHidden()
    // Focus lands where the change is, not on <body>.
    await expect(page.getByRole('button', { name: 'Find route' })).toBeFocused()
    await expect(drawnLines(page).first()).toBeAttached()

    await page.getByRole('button', { name: 'Find route' }).tap()
    await expect(tripBox(page)).toBeVisible()
    await expect(tripBox(page)).toBeFocused()
    await expect(page.getByRole('combobox', { name: 'Start point' })).toBeHidden()
  })

  test('a map tap on the route screen starts a new trip, back on the plan screen', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    const map = await page.locator('.leaflet-container').boundingBox()
    expect(map).not.toBeNull()

    await page.mouse.click(map!.x + map!.width / 2, map!.y + map!.height / 2)
    // A fresh start: a new A, B dropped, and the plan screen to set B on.
    await expect.poll(() => new URL(page.url()).searchParams.has('to')).toBe(false)
    await expect(page.getByRole('combobox', { name: 'End point' })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('')
    await expect(tripBox(page)).toBeHidden()
  })

  test('an error sends the phone back to the plan screen, where the alert is', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
    await expect(page.getByRole('alert')).toContainText('outside our current coverage area')
    await expect(page.getByRole('combobox', { name: 'End point' })).toBeVisible()
    await expect(tripBox(page)).toBeHidden()
    // Its history entry went with it, so the next Back isn't a dead press.
    await expect.poll(() => page.evaluate(() => window.history.state?.screen ?? null)).toBeNull()
  })

  test('Back on the route screen goes to the plan screen, not off the site; Forward is FIND_ROUTE', async ({
    page,
  }) => {
    await mockGeocode(page)
    await page.goto('/')
    await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
    await page.keyboard.press('Enter')
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('3rd St & 3rd Ave')
    await end.blur()
    await findRoute(page)
    await expect(tripBox(page)).toBeVisible()
    await expect(routeDrawn(page)).toBeVisible()

    await page.goBack()
    await expect(page.getByRole('button', { name: 'Find route' })).toBeVisible()
    await expect(tripBox(page)).toBeHidden()
    expect(new URL(page.url()).searchParams.has('to')).toBe(true)

    await page.goForward()
    await expect(tripBox(page)).toBeVisible()
  })

  test('a shared link on a phone: Back goes to its addresses, with the trip as it is now', async ({
    page,
  }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B, 15))
    await expect(routeDrawn(page)).toBeVisible()
    // Picked on the route screen: the plan screen's URL must follow.
    await page.getByRole('radio', { name: /^Maximum:/ }).check()
    await expect.poll(() => new URL(page.url()).searchParams.get('w')).toBe('40')

    await page.goBack()
    await expect(page.getByRole('combobox', { name: 'Start point' })).toBeVisible()
    await expect(tripBox(page)).toBeHidden()
    expect(new URL(page.url()).searchParams.get('w')).toBe('40')
  })

  test('the trip box and FIND_ROUTE go back and forth without piling up history', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    const entries = await page.evaluate(() => window.history.length)

    for (let i = 0; i < 2; i++) {
      await tripBox(page).tap()
      await expect(page.getByRole('button', { name: 'Find route' })).toBeVisible()
      await page.getByRole('button', { name: 'Find route' }).tap()
      await expect(tripBox(page)).toBeVisible()
    }
    expect(await page.evaluate(() => window.history.length)).toBe(entries)

    // And one Back still lands on the plan screen.
    await page.goBack()
    await expect(page.getByRole('button', { name: 'Find route' })).toBeVisible()
  })

  test('two long addresses share the trip line: neither pushes the other off', async ({ page }) => {
    await mockGeocode(page)
    const from = 'Whole Foods Market, 250 7th Avenue, Manhattan'
    const to = "Trader Joe's, 130 Court Street, Brooklyn"
    await page.goto(
      `${routeUrl(POINT_A, POINT_B)}&fromq=${encodeURIComponent(from)}&toq=${encodeURIComponent(to)}`,
    )
    await expect(tripBox(page)).toBeVisible()
    // Read out in full, whatever is cut off on screen.
    await expect(tripBox(page)).toHaveAccessibleName(`Change trip: ${from} to ${to}`)
    const widths = await tripBox(page).evaluate(
      (box, texts) => {
        const places = [...box.querySelectorAll('span')].filter((el) => texts.includes(el.textContent ?? ''))
        return places.map((el) => Math.round(el.getBoundingClientRect().width))
      },
      [from, to],
    )
    expect(widths).toHaveLength(2)
    // Both cut to the same width, and both inside the screen.
    expect(Math.abs(widths[0] - widths[1])).toBeLessThanOrEqual(1)
    expect(widths[0]).toBeGreaterThan(100)
    expect(widths[0] + widths[1]).toBeLessThan(390)
  })

  test('the route screen has no accessibility violations', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await expect(tripBox(page)).toBeVisible()
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })
})
