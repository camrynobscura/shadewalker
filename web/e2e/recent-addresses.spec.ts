import { expect, test } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, routeUrl } from './fixtures'

test('deliberate entries become recents once a route draws', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')

  const start = page.getByRole('combobox', { name: 'Start point' })
  const end = page.getByRole('combobox', { name: 'End point' })

  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  // The resolve has settled (its point reached the URL mirror) — but
  // with no route yet, nothing is recorded: a lone entry must not
  // surface in the other field's recents (user call 2026-09-03).
  await expect(page).toHaveURL(/from=/)
  expect(await page.evaluate(() => localStorage.getItem('sw-recents'))).toBeNull()

  // The picked suggestion completes the route, which is what commits
  // both entries: the typed text under the TYPED text, the pick under
  // the suggestion's full label.
  await end.fill('court')
  await page.getByRole('option', { name: 'Court St & Baltic St, Brooklyn' }).click()
  await expect(page.getByText('distance', { exact: true })).toBeVisible()

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
  await expect(page.getByText('distance', { exact: true })).toBeVisible()
})

test('recents survive a reload and are keyboard-pickable', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('combobox', { name: 'End point' }).fill('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')
  // The route drawing means both resolves finished — both recents are
  // recorded before the reload below wipes the page (not the storage).
  await expect(page.getByText('distance', { exact: true })).toBeVisible()

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

test('points arriving from outside the field record nothing', async ({ page }) => {
  // A URL-restored point drives the exact externalPoint path a map tap
  // does (see fixtures.ts on why tapping the Leaflet map itself is too
  // brittle headless) — its reverse-geocoded label must NOT become a
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
