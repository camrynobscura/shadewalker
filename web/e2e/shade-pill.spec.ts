import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { atNight, mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// The shade pill (PLAN `time-and-layers` 2b): beside the time pill, it
// reads "All shade" until set, then "Tree shade" or "Building shade", and
// opens the same small white menu (PillMenu). A pick applies and closes
// it. A set pick rides every /route request as `layers` and sits in the
// link; all shade sends and stores nothing (the server's default). With
// one kind off the rows count only the other (the server's
// shade_fraction does that; test_server_shade_time.py pins it) and
// LOW_SHADE says nothing -- "expect mostly direct sun" could be false. After
// dark every kind is full shade: "Tree shade" too.
//
// Runs in WebKit as well (playwright.config's `webkit` project): the menu
// is PillMenu's, whose Safari focus rules Chromium can't see.

const NIGHT_LINE = 'after dark // the whole city is in shade'

/** The next /route request's `layers` (null for all shade). */
async function nextRouteLayers(page: Page, action: () => Promise<unknown>): Promise<string | null> {
  const request = page.waitForRequest((r) => r.url().includes('/route?'))
  await action()
  return new URL((await request).url()).searchParams.get('layers')
}

/** The visible shade pill (a phone has one per screen; only one shows). */
function shadePill(page: Page, words: string) {
  return page.getByRole('button', { name: `Shade from: ${words}` })
}

/** The open menu's choices (its contents exist only while it's open). */
function menu(page: Page) {
  return page.getByRole('group', { name: 'Shade from' })
}

test.describe('on desktop', () => {
  test('a pick goes out with the route, shows on the pill, rides the link and survives a reload', async ({
    page,
  }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await shadePill(page, 'All shade').click()
    const all = page.getByRole('radio', { name: 'All shade' })
    await expect(all).toBeChecked()
    await expect(all).toBeFocused()

    // click, not check: the pick closes the menu, so there's no radio left
    // to confirm as checked.
    const sent = await nextRouteLayers(page, () => page.getByRole('radio', { name: 'Tree shade' }).click())
    expect(sent).toBe('trees')
    await expect(menu(page)).toBeHidden()
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
    expect(new URL(page.url()).searchParams.get('layers')).toBe('trees')

    const resent = await nextRouteLayers(page, () => page.reload())
    expect(resent).toBe('trees')
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
  })

  test('All shade goes back: no layers in the request or the link', async ({ page }) => {
    await mockGeocode(page)
    const opened = await nextRouteLayers(page, () =>
      page.goto(`${routeUrl(POINT_A, POINT_B)}&layers=buildings`),
    )
    expect(opened).toBe('buildings')

    await shadePill(page, 'Building shade').click()
    await expect(page.getByRole('radio', { name: 'Building shade' })).toBeChecked()
    const sent = await nextRouteLayers(page, () => page.getByRole('radio', { name: 'All shade' }).click())
    expect(sent).toBeNull()
    await expect(shadePill(page, 'All shade')).toBeVisible()
    expect(new URL(page.url()).searchParams.has('layers')).toBe(false)
  })

  test('a malformed layers in a link is all shade', async ({ page }) => {
    await mockGeocode(page)
    const opened = await nextRouteLayers(page, () => page.goto(`${routeUrl(POINT_A, POINT_B)}&layers=sun`))
    expect(opened).toBeNull()
    await expect(shadePill(page, 'All shade')).toBeVisible()
  })

  test('LOW_SHADE speaks only for all shade', async ({ page }) => {
    // Every route made low-shade on its way back, so the warning's rule is
    // the only thing that differs between the two loads.
    await mockGeocode(page)
    await page.route('**/route?*', async (route) => {
      const body = await (await route.fetch()).json()
      for (const feature of body.routes) feature.properties.shade_fraction = 0.05
      await route.fulfill({ json: body })
    })
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    const warning = page.getByText('LOW SHADE:', { exact: true })
    await expect(warning).toBeAttached()

    await nextRouteLayers(page, async () => {
      await shadePill(page, 'All shade').click()
      await page.getByRole('radio', { name: 'Tree shade' }).click()
    })
    await expect(routeDrawn(page)).toBeVisible()
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
    await expect(warning).toHaveCount(0)
    // A request the app dropped (superseded) can still be in the rewriter
    // as the page closes; that must not fail the run.
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })

  test('after dark, Tree shade is full shade too: one line, every preset 100%', async ({ page }) => {
    // The real backend at 02:00 (atNight adds the time): #112 had let the
    // trees-alone view keep its own night; night now beats the layer.
    await mockGeocode(page)
    await atNight(page)
    await page.goto(`${routeUrl(POINT_A, POINT_B)}&layers=trees`)
    await expect(page.getByText(NIGHT_LINE)).toBeVisible()
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
    for (const preset of ['None', 'Low', 'Medium', 'Maximum']) {
      await expect(
        page.getByRole('radio', { name: new RegExp(`^${preset}: .*, 100 percent shaded$`) }),
      ).toHaveCount(1)
    }
  })
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test('the menu opens under the pill, a tap picks and closes it, with no accessibility violations', async ({
    page,
  }) => {
    await page.goto('/')
    const pill = shadePill(page, 'All shade')
    await pill.tap()
    const pillBox = (await pill.boundingBox())!
    const box = (await menu(page).boundingBox())!
    expect(box.y).toBeGreaterThan(pillBox.y + pillBox.height)
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])

    await page.getByRole('radio', { name: 'Building shade' }).tap()
    await expect(menu(page)).toBeHidden()
    await expect(shadePill(page, 'Building shade')).toBeVisible()
  })

  test('on the route screen a pick re-routes at once', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()

    await shadePill(page, 'All shade').tap()
    const sent = await nextRouteLayers(page, () => page.getByRole('radio', { name: 'Tree shade' }).tap())
    expect(sent).toBe('trees')
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
  })

  test('on the plan screen a pick waits for FIND_ROUTE, like the addresses', async ({ page }) => {
    await mockGeocode(page)
    await page.goto(routeUrl(POINT_A, POINT_B))
    await expect(routeDrawn(page)).toBeVisible()
    await page.getByRole('button', { name: /^Change trip:/ }).tap()
    // Both screens have a shade pill: wait for the plan screen, or the tap
    // can land on the route screen's as it hides (web/CLAUDE.md traps).
    await expect(page.getByRole('button', { name: 'Find route' })).toBeVisible()

    const routes: string[] = []
    page.on('request', (r) => {
      if (r.url().includes('/route?')) routes.push(r.url())
    })
    await shadePill(page, 'All shade').tap()
    await page.getByRole('radio', { name: 'Tree shade' }).tap()
    await expect(shadePill(page, 'Tree shade')).toBeVisible()
    // A "nothing happened" check needs a window to happen in.
    await page.waitForTimeout(500)
    expect(routes).toHaveLength(0)

    const sent = await nextRouteLayers(page, () => page.getByRole('button', { name: 'Find route' }).tap())
    expect(sent).toBe('trees')
  })
})
