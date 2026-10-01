import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, POINT_OUTSIDE_COVERAGE, routeDrawn, routeUrl } from './fixtures'

// The app routes by itself at every width: there is no button. A route
// draws as soon as both points exist, and again whenever the trip
// changes. A phone shows the same one panel as desktop, stacked under
// the map: addresses, the pills, the four route rows, directions.

/** Counts /route requests from here on. */
function countRouteRequests(page: Page): () => number {
  let count = 0
  page.on('request', (r) => {
    if (r.url().includes('/route?')) count++
  })
  return () => count
}

test('desktop routes by itself: no button, a route once both points are in', async ({ page }) => {
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

  test('a phone routes by itself too, and a typed address counts without Enter', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')
    await expect(page.getByRole('button', { name: 'Find route' })).toHaveCount(0)
    // One panel: the route rows sit under the addresses before any route.
    await expect(page.getByRole('radio', { name: /^Medium:/ })).toBeVisible()

    await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
    await page.keyboard.press('Enter')
    // Typed, never confirmed with Enter: leaving the field looks it up.
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('3rd St & 3rd Ave')
    await end.blur()

    await expect(routeDrawn(page)).toBeVisible()
    // The addresses stay on screen beside the route's rows.
    await expect(page.getByRole('combobox', { name: 'Start point' })).toBeVisible()
    await expect(page.getByRole('radio', { name: /^Medium:/ })).toBeVisible()
  })

  test('a phone re-routes on its own when an address changes', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    const routeRequests = countRouteRequests(page)

    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('baltic')
    await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
    await expect(end).toHaveValue('Court St & Baltic St, Brooklyn')
    await expect.poll(routeRequests).toBe(1)
  })

  test('an address that finds nothing routes nothing; fixing it routes', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')
    const routeRequests = countRouteRequests(page)

    await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
    await page.keyboard.press('Enter')
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('Nowhere at all')
    await end.blur()
    await expect(page.getByText('NOT_FOUND', { exact: false }).first()).toBeVisible()
    // A "nothing happened" check needs a window to happen in.
    await page.waitForTimeout(300)
    expect(routeRequests()).toBe(0)

    await end.fill('baltic')
    await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
    await expect.poll(routeRequests).toBe(1)
  })

  test('a map tap with a route showing starts a new trip', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    const map = await page.locator('.leaflet-container').boundingBox()
    expect(map).not.toBeNull()

    await page.mouse.click(map!.x + map!.width / 2, map!.y + map!.height / 2)
    // A fresh start: a new A, B dropped, and the old route gone.
    await expect.poll(() => new URL(page.url()).searchParams.has('to')).toBe(false)
    await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('')
    await expect(routeDrawn(page)).toBeHidden()
  })

  test('an error shows over the addresses that caused it', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
    await expect(page.getByRole('alert')).toContainText('outside our current coverage area')
    await expect(page.getByRole('combobox', { name: 'End point' })).toBeVisible()
  })

  test('routing and picking a preset add nothing to Back', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B, 15))
    const entries = await page.evaluate(() => window.history.length)
    await expect(routeDrawn(page)).toBeVisible()

    await page.getByRole('radio', { name: /^Maximum:/ }).check()
    await expect.poll(() => new URL(page.url()).searchParams.get('w')).toBe('40')
    expect(await page.evaluate(() => window.history.length)).toBe(entries)
    expect(await page.evaluate(() => window.history.state)).toBeNull()
  })

  test('the panel with a route has no accessibility violations', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
  })
})
