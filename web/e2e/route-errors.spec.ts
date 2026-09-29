import { expect, test } from '@playwright/test'
import { mockGeocode, pauseClockAt, POINT_A, POINT_B, routeUrl } from './fixtures'

// What the panel says when the backend is broken, not merely refusing
// (the refusals -- outside coverage, no path -- are covered elsewhere).
// Every failure is staged with page.route() in the browser, so the real
// test backend is never touched: a request that never answers, a proxy
// error page in place of uvicorn, a rate limit, a dead geocoder.

const ERROR = () => 'alert'

test('a route request that never answers times out with its own message', async ({ page }) => {
  await mockGeocode(page)
  // Paused before navigation so the app's own setTimeout runs on a fake
  // clock that only runFor() moves. An installed-but-running clock keeps
  // ticking with real time through the page load, which ate into the 10s
  // deadline and failed ~1 in 6 runs even on an idle machine (2026-09-25).
  await pauseClockAt(page, new Date('2026-06-15T12:00:00'))
  await page.route('**/route?*', () => {
    /* never fulfilled: a hung worker */
  })
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByText('Finding your route…', { exact: true })).toBeAttached()

  await page.clock.runFor(9_000)
  await expect(page.getByRole(ERROR())).toHaveText('')

  await page.clock.runFor(1_500)
  await expect(page.getByRole(ERROR())).toContainText('took too long')
  await expect(page.getByText('Finding your route…', { exact: true })).toHaveCount(0)
})

test("a proxy error page (uvicorn down behind Caddy/Vite) reads as 'check your connection', never a status code", async ({
  page,
}) => {
  await mockGeocode(page)
  await page.route('**/route?*', (route) =>
    route.fulfill({
      status: 502,
      contentType: 'text/html',
      body: '<html><body>502 Bad Gateway</body></html>',
    }),
  )
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByRole(ERROR())).toContainText("couldn't load the route")
  await expect(page.getByRole(ERROR())).not.toContainText('502')
})

test('a rate-limited route shows the wait message', async ({ page }) => {
  await mockGeocode(page)
  // The body server/app.py's handler sends (one shape for every error).
  await page.route('**/route?*', (route) =>
    route.fulfill({
      status: 429,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'too many requests — wait a moment and try again' }),
    }),
  )
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByRole(ERROR())).toContainText('wait a moment')
})

test('a dead geocoder shows SEARCH_DOWN, not NOT_FOUND, and Enter retries the same text', async ({
  page,
}) => {
  let calls = 0
  await page.route('**/geocode?*', (route) => {
    calls++
    return route.fulfill({
      status: 502,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Geocoding is temporarily unavailable' }),
    })
  })
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  await expect(page.getByText('SEARCH_DOWN')).toBeVisible()
  await expect(page.getByText('NOT_FOUND')).toHaveCount(0)

  // An outage is worth retrying without retyping: the same text again.
  const before = calls
  await page.keyboard.press('Enter')
  await expect.poll(() => calls).toBeGreaterThan(before)
})

test('no network during a geocode shows SEARCH_DOWN instead of a field stuck searching', async ({ page }) => {
  await page.route('**/geocode?*', (route) => route.abort('failed'))
  await page.goto('/')
  await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
  await page.keyboard.press('Enter')
  await expect(page.getByText('SEARCH_DOWN')).toBeVisible()
})
