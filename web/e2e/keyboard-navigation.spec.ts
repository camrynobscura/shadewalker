import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

// axe-core inspects markup, not actual tab behavior — this checks the part
// it can't: that CLAUDE.md's accessibility promises (skip link, logical
// order, visible focus, arrow-key radio group) hold up when you actually
// drive the app with a keyboard, nothing else.

test('reaches and operates every control in order via keyboard alone', async ({ page }) => {
  // routeUrl() lands with start/end already set, which now reverse-geocodes
  // into the address fields on load -- mock it or this hits live Nominatim.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  // Wait for the route so both fields hold text and their ✕ accessories
  // (which replaced CLEAR_ROUTE, 2026-09-02) are part of the tab order.
  await expect(routeDrawn(page)).toBeVisible()

  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Skip to route controls' })).toBeFocused()

  // Activating the skip link jumps focus straight to the controls panel,
  // past every keyboard-focusable thing inside the map (Leaflet's own
  // pan/zoom controls, the locate button).
  await page.keyboard.press('Enter')
  await expect(page.locator('#controls')).toBeFocused()

  // Each field is followed by its in-field accessory: the ✕ while it
  // holds text (as both do here), the start field's ⌖ once cleared.
  await page.keyboard.press('Tab')
  await expect(page.getByRole('combobox', { name: 'Start point' })).toBeFocused()

  await page.keyboard.press('Tab')
  const clearStart = page.getByRole('button', { name: 'Clear start point' })
  await expect(clearStart).toBeFocused()

  await page.keyboard.press('Tab')
  await expect(page.getByRole('combobox', { name: 'End point' })).toBeFocused()

  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Clear end point' })).toBeFocused()

  // Then the time pill under the addresses: when, right after where.
  // (No FIND_ROUTE on desktop: it routes by itself.)
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Start time: Leave now' })).toBeFocused()

  // And the shade pill beside it: which shade, right after when.
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Shade from: All shade' })).toBeFocused()

  // Shade_priority is one native radiogroup tab stop: focus lands on
  // whichever row is already checked (MED, from routeUrl's default w=15),
  // and arrow keys move both focus and the checked value together. The
  // rows run shadiest first (MAX, MED, LOW, NONE), so down from MED is LOW.
  await page.keyboard.press('Tab')
  const medRadio = page.getByRole('radio', { name: 'MEDIUM' })
  await expect(medRadio).toBeFocused()
  await expect(medRadio).toBeChecked()

  await page.keyboard.press('ArrowDown')
  const lowRadio = page.getByRole('radio', { name: 'LOW' })
  await expect(lowRadio).toBeFocused()
  await expect(lowRadio).toBeChecked()

  // Enter/Space activate buttons reached this way, same as a pointer
  // click: clearing the start field drops its text, point, and the route
  // with them, and the emptied field's accessory swaps to the ⌖.
  await clearStart.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('combobox', { name: 'Start point' })).toHaveValue('')
  // The ✕ unmounts as it clears; a keyboard activation must land focus on
  // the emptied input, not on <body> (audit 2026-09-09).
  await expect(page.getByRole('combobox', { name: 'Start point' })).toBeFocused()
  await expect(routeDrawn(page)).toBeHidden()
  await expect(page.getByRole('button', { name: 'Use location' })).toBeVisible()
})

// The autocomplete combobox never moves DOM focus into its listbox --
// arrows drive aria-activedescendant while the caret stays in the input
// (the ARIA combobox pattern). axe can check the attributes exist; only
// actually pressing the keys proves they do anything.
test('address autocomplete is fully keyboard operable', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  // getByRole('combobox'), not getByLabel: while the dropdown is open,
  // the listbox's own accessible name ("Start_point suggestions")
  // substring-matches the label text too, and strict mode rightly
  // refuses the ambiguity. The role pins the input.
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.click()
  // pressSequentially, not fill: suggestions react to typing, and fill()
  // collapses it into one synthetic event -- fine for the smoke test,
  // wrong for a test ABOUT typing.
  await start.pressSequentially('court', { delay: 30 })

  const listbox = page.getByRole('listbox', { name: 'Start point suggestions' })
  await expect(listbox).toBeVisible()
  const options = listbox.getByRole('option')
  await expect(options).toHaveCount(2) // '250 Court St' + 'Court St & Baltic St'

  // Nothing highlighted until the keyboard asks -- Enter here would
  // geocode the typed text, not grab an option the user never chose.
  await expect(start).not.toHaveAttribute('aria-activedescendant', /.+/)

  await page.keyboard.press('ArrowDown')
  await expect(options.nth(0)).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('ArrowDown')
  await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('ArrowUp')
  await expect(options.nth(0)).toHaveAttribute('aria-selected', 'true')
  await expect(start).toHaveAttribute('aria-activedescendant', /.+/)

  // Enter picks the highlighted option: label lands in the field and the
  // list closes — no second geocode round trip for text a pick resolved.
  await page.keyboard.press('Enter')
  await expect(listbox).toBeHidden()
  await expect(start).toHaveValue('250 Court St, Brooklyn')

  // Escape dismisses without touching the text; ArrowDown reopens.
  const end = page.getByRole('combobox', { name: 'End point' })
  await end.click()
  await end.pressSequentially('baltic', { delay: 30 })
  const endListbox = page.getByRole('listbox', { name: 'End point suggestions' })
  await expect(endListbox).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(endListbox).toBeHidden()
  await expect(end).toHaveValue('baltic')
  await page.keyboard.press('ArrowDown')
  await expect(endListbox).toBeVisible()
})
