import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import { mockGeocode } from './fixtures'

// The mobile full-screen map mode: a toggle in the map's top-right
// corner hides the header and the panel so the map takes the whole
// screen (the stacked 45vh map feels cramped exactly when a route exists
// and the panel matters least). It's a layout mode, not
// the Fullscreen API — iPhone Safari has no element fullscreen. These
// specs pin the mode's lifecycle: what disappears, what comes back, and
// that hidden-not-unmounted panel state survives a round trip.

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

test('expanding hides header and panel, fills the screen; collapsing restores both', async ({ page }) => {
  await page.goto('/')

  const toggle = page.getByRole('button', { name: 'Expand map' })
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  await toggle.tap()

  await expect(toggle).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('banner')).toBeHidden()
  await expect(page.getByRole('region', { name: 'Route controls and details' })).toBeHidden()
  // The point of the mode: the map region owns the whole viewport.
  const box = await page.getByRole('region', { name: 'Map' }).boundingBox()
  expect(box).not.toBeNull()
  expect(box!.height).toBeGreaterThan(830)

  await toggle.tap()
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByRole('banner')).toBeVisible()
  await expect(page.getByRole('region', { name: 'Route controls and details' })).toBeVisible()
})

test('Escape collapses the expanded map', async ({ page }) => {
  await page.goto('/')

  await page.getByRole('button', { name: 'Expand map' }).tap()
  await expect(page.getByRole('banner')).toBeHidden()

  await page.keyboard.press('Escape')
  await expect(page.getByRole('banner')).toBeVisible()
})

test('panel state survives an expand/collapse round trip', async ({ page }) => {
  // The panel is display:none while expanded, not unmounted — typed-but-
  // unresolved field text must still be there when it comes back.
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.tap()
  await start.pressSequentially('court', { delay: 30 })
  await page.getByRole('button', { name: 'CANCEL' }).tap()
  await expect(start).toHaveValue('court')

  const toggle = page.getByRole('button', { name: 'Expand map' })
  await toggle.tap()
  await expect(start).toBeHidden()
  await toggle.tap()
  await expect(start).toHaveValue('court')
})

test('the expanded map has no axe violations', async ({ page }) => {
  // A state none of the other scans see: no header, no panel, the map's
  // floating controls as the only interactive surface.
  await page.goto('/')
  await page.getByRole('button', { name: 'Expand map' }).tap()
  await expect(page.getByRole('banner')).toBeHidden()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test.describe('desktop has no expand toggle', () => {
  test.use({ viewport: { width: 1280, height: 800 }, hasTouch: false })

  test('the toggle never renders on the desktop layout', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('region', { name: 'Map' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Expand map' })).toHaveCount(0)
  })
})
