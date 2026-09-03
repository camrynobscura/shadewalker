import { expect, test } from '@playwright/test'
import { mockGeocode } from './fixtures'

// Field labels ride the URL (2026-09-02): a reload must show the text the
// field showed, not a fresh reverse-geocode of the bare coordinate (which
// names the NEAREST thing — "Court Street" reloading as a Montague Street
// address — and shows raw coordinates when the lookup fails).

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
