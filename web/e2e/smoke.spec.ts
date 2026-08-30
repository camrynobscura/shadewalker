import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

test('searching two addresses draws a route with stats and directions', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  // Escape after each fill: fill() counts as typing, so the autocomplete
  // dropdown opens after its debounce and would float over the next
  // control this test needs to reach (the listbox deliberately overlays
  // rather than pushes). Dismissing it is exactly what a person does too.
  await page.getByLabel('Start_point').fill('250 Court St')
  await page.keyboard.press('Escape')
  await page.getByLabel('End_point').fill('3rd St & 3rd Ave')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'FIND_ROUTE' }).click()

  await expect(page.getByText('dist', { exact: true })).toBeVisible()
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
  await expect(page.getByLabel('Start_point')).toHaveValue('250 Court St')
  await expect(page.getByLabel('End_point')).toHaveValue('3rd St & 3rd Ave')
})

test('loading a shared route URL fills in the address fields via reverse geocoding', async ({ page }) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))

  await expect(page.getByLabel('Start_point')).toHaveValue('250 Court St')
  await expect(page.getByLabel('End_point')).toHaveValue('3rd Ave')
})
