import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import { mockGeocode } from './fixtures'

// The full-screen search mode (2026-09-02): on the mobile layout a focused
// address field expands to a fixed full-screen layer with the input at the
// TOP of the screen. That geometry IS the iOS keyboard fix — Safari
// scrolled the window to lift a mid-screen input above the keyboard,
// exposing bare canvas below the one-screen-tall app (the "green box");
// a top-of-screen input gives it nothing to scroll for. Playwright can't
// raise a real iOS keyboard, so these specs pin the geometry and the
// expand/collapse lifecycle — the parts a desktop regression would break
// silently.

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

test('focusing a field expands full-screen search; picking a suggestion collapses it', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  // tap(), not click(): the expansion arms on touchstart, before focus —
  // the order real phones deliver.
  await start.tap()

  // CANCEL only exists while expanded — it's the expansion's marker.
  await expect(page.getByRole('button', { name: 'CANCEL' })).toBeVisible()
  // The load-bearing geometry: the input sits at the top of the screen.
  const box = await start.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.y).toBeLessThan(120)

  await start.pressSequentially('court', { delay: 30 })
  const listbox = page.getByRole('listbox', { name: 'Start point suggestions' })
  await expect(listbox).toBeVisible()

  await listbox.getByRole('option').first().tap()
  // Picking an address ends the search session: overlay gone, field filled.
  await expect(page.getByRole('button', { name: 'CANCEL' })).toBeHidden()
  await expect(start).toHaveValue(/Brooklyn/)
})

test('CANCEL collapses the overlay and keeps the typed text', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.tap()
  await start.pressSequentially('court', { delay: 30 })
  await page.getByRole('button', { name: 'CANCEL' }).tap()

  await expect(page.getByRole('button', { name: 'CANCEL' })).toBeHidden()
  // CANCEL abandons the search, not the text — blur only clears a field
  // that was emptied (useAddressField.onBlur's existing contract).
  await expect(start).toHaveValue('court')
  // Focus parks on the field's own box (the nearest script-focusable
  // ancestor), not <body>: the next Tab / VoiceOver swipe continues from
  // the field just edited (2026-09-23). Never the input — that would
  // reopen the keyboard and the overlay with it.
  await expect(start.locator('xpath=ancestor::*[@tabindex="-1"][1]')).toBeFocused()
  await expect(start).not.toBeFocused()
})

test('Escape collapses the overlay the same way, keeping text and parking focus on the field', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.tap()
  await start.pressSequentially('court', { delay: 30 })
  // First Escape closes the suggestion list, second the overlay.
  await page.keyboard.press('Escape')
  await page.keyboard.press('Escape')

  await expect(page.getByRole('button', { name: 'CANCEL' })).toBeHidden()
  await expect(start).toHaveValue('court')
  await expect(start.locator('xpath=ancestor::*[@tabindex="-1"][1]')).toBeFocused()
})

test('the expanded overlay has no axe violations', async ({ page }) => {
  // The overlay reuses the combobox DOM, but fixed-fullscreen positioning
  // changes stacking and contrast contexts — scan the state axe never
  // sees in the desktop specs.
  await mockGeocode(page)
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.tap()
  await start.pressSequentially('court', { delay: 30 })
  await expect(page.getByRole('listbox', { name: 'Start point suggestions' })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test.describe('desktop stays inline', () => {
  test.use({ viewport: { width: 1280, height: 800 }, hasTouch: false })

  test('focusing a field never expands on the desktop layout', async ({ page }) => {
    await mockGeocode(page)
    await page.goto('/')

    await page.getByRole('combobox', { name: 'Start point' }).click()
    await expect(page.getByRole('button', { name: 'CANCEL' })).toHaveCount(0)
  })
})
