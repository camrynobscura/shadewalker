import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

/** The four Shade_priority presets, in the order Controls.tsx lists them
 * (NONE / LOW / MED / MAX). /route computes all four in ONE call and the
 * app displays the selected one, which is what makes the monotonicity
 * guarantee testable from the UI at all. */
const PRESETS = [0, 5, 15, 40]

/** Reads the "shaded" stat as a whole number, anchored on its LABEL rather
 * than on a class or DOM position — RouteStats renders `<span>NN%</span>`
 * followed by `<span>shaded</span>`, and the label is the part that carries
 * meaning if the markup is refactored. */
async function readShadePercent(page: Page): Promise<number> {
  const label = page.getByText('shaded', { exact: true })
  await expect(label).toBeVisible()
  const text = await label.locator('xpath=preceding-sibling::span[1]').innerText()
  const value = Number(text.replace('%', '').trim())
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

/** THE REGRESSION TEST FOR clamp_shade_monotonic.
 *
 * The promise the Shade_priority control makes is that asking for more
 * shade never returns a route reporting less of it. That can genuinely
 * break: the router minimises an UNSATURATED density while the displayed
 * stat saturates at SHADE_SATURATION_DENSITY, so the router keeps being
 * rewarded for density the stat has stopped crediting.
 *
 * Measured 2026-08-24 over 400 citywide routes computed PRE-clamp: 3.8% of
 * July routes violate this, 2.0% visibly after rounding to whole percent,
 * worst drop 7 points. So the clamp is load-bearing, not vestigial -- an
 * earlier "0 of 24 sampled pairs" reading suggested otherwise and was
 * simply underpowered (24 samples cannot separate 0% from 3.8%).
 *
 * This asserts the guarantee through the UI, at whole-percent precision,
 * which is the level a person actually sees. It is month-independent on
 * purpose: monotonicity must hold in every season, whereas how MUCH the
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

/** The shade path is LIVE, not failing closed.
 *
 * Until 2026-08-24 graph_store returned zero shade for every route by
 * design: DENSITY_LENGTH_FLOOR_M and SHADE_SATURATION_DENSITY were both
 * None with explicit guards, so nothing the pipeline scored could reach the
 * UI. That was correct then and would be a silent, total regression now --
 * every preset would render "0%" and every route would be the shortest
 * walk, with nothing on screen to explain why.
 *
 * Deliberately asserts only that real shade reaches the UI, not a specific
 * value: the number legitimately moves with the month (CANOPY_BY_MONTH runs
 * 1.0 in July down to 0.2 in January) and with any re-scoring of the
 * fixture. */
test('the shade stat carries real data rather than failing closed', async ({ page }) => {
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))

  const shade = await readShadePercent(page)
  expect(shade, 'every preset reading 0% means the shade path is failing closed')
    .toBeGreaterThan(0)
  expect(shade).toBeLessThanOrEqual(100)
})
