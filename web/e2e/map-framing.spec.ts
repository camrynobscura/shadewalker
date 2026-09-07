import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

/** The steady-preset-camera guarantee (MapView's RouteFraming guard):
 * flipping Shade_priority holds the camera still while the newly
 * selected route already fits in the padded view, and still reframes
 * when it doesn't -- e.g. switching presets while zoomed in. Every test
 * emulates reduced motion so camera moves apply instantly instead of
 * animating under the assertions.
 *
 * The camera signal is the START MARKER's on-screen bounding box, not
 * Leaflet's pane transform: the marker's position is the user-visible
 * truth, and a pane transform can survive some camera changes (Leaflet
 * resets its pixel origin on zoom). */

async function markerBox(page: Page) {
  const box = await page.locator('[class*="markerStart"]').boundingBox()
  expect(box).not.toBeNull()
  return { x: Math.round(box!.x), y: Math.round(box!.y) }
}

/** Waits until the marker stops moving between consecutive reads, then
 * returns where it settled. */
async function settledMarkerBox(page: Page) {
  let prev = await markerBox(page)
  await expect
    .poll(
      async () => {
        const now = await markerBox(page)
        const stable = now.x === prev.x && now.y === prev.y
        prev = now
        return stable
      },
      { intervals: [150, 150, 150, 300, 300, 600] },
    )
    .toBe(true)
  return prev
}

async function drawRoute(page: Page) {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockGeocode(page)
  // Start at NONE so the switch to MAX below crosses the widest gap the
  // presets offer -- the pair most likely to have different bounds.
  await page.goto(routeUrl(POINT_A, POINT_B, 0))
  await expect(page.getByText('shaded', { exact: true })).toBeVisible()
}

test('switching presets holds the camera while the route stays in view', async ({ page }) => {
  await drawRoute(page)
  const framed = await settledMarkerBox(page)

  await page.getByRole('radio', { name: 'MAX' }).check()
  // Prove a negative: give a (wrong) refit a real window to land before
  // reading. All four routes arrive in the one /route call, so the
  // preset swap itself is synchronous -- nothing async to wait out.
  await page.waitForTimeout(400)

  expect(await markerBox(page)).toEqual(framed)
})

test('a preset switch that pushes the route off screen still reframes', async ({ page }) => {
  await drawRoute(page)
  await settledMarkerBox(page)

  // Zoom in far enough that the route can't fit the view any more; the
  // camera holds its center, so the endpoints slide off screen.
  const zoomIn = page.locator('.leaflet-control-zoom-in')
  for (let i = 0; i < 3; i++) {
    await zoomIn.click()
    await settledMarkerBox(page)
  }
  const zoomed = await settledMarkerBox(page)

  await page.getByRole('radio', { name: 'MAX' }).check()
  await expect.poll(() => markerBox(page)).not.toEqual(zoomed)

  // And not just "moved": the refit must bring both endpoints back into
  // the visible map.
  const mapBox = await page.locator('.leaflet-container').boundingBox()
  expect(mapBox).not.toBeNull()
  for (const marker of ['markerStart', 'markerEnd']) {
    const box = await page.locator(`[class*="${marker}"]`).boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(mapBox!.x)
    expect(box!.y).toBeGreaterThanOrEqual(mapBox!.y)
    expect(box!.x + box!.width).toBeLessThanOrEqual(mapBox!.x + mapBox!.width)
    expect(box!.y + box!.height).toBeLessThanOrEqual(mapBox!.y + mapBox!.height)
  }
})
