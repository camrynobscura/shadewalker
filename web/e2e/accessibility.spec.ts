import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, POINT_OUTSIDE_COVERAGE, routeUrl } from './fixtures'

// axe-core catches markup-detectable WCAG issues (missing labels, contrast,
// ARIA misuse) across the app's real, distinct states — not a replacement
// for the manual VoiceOver/Lighthouse passes CLAUDE.md already calls for,
// but a regression net between them.

test('initial load has no violations', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Click the map', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('a drawn route has no violations', async ({ page }) => {
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByText('dist', { exact: true })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('address search with no match has no violations', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')
  await page.getByLabel('Start_point').fill('Nowhere, USA')
  await page.getByRole('button', { name: 'FIND_ROUTE' }).click()
  await expect(page.getByText('NOT_FOUND', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('out-of-coverage rejection has no violations', async ({ page }) => {
  await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
  await expect(page.locator('[aria-live="polite"]')).toContainText('coverage', { ignoreCase: true })

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})
