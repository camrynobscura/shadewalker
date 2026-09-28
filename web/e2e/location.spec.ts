import { expect, test } from '@playwright/test'
import { mockGeocode } from './fixtures'

// The use-location-ux pass (2026-09-02): one ⌖ tap = enable + gated fill
// + recenter, errors surfaced instead of an eternal "acquiring", and
// field labels carried in the URL so a reload shows the text the field
// showed. The accuracy gate and settle timeout live in vitest
// (useLocationFill.test.ts) — Playwright's mock geolocation serves one
// fixed position, so these specs cover the wiring around the gate, not
// the gate itself.

test.describe('one-tap location fill', () => {
  // POINT_A's coordinates, so the reverse-geocode mock has a name for the
  // fix ("250 Court St"); accuracy 20 passes the 50m gate immediately.
  test.use({
    geolocation: { latitude: 40.68, longitude: -73.998, accuracy: 20 },
    permissions: ['geolocation'],
  })

  test('a single tap fills the start field from the fix', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')

    await page.getByRole('button', { name: 'Use location' }).click()

    // No second tap: the field fills (reverse-geocoded) on its own, and
    // the accessory slot flips to the clear button that non-empty fields
    // get.
    const start = page.getByRole('combobox', { name: 'Start point' })
    await expect(start).toHaveValue('250 Court St')
    await expect(page.getByRole('button', { name: 'Clear start point' })).toBeVisible()
  })

  test('a tap on Locate me never drops a route point', async ({ page }) => {
    // It sits inside the map, so without Leaflet's click guard (it's a
    // Leaflet control since 2026-09-27) the tap also reached the map.
    await mockGeocode(page)
    await page.goto('/')
    await page.getByRole('button', { name: 'Use location' }).click()
    await expect(page.getByRole('combobox', { name: 'Start point' })).toHaveValue('250 Court St')

    // Start is set and end isn't, so a click reaching the map would make
    // the button's spot the end point.
    await page.getByRole('button', { name: 'Locate me' }).click()
    await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('')
  })
})

test.describe('location denied', () => {
  // No permissions granted: Playwright auto-denies the request, which is
  // exactly the phone flow where the user dismisses the prompt.
  test('the denial is explained, and the button stays as the retry', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')

    const useLocation = page.getByRole('button', { name: 'Use location' })
    await useLocation.click()

    await expect(page.getByText('allow location for this site')).toBeVisible()
    // Not stuck on a disabled "acquiring" ⌖ — the founding bug.
    await expect(useLocation).toBeEnabled()

    // Entering an address makes the advice stale — it clears with the ⌖
    // (the ✕ takes the slot), and returns if the field is cleared again.
    const start = page.getByRole('combobox', { name: 'Start point' })
    await start.fill('court')
    await expect(page.getByText('allow location for this site')).toBeHidden()
    await page.getByRole('button', { name: 'Clear start point' }).click()
    await expect(page.getByText('allow location for this site')).toBeVisible()
  })
})

test('a reload shows the label that was picked, not a re-derived name', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('court')
  const listbox = page.getByRole('listbox', { name: 'Start point suggestions' })
  await listbox.getByRole('option', { name: '250 Court St, Brooklyn' }).click()
  await expect(start).toHaveValue('250 Court St, Brooklyn')
  await expect(page).toHaveURL(/fromq=250\+Court\+St/)

  await page.reload()
  // The tell: reverse-geocoding this coordinate returns plain
  // "250 Court St" (no borough) in the mock — so the suffix surviving the
  // reload proves the URL label was used and no reverse lookup ran.
  await expect(start).toHaveValue('250 Court St, Brooklyn')
})
