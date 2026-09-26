import { expect, test, type Page } from '@playwright/test'
import { atNight, mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

// After dark (PLAN `night-shade`): with no sun every street is shade, so
// the server hands all four presets the same fastest route at 100% and the
// Shade_priority box trades its mode hint and comparison for one line.

// The VISIBLE line: its spoken twin shares the words ("After dark. The whole
// city is in shade, so...") but not the "//", so this matches one element.
const NIGHT_LINE = 'after dark // the whole city is in shade'

/** A stat's visible value, anchored on its label (same approach as
 * shade.spec.ts's readShadePercent). */
async function statValue(page: Page, label: string): Promise<string> {
  const labelNode = page.getByText(label, { exact: true })
  await expect(labelNode).toBeVisible()
  return labelNode.locator('xpath=following-sibling::*[1]/span[@aria-hidden="true"]').innerText()
}

test('after dark every preset is the same fastest route, said in one line', async ({ page }) => {
  await mockGeocode(page)
  await atNight(page)
  await page.goto(routeUrl(POINT_A, POINT_B, 0))
  await expect(page.getByText(NIGHT_LINE)).toBeVisible()
  await expect(page.getByText('so every setting gives the fastest route')).toHaveCount(1)
  const eta = await statValue(page, 'eta')
  const distance = await statValue(page, 'distance')

  for (const preset of ['LOW', 'MED', 'MAX']) {
    await page.getByRole('radio', { name: preset }).check()
    await expect(page.getByText(NIGHT_LINE)).toBeVisible()
    await expect(page.getByText(/% shade/)).toHaveCount(0)
    expect(await statValue(page, 'eta')).toBe(eta)
    expect(await statValue(page, 'distance')).toBe(distance)
    expect((await statValue(page, 'shaded')).replace(/\s/g, '')).toBe('100%')
  }
})

test('by day the box compares presets as always', async ({ page }) => {
  // The control for the spec above, and for the pinned clock itself: the
  // test backend's "now" is July 15 at noon, so nothing here is night
  // whatever the wall clock says.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B, 15))
  await expect(page.getByText('short detours for shadier blocks')).toBeVisible()
  await expect(page.getByText(/% shade/).first()).toBeVisible()
  await expect(page.getByText(NIGHT_LINE)).toHaveCount(0)
})
