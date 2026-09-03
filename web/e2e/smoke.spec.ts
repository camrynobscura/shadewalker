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

test('the About link survives every width, icon form included', async ({ page }) => {
  // ≤720px the header's ABOUT swaps its word for the ⓘ icon (2026-09-03)
  // — one link, two CSS-toggled forms. The risk this guards: both forms
  // hidden at some width leaves phones with NO path to About, and the
  // swap regressing the zoomed-phone wrap (the whole reason the word
  // left) would put ABOUT below the wordmark again. 320px is the reflow
  // floor and the tightest fit for wordmark + icon on one row.
  await page.setViewportSize({ width: 320, height: 667 })
  await page.goto('/')
  const about = page.getByRole('link', { name: 'About Shade Walker' })
  await expect(about).toBeVisible()
  // Same row as the wordmark: the icon's top edge must sit above the
  // wordmark's bottom, or it has wrapped below it.
  const wordmark = page.getByRole('link', { name: 'Shade Walker, home' })
  const [aboutBox, wordmarkBox] = [await about.boundingBox(), await wordmark.boundingBox()]
  expect(aboutBox!.y).toBeLessThan(wordmarkBox!.y + wordmarkBox!.height)

  await about.click()
  await expect(page).toHaveURL(/about\.html/)
  // About's own header keeps its MAP text link — the way back — at
  // every width; only the map page's link has an icon form.
  await expect(page.locator('header').getByRole('link', { name: 'MAP', exact: true })).toBeVisible()

  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')
  await expect(about).toBeVisible()
  await expect(about).toContainText('ABOUT') // the word form, not the icon
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

test("the caution link's fragment lands on the caution section, not the page top", async ({ page }) => {
  // RouteStats' "Learn more" links /about.html#caution. About is a React
  // entry (PR #69): at the moment the browser attempts its native
  // fragment scroll, #root is still empty, so #caution doesn't exist and
  // the scroll silently no-ops at the top -- AboutPage re-scrolls after
  // render. Short viewport so the section genuinely starts off-screen.
  await page.setViewportSize({ width: 800, height: 450 })
  await page.goto('/about.html#caution')
  await expect(page.locator('#caution')).toBeInViewport()
  // Native fragment navigation also moves the reading/tab position to
  // the target; the re-scroll matches that by focusing it.
  await expect(page.locator('#caution')).toBeFocused()
})
