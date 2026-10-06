import { expect, test, type Page } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

/** The steady-preset-camera guarantee (MapView's RouteFraming guard):
 * flipping Shade_priority holds the camera still while the newly
 * selected route already fits in the padded view, and still reframes
 * when it doesn't -- e.g. switching presets while zoomed in. Every test
 * emulates reduced motion so camera moves apply instantly instead of
 * animating under the assertions.
 *
 * The camera signal is the start marker's on-screen bounding box, not
 * Leaflet's pane transform: the marker's position is the user-visible
 * truth, and a pane transform can survive some camera changes (Leaflet
 * resets its pixel origin on zoom). */

type MarkerClass = 'markerStart' | 'markerEnd'

async function markerBox(page: Page, marker: MarkerClass = 'markerStart') {
  const box = await page.locator(`[class*="${marker}"]`).boundingBox()
  expect(box).not.toBeNull()
  return { x: Math.round(box!.x), y: Math.round(box!.y) }
}

/** Waits until the marker stops moving between consecutive reads, then
 * returns where it settled. */
async function settledMarkerBox(page: Page, marker: MarkerClass = 'markerStart') {
  let prev = await markerBox(page, marker)
  await expect
    .poll(
      async () => {
        const now = await markerBox(page, marker)
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
  await expect(routeDrawn(page)).toBeVisible()
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

/** How far a marker's centre sits from the map's centre, in px. */
async function offCentre(page: Page, marker: MarkerClass) {
  const map = (await page.locator('.leaflet-container').boundingBox())!
  const box = (await page.locator(`[class*="${marker}"]`).boundingBox())!
  return {
    dx: Math.abs(box.x + box.width / 2 - (map.x + map.width / 2)),
    dy: Math.abs(box.y + box.height / 2 - (map.y + map.height / 2)),
  }
}

/** The zoom levels of the tiles on the map, from their URLs
 * (.../{z}/{x}/{y}.png). Only numbers leave the page: a built tile URL
 * can carry the CARTO key. */
async function tileZooms(page: Page) {
  await expect(page.locator('img.leaflet-tile').first()).toBeAttached()
  return page.locator('img.leaflet-tile').evaluateAll((imgs) => {
    const zooms = imgs.map((img) => {
      const parts = new URL((img as HTMLImageElement).src).pathname.split('/')
      return Number(parts[parts.length - 3])
    })
    return [...new Set(zooms)]
  })
}

// A lone point -- A or B with the other still empty. RouteFraming ignores
// one on purpose (a map tap lands where you're looking), but an address
// typed or picked in a field is usually somewhere else: otherwise its
// marker lands off-screen and the pick looks like it did nothing. The
// map pans to it, keeping the zoom.
test.describe('a lone address', () => {
  test.beforeEach(async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await mockGeocode(page)
  })

  test('typed for B with A empty: the map pans to it, same zoom', async ({ page }) => {
    await page.goto('/')
    const zooms = await tileZooms(page)
    const end = page.getByRole('combobox', { name: 'End point' })
    await end.fill('3rd St & 3rd Ave')
    await end.press('Enter')
    await expect(page.locator('[class*="markerEnd"]')).toBeAttached()
    await settledMarkerBox(page, 'markerEnd')

    const off = await offCentre(page, 'markerEnd')
    expect(off.dx).toBeLessThan(3)
    expect(off.dy).toBeLessThan(3)
    expect(await tileZooms(page)).toEqual(zooms)
  })

  test('already in view: the map holds still', async ({ page }) => {
    // ~60px north-east of POINT_A: inside the padded view once the map is
    // centred there, so panning to it would only jostle the map.
    await page.route('**/geocode?*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ results: [{ lat: 40.682, lon: -73.996, label: 'Near St, Brooklyn' }] }),
      }),
    )
    await page.goto(`/?from=${POINT_A}`)
    await settledMarkerBox(page)
    const start = page.getByRole('combobox', { name: 'Start point' })
    await start.fill('near')
    await start.press('Enter')
    await expect.poll(async () => (await offCentre(page, 'markerStart')).dy).toBeGreaterThan(20)
    await page.waitForTimeout(400) // a (wrong) pan gets a real window to land

    const off = await offCentre(page, 'markerStart')
    expect(off.dx).toBeGreaterThan(20)
    expect(off.dy).toBeGreaterThan(20)
  })

  test.describe('on a phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

    test('picked for A: the map pans to it, same zoom', async ({ page }) => {
      await page.goto('/')
      const zooms = await tileZooms(page)
      const start = page.getByRole('combobox', { name: 'Start point' })
      await start.tap()
      await start.pressSequentially('250 court', { delay: 30 })
      await page.getByRole('listbox', { name: 'Start point suggestions' }).getByRole('option').first().tap()
      await expect(page.getByRole('button', { name: 'CANCEL' })).toBeHidden()
      await settledMarkerBox(page)

      const off = await offCentre(page, 'markerStart')
      expect(off.dx).toBeLessThan(3)
      expect(off.dy).toBeLessThan(3)
      expect(await tileZooms(page)).toEqual(zooms)
    })

    test('a link with only A opens on it', async ({ page }) => {
      await page.goto(`/?from=${POINT_A}`)
      await settledMarkerBox(page)
      const off = await offCentre(page, 'markerStart')
      expect(off.dx).toBeLessThan(3)
      expect(off.dy).toBeLessThan(3)
    })

    test('a map tap still leaves the map where it is', async ({ page }) => {
      await page.goto('/')
      const map = (await page.locator('.leaflet-container').boundingBox())!
      // Inside the fit's 60px side padding: a pan would pull it to the middle.
      const tap = { x: map.x + 30, y: map.y + map.height / 2 }
      await page.touchscreen.tap(tap.x, tap.y)
      await page.waitForTimeout(400)
      const box = await settledMarkerBox(page)
      const size = (await page.locator('[class*="markerStart"]').boundingBox())!
      expect(Math.abs(box.x + size.width / 2 - tap.x)).toBeLessThan(3)
      expect(Math.abs(box.y + size.height / 2 - tap.y)).toBeLessThan(3)
    })
  })
})

test.describe('on a short phone map', () => {
  // An iPhone SE's Safari: a 375x220 map (40svh). The desktop padding's
  // 60px top + 100px bottom would leave the pair 60px of it -- this trip
  // framed at zoom 9, its markers 37px apart.
  test.use({ viewport: { width: 375, height: 550 }, hasTouch: true })

  test('a Brooklyn-Manhattan pair frames a zoom step closer than the desktop padding allows', async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await mockGeocode(page)
    // Outside the pilot fixture, so no route draws: the pair frames the
    // moment B lands.
    const places: Record<string, { lat: number; lon: number }> = {
      court: { lat: 40.68, lon: -73.998 },
      times: { lat: 40.758, lon: -73.9855 },
    }
    await page.route('**/geocode?*', (route) => {
      const q = new URL(route.request().url()).searchParams.get('q') ?? ''
      const results = Object.entries(places)
        .filter(([key]) => key.startsWith(q.toLowerCase()))
        .map(([key, point]) => ({ ...point, label: key }))
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ results }),
      })
    })
    await page.goto('/')
    for (const [name, text] of [
      ['Start point', 'court'],
      ['End point', 'times'],
    ]) {
      const field = page.getByRole('combobox', { name })
      await field.tap()
      await field.pressSequentially(text, { delay: 30 })
      await page
        .getByRole('listbox', { name: `${name} suggestions` })
        .getByRole('option')
        .first()
        .tap()
    }
    await expect(page.locator('[class*="markerEnd"]')).toBeAttached()
    await settledMarkerBox(page, 'markerEnd')

    const a = (await page.locator('[class*="markerStart"]').boundingBox())!
    const b = (await page.locator('[class*="markerEnd"]').boundingBox())!
    expect(a.y - b.y).toBeGreaterThan(60)
  })
})

// The landing view: with no route the map opens pulled back on a piece of
// the city with water and bridges in it (Barclays Center up to Washington
// Square Park), not on a close-up of street grid, which was zoom 15. The
// frame is two corners, so the zoom depends on the map's size; a link with
// one point still opens close-up on that point.
test('with no route the map opens pulled back, and a one-point link still opens close-up', async ({
  page,
}) => {
  await mockGeocode(page)
  await page.goto('/')
  const [landing, ...others] = await tileZooms(page)
  expect(others).toEqual([])
  expect(landing).toBeGreaterThanOrEqual(12)
  expect(landing).toBeLessThanOrEqual(14)

  await page.goto(`/?from=${POINT_A}`)
  await settledMarkerBox(page)
  expect(await tileZooms(page)).toEqual([15])
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test("the landing view pulls back further to fit the same area in a phone's short map", async ({
    page,
  }) => {
    await page.goto('/')
    const zooms = await tileZooms(page)
    expect(zooms).toHaveLength(1)
    expect(zooms[0]).toBeGreaterThanOrEqual(11)
    expect(zooms[0]).toBeLessThanOrEqual(13)
  })

  test('two tapped points frame in the middle of the map, not its right side', async ({ page }) => {
    // A wide left padding "for the legend" would be half a phone's map,
    // framing a tapped pair into its right side and zoomed out. The
    // legend is kept clear by the bottom padding instead.
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto('/')
    const map = (await page.locator('.leaflet-container').boundingBox())!
    await page.touchscreen.tap(map.x + map.width * 0.4, map.y + map.height * 0.45)
    await expect(page.locator('[class*="markerStart"]')).toBeVisible()
    // A person's pace: two taps in quick succession are a double-tap, which
    // Leaflet reads as zoom in (Chromium fired it here, WebKit didn't).
    await page.waitForTimeout(600)
    await page.touchscreen.tap(map.x + map.width * 0.6, map.y + map.height * 0.55)
    await expect(page.locator('[class*="markerEnd"]')).toBeVisible()
    await settledMarkerBox(page)

    const centre = async (selector: string) => {
      const box = (await page.locator(selector).boundingBox())!
      return box.x + box.width / 2 - map.x
    }
    const midpoint = ((await centre('[class*="markerStart"]')) + (await centre('[class*="markerEnd"]'))) / 2
    expect(Math.abs(midpoint - map.width / 2)).toBeLessThan(12)
  })
})
