import { expect, test } from '@playwright/test'
import { mockGeocode } from './fixtures'

test('searching two addresses draws a route with stats and directions', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  await page.getByLabel('Start_point').fill('250 Court St')
  await page.getByLabel('End_point').fill('3rd St & 3rd Ave')
  await page.getByRole('button', { name: 'FIND_ROUTE' }).click()

  await expect(page.getByText('dist', { exact: true })).toBeVisible()
  await expect(page.getByText('trees', { exact: true })).toBeVisible()
  // The directions list is the only <ol> on the page (the map legend below
  // it is a <ul>), so the tag alone disambiguates without a test-only hook.
  await expect(page.locator('ol')).toContainText('Head')
})
