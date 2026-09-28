import { expect, test, type Page } from '@playwright/test'
import { atNight, mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// After dark (PLAN `night-shade`): with no sun every street is shade, so
// the server hands all four presets the same fastest route at 100% and the
// Shade_priority box trades its mode hint for one line.

// The VISIBLE line: its spoken twin shares the words ("After dark. The whole
// city is in shade, so...") but not the "//", so this matches one element.
const NIGHT_LINE = 'after dark // the whole city is in shade'

/** A route row's spoken numbers ("12 minutes, 0.4 miles, 100 percent
 * shaded"), minus its preset name: the rows replaced the stats box whose
 * labels this used to read (2026-09-27). */
async function rowNumbers(page: Page, preset: string): Promise<string> {
  const radio = page.getByRole('radio', { name: new RegExp(`^${preset}:`) })
  await expect(radio).toBeVisible()
  const spoken = await radio.locator('xpath=following-sibling::span[2]').textContent()
  return (spoken ?? '').replace(/^[^:]*: /, '')
}

test('after dark every preset is the same fastest route, said in one line', async ({ page }) => {
  await mockGeocode(page)
  await atNight(page)
  await page.goto(routeUrl(POINT_A, POINT_B, 0))
  await expect(page.getByText(NIGHT_LINE)).toBeVisible()
  await expect(page.getByText('so every setting gives the fastest route')).toHaveCount(1)
  const fastest = await rowNumbers(page, 'None')
  expect(fastest).toMatch(/, 100 percent shaded$/)

  for (const preset of ['Low', 'Medium', 'Maximum']) {
    expect(await rowNumbers(page, preset)).toBe(fastest)
    await page.getByRole('radio', { name: new RegExp(`^${preset}:`) }).check()
    await expect(page.getByText(NIGHT_LINE)).toBeVisible()
  }
})

test('by day the box describes the chosen preset as always', async ({ page }) => {
  // The control for the spec above, and for the pinned clock itself: the
  // test backend's "now" is July 15 at noon, so nothing here is night
  // whatever the wall clock says.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B, 15))
  // The route first: while it loads, the MED row shows this same
  // description in place of its numbers.
  await expect(routeDrawn(page)).toBeVisible()
  await expect(page.getByText('short detours for shadier blocks')).toBeVisible()
  await expect(page.getByText(NIGHT_LINE)).toHaveCount(0)
})
