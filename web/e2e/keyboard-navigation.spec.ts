import { expect, test } from '@playwright/test'
import { POINT_A, POINT_B, routeUrl } from './fixtures'

// axe-core inspects markup, not actual tab behavior — this checks the part
// it can't: that CLAUDE.md's accessibility promises (skip link, logical
// order, visible focus, arrow-key radio group) hold up when you actually
// drive the app with a keyboard, nothing else.

test('reaches and operates every control in order via keyboard alone', async ({ page }) => {
  await page.goto(routeUrl(POINT_A, POINT_B))
  // Wait for the route so CLEAR_ROUTE exists and is part of the tab order.
  await expect(page.getByText('dist', { exact: true })).toBeVisible()

  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Skip to route controls' })).toBeFocused()

  // Activating the skip link jumps focus straight to the controls panel,
  // past every keyboard-focusable thing inside the map (Leaflet's own
  // pan/zoom controls, the locate button).
  await page.keyboard.press('Enter')
  await expect(page.locator('#controls')).toBeFocused()

  await page.keyboard.press('Tab')
  await expect(page.getByLabel('Start_point')).toBeFocused()

  await page.keyboard.press('Tab')
  await expect(page.getByLabel('End_point')).toBeFocused()

  await page.keyboard.press('Tab')
  const findRoute = page.getByRole('button', { name: 'FIND_ROUTE' })
  await expect(findRoute).toBeFocused()

  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'USE_LOCATION' })).toBeFocused()

  await page.keyboard.press('Tab')
  const clearRoute = page.getByRole('button', { name: 'CLEAR_ROUTE' })
  await expect(clearRoute).toBeFocused()

  // Shade_priority is one native radiogroup tab stop: focus lands on
  // whichever preset is already checked (MED, from routeUrl's default
  // w=15), and arrow keys move both focus and the checked value together.
  await page.keyboard.press('Tab')
  const medRadio = page.getByRole('radio', { name: 'MED' })
  await expect(medRadio).toBeFocused()
  await expect(medRadio).toBeChecked()

  await page.keyboard.press('ArrowRight')
  const maxRadio = page.getByRole('radio', { name: 'MAX' })
  await expect(maxRadio).toBeFocused()
  await expect(maxRadio).toBeChecked()

  // Enter/Space activate buttons reached this way, same as a pointer click.
  await clearRoute.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'CLEAR_ROUTE' })).toHaveCount(0)
})
