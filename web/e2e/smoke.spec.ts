import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

test('searching two addresses draws a route with stats and directions', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  // Enter after each fill: with FIND_ROUTE gone (2026-09-02) typed text
  // resolves on Enter (or blur) — Enter also dismisses the autocomplete
  // dropdown that fill()'s synthetic typing opens.
  await page.getByRole('combobox', { name: 'Start point' }).fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('combobox', { name: 'End point' }).fill('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')

  await expect(page.getByText('distance', { exact: true })).toBeVisible()
  await expect(page.getByText('trees', { exact: true })).toBeVisible()
  // The directions list is the only <ol> on the page (the map legend below
  // it is a <ul>), so the tag alone disambiguates without a test-only hook.
  await expect(page.locator('ol')).toContainText('Head')

  // Controls.tsx's useAddressField reverse-geocodes start/end whenever they
  // change, from anywhere -- a real risk for the very query text this test
  // just typed, once the resolved point round-trips back down as a prop.
  // It's compared by value against what resolve() already set, precisely
  // so this can't happen; assert the typed text actually survives, not
  // just that the route drew.
  await expect(page.getByRole('combobox', { name: 'Start point' })).toHaveValue('250 Court St')
  await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('3rd St & 3rd Ave')
})

test('the share button copies the route URL to the clipboard', async ({ page }) => {
  // Headless Chromium has no navigator.share, so the button falls to the
  // clipboard path -- grant it and assert both the confirmation and the
  // actual clipboard contents (the shareable URL App keeps in sync).
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByText('distance', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Share route' }).click()
  await expect(page.getByText('link copied', { exact: true })).toBeVisible()

  const clip = await page.evaluate(() => navigator.clipboard.readText())
  expect(clip).toContain('from=')
  expect(clip).toContain('to=')
})

test('loading a shared route URL fills in the address fields via reverse geocoding', async ({ page }) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))

  await expect(page.getByRole('combobox', { name: 'Start point' })).toHaveValue('250 Court St')
  await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('3rd Ave')
})

test('the About page is a scrollable document', async ({ page }) => {
  // Regression: about.css imports index.css for the tokens, and the app's
  // fixed-viewport guard there (html/body overflow:hidden, 2026-08-31)
  // silently clipped this page's scroll until about.css restored document
  // behavior. A short viewport guarantees the prose overflows it.
  await page.setViewportSize({ width: 800, height: 450 })
  await page.goto('/about.html')
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

  await page.evaluate(() => window.scrollTo(0, 9999))
  // With overflow:hidden inherited, scrollTo() is a no-op and scrollY
  // stays 0 -- this asserts real scrolling, not just computed styles.
  const scrolled = await page.evaluate(() => window.scrollY)
  expect(scrolled).toBeGreaterThan(0)
})
