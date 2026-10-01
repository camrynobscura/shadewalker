import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/** A geocoder the test answers by hand: each lookup waits until
 * `answer(text, labels)` is called for its text, so the order answers
 * land in is the test's to choose — no real waiting, no timing. */
async function heldGeocoder(page: Page) {
  const waiting = new Map<string, (labels: string[]) => void>()
  const arrived = new Map<string, () => void>()
  await page.route('**/geocode?*', async (route) => {
    const text = new URL(route.request().url()).searchParams.get('q') ?? ''
    const labels = await new Promise<string[]>((resolve) => {
      waiting.set(text, resolve)
      arrived.get(text)?.()
    })
    const results = labels.map((label) => ({ lat: 40.68, lon: -73.998, label }))
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results }) })
  })
  return {
    /** Resolves once the app has asked for `text`. */
    asked: (text: string) =>
      new Promise<void>((resolve) => (waiting.has(text) ? resolve() : arrived.set(text, resolve))),
    answer: (text: string, labels: string[]) => waiting.get(text)!(labels),
  }
}

test('suggestions arrive while typing goes on, with a searching line until the list catches up', async ({
  page,
}) => {
  const geocoder = await heldGeocoder(page)
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  const searching = page.getByText('// SEARCHING…', { exact: true })
  const spoken = page.getByRole('status').filter({ hasText: 'Searching…' })

  // The first lookup is out and unanswered: the wait is shown and spoken.
  await start.fill('cour')
  await geocoder.asked('cour')
  await expect(searching).toBeVisible()
  await expect(spoken).toHaveCount(1)
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])

  // More typing sends a second lookup; the first is not cancelled, so
  // its answer shows — and the list is still catching up.
  await start.pressSequentially('t')
  await geocoder.asked('court')
  geocoder.answer('cour', ['Courtlandt Ave, Bronx'])
  const list = page.getByRole('listbox', { name: 'Start point suggestions' })
  await expect(list.getByRole('option')).toHaveText(['Courtlandt Ave, Bronx'])
  await expect(searching).toBeVisible()
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])

  // The answer for the text as it stands replaces it, and the wait ends.
  geocoder.answer('court', ['Court St, Brooklyn', 'Court Sq, Queens'])
  await expect(list.getByRole('option')).toHaveText(['Court St, Brooklyn', 'Court Sq, Queens'])
  await expect(searching).toBeHidden()
  await expect(spoken).toHaveCount(0)
})
