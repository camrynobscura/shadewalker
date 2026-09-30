import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeDrawn, routeUrl } from './fixtures'

test('deliberate entries become recents once a route draws', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  const end = page.getByRole('combobox', { name: 'End point' })

  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  // The resolve has settled (its point reached the URL mirror) — but
  // with no route yet, nothing is recorded: a lone entry must not
  // surface in the other field's recents.
  await expect(page).toHaveURL(/from=/)
  expect(await page.evaluate(() => localStorage.getItem('sw-recents'))).toBeNull()

  // The picked suggestion completes the route, which is what commits
  // both entries: the typed text under the typed text, the pick under
  // the suggestion's full label.
  await end.fill('court')
  await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
  await expect(routeDrawn(page)).toBeVisible()

  await page.getByRole('button', { name: 'Clear start point' }).click()
  await start.click()
  const recents = page.getByRole('listbox', { name: 'Start point recent addresses' })
  await expect(recents).toBeVisible()
  await expect(recents.getByText('// RECENT')).toBeVisible()
  // Newest first: the end entry committed after the start entry.
  await expect(recents.getByRole('option')).toHaveText(['Court St & Baltic St, Brooklyn', '250 Court St'])

  // Picking a recent behaves exactly like picking a suggestion — it
  // carries its stored point, so the route redraws with no new geocode.
  await recents.getByRole('option', { name: '250 Court St' }).click()
  await expect(start).toHaveValue('250 Court St')
  await expect(routeDrawn(page)).toBeVisible()
})

test('recents survive a reload and are keyboard-pickable', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('combobox', { name: 'End point' }).fill('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')
  // The route drawing means both resolves finished — both recents are recorded before the reload
  // below wipes the page (not the storage).
  await expect(routeDrawn(page)).toBeVisible()

  await page.goto('/')
  await start.click()
  const recents = page.getByRole('listbox', { name: 'Start point recent addresses' })
  await expect(recents.getByRole('option')).toHaveText(['3rd St & 3rd Ave', '250 Court St'])
  // Same aria-activedescendant machinery as suggestions: arrows move the
  // highlight, Enter picks it.
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await expect(start).toHaveValue('3rd St & 3rd Ave')
})

test('an arrow pressed the instant the list appears keeps its highlight', async ({ page }) => {
  // If the highlight's reset ran as an effect, a beat after the new list
  // was on screen, an ArrowDown landing in that gap would be wiped and
  // Enter would then resolve the empty text instead of picking.
  // Machine-speed typing hits the gap only sometimes; this hits it every
  // time — a MutationObserver fires as the options
  // are inserted (the same task React commits them in, before any
  // effect) and sends the key right there. Recents are seeded directly:
  // how they get recorded is the first test's job.
  await mockGeocode(page)
  await page.goto('/')
  await page.evaluate(() => {
    const seeded = [
      { label: '3rd St & 3rd Ave', lat: 40.672, lon: -73.988 },
      { label: '250 Court St', lat: 40.68, lon: -73.998 },
    ]
    localStorage.setItem('sw-recents', JSON.stringify(seeded))
    const startInput = document.querySelector<HTMLInputElement>('input[role="combobox"]')! // Start is first
    const observer = new MutationObserver(() => {
      if (!document.querySelector('[role="listbox"] [role="option"]')) return
      observer.disconnect()
      startInput.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }),
      )
    })
    observer.observe(document.body, { childList: true, subtree: true })
  })

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.click()
  const recents = page.getByRole('listbox', { name: 'Start point recent addresses' })
  await expect(recents.getByRole('option')).toHaveText(['3rd St & 3rd Ave', '250 Court St'])
  // No event marks "every effect has run", so wait out an effect's
  // window, then prove the highlight outlived it.
  await page.waitForTimeout(300)
  await expect(recents.locator('[aria-selected="true"]')).toHaveText('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')
  await expect(start).toHaveValue('3rd St & 3rd Ave')
})

test('points arriving from outside the field record nothing', async ({ page }) => {
  // A URL-restored point drives the exact externalPoint path a map tap
  // does (see fixtures.ts on why tapping the Leaflet map itself is too
  // brittle headless) — its reverse-geocoded label must not become a
  // recent: nobody typed it, and the "nearest thing" name is noise.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByRole('combobox', { name: 'Start point' })).toHaveValue('250 Court St')
  await expect(page.getByRole('combobox', { name: 'End point' })).toHaveValue('3rd Ave')

  expect(await page.evaluate(() => localStorage.getItem('sw-recents'))).toBeNull()

  // And the empty focused field shows no dropdown at all, same as before
  // the feature existed.
  await page.getByRole('button', { name: 'Clear start point' }).click()
  await page.getByRole('combobox', { name: 'Start point' }).click()
  await expect(page.getByRole('listbox')).toHaveCount(0)
})

test('clearing during an in-flight lookup drops the late answer', async ({ page }) => {
  // A geocode answer arriving after its field was cleared must not
  // refill the point. Delaying the geocode makes the sub-second race
  // deterministic — this
  // route registers after mockGeocode, so it runs first and hands the
  // request back to the mock only after the field is long cleared.
  await mockGeocode(page)
  await page.route('**/geocode?*', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 600))
    await route.fallback()
  })
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('button', { name: 'Clear start point' }).click()

  // Give the delayed answer time to land, then prove it changed nothing:
  // no text back in the field, no point in the URL, no recent recorded.
  await page.waitForTimeout(900)
  await expect(start).toHaveValue('')
  await expect(page).not.toHaveURL(/from=/)
  expect(await page.evaluate(() => localStorage.getItem('sw-recents'))).toBeNull()
})
