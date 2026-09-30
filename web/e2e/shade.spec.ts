import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

/** The four Shade_priority presets (NONE / LOW / MED / MAX). /route
 * computes all four in one call and the app displays the selected one,
 * which is what makes the monotonicity guarantee testable from the UI at
 * all. */
const PRESETS = [0, 5, 15, 40]

/** Reads the checked route row's shade as a whole number, from its
 * spoken name ("Medium: 12 minutes, 0.4 miles, 44 percent shaded") -- the
 * words are what carry meaning if the markup changes. */
async function readShadePercent(page: Page): Promise<number> {
  const checked = page.getByRole('radio', { checked: true })
  await expect(checked).toHaveAccessibleName(/percent shaded$/)
  const spoken = (await checked.locator('xpath=following-sibling::span[2]').textContent()) ?? ''
  const value = Number(/(\d+) percent shaded$/.exec(spoken)?.[1])
  expect(Number.isFinite(value)).toBe(true)
  return value
}

async function shadeAtEveryPreset(page: Page): Promise<number[]> {
  const readings: number[] = []
  for (const weight of PRESETS) {
    await page.goto(routeUrl(POINT_A, POINT_B, weight))
    readings.push(await readShadePercent(page))
  }
  return readings
}

/** The regression test for clamp_shade_monotonic.
 *
 * The promise the Shade_priority control makes is that asking for more
 * shade never returns a route reporting less of it. That can genuinely
 * break: cost is hyperbolic per edge while the displayed fraction is
 * linear, so near-tied routes can swap at a higher weight with the
 * winner's fraction a hair lower.
 *
 * Measured 2026-08-24 over 400 citywide routes computed pre-clamp: 3.8% of
 * July routes violate this, 2.0% visibly after rounding to whole percent,
 * worst drop 7 points. So the clamp is load-bearing, not vestigial.
 *
 * This asserts the guarantee through the UI, at whole-percent precision,
 * which is the level a person actually sees. It is month-independent on
 * purpose: monotonicity must hold in every season, whereas how much the
 * presets diverge depends on CANOPY_BY_MONTH and would make this flaky in
 * winter. */
test('raising Shade_priority never lowers the reported shade', async ({ page }) => {
  await mockGeocode(page)
  const readings = await shadeAtEveryPreset(page)

  for (let i = 1; i < readings.length; i++) {
    expect(
      readings[i],
      `preset ${PRESETS[i]} reported ${readings[i]}% but preset ` +
        `${PRESETS[i - 1]} reported ${readings[i - 1]}% -- ` +
        `asking for more shade returned less (readings: ${readings.join(', ')})`,
    ).toBeGreaterThanOrEqual(readings[i - 1])
  }
})

/** The shade path is live, not failing closed: a scoring or loading
 * regression that zeroes every edge would render every preset as "0%"
 * and make every route the shortest walk, with nothing on screen to
 * explain why.
 *
 * Deliberately asserts only that real shade reaches the UI, not a specific
 * value: the number legitimately moves with the month (CANOPY_BY_MONTH runs
 * 1.0 in July down to 0.2 in January) and with any re-scoring of the
 * fixture. */
test('the shade stat carries real data rather than failing closed', async ({ page }) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))

  const shade = await readShadePercent(page)
  expect(shade, 'every preset reading 0% means the shade path is failing closed').toBeGreaterThan(0)
  expect(shade).toBeLessThanOrEqual(100)
})
