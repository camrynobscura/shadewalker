import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Locator } from '@playwright/test'
import {
  atNight,
  mockGeocode,
  POINT_A,
  POINT_B,
  POINT_OUTSIDE_COVERAGE,
  routeDrawn,
  routeUrl,
} from './fixtures'

// axe-core catches markup-detectable WCAG issues (missing labels, contrast,
// ARIA misuse) across the app's real, distinct states — not a replacement
// for manual VoiceOver/Lighthouse passes, but a regression net between
// them.

test('initial load has no violations', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Tap the map', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('a drawn route has no violations', async ({ page }) => {
  // routeUrl() lands with start/end already set, which reverse-geocodes
  // into the address fields on load -- mock it or this hits the live
  // geocoder.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(routeDrawn(page)).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('a route after dark has no violations', async ({ page }) => {
  // One line replaces the Shade_priority box's two after dark (#112) --
  // a state no other scan here reaches.
  await mockGeocode(page)
  await atNight(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByText('after dark // the whole city is in shade')).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('the open time menu, and a set departure on the time pill, have no violations', async ({ page }) => {
  // Both only exist after a click: the menu (the radios, native
  // date/time inputs, the pill's aria-expanded) and the pill's
  // "Depart ..." text once a time is set. (The phone's menu has its own
  // scan in time-picker.spec.)
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(routeDrawn(page)).toBeVisible()
  await page.getByRole('button', { name: 'Start time: Leave now' }).click()
  await page.getByRole('radio', { name: 'Depart at' }).check()
  await expect(page.getByLabel('date', { exact: true })).toBeVisible()
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])

  await page.getByLabel('time (NYC)').fill('09:05')
  await expect(page.getByRole('button', { name: /^Start time: Depart .*9:05/ })).toBeVisible()
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([])
})

test('an open autocomplete listbox has no violations', async ({ page }) => {
  // The combobox wiring (aria-expanded/-controls/-activedescendant,
  // option roles) only exists in the DOM while the dropdown is open --
  // none of the other states here ever render it, so without this state
  // axe never sees the pattern at all.
  await mockGeocode(page)
  await page.goto('/')
  const start = page.getByLabel('Start point')
  await start.click()
  await start.pressSequentially('court', { delay: 30 })
  await expect(page.getByRole('listbox', { name: 'Start point suggestions' })).toBeVisible()
  await page.keyboard.press('ArrowDown') // active option: aria-activedescendant set

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('an open recents listbox has no violations', async ({ page }) => {
  // Distinct from the autocomplete state above: the recents list carries
  // a non-option header row (role="presentation" + aria-hidden), which is
  // exactly the shape axe's listbox required-children rule exists to
  // police — prove the exemption actually holds.
  await mockGeocode(page)
  await page.goto('/')
  const start = page.getByRole('combobox', { name: 'Start point' })
  await start.fill('250 Court St')
  await page.keyboard.press('Enter')
  await page.getByRole('combobox', { name: 'End point' }).fill('3rd St & 3rd Ave')
  await page.keyboard.press('Enter')
  // Recents only exist once a route draws — commit rides route arrival.
  await expect(routeDrawn(page)).toBeVisible()
  await page.getByRole('button', { name: 'Clear start point' }).click()
  await start.click()
  await expect(page.getByRole('listbox', { name: 'Start point recent addresses' })).toBeVisible()
  await page.keyboard.press('ArrowDown') // active option: aria-activedescendant set

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('the About page has no violations and links back', async ({ page }) => {
  // A second page served from the same build -- easy for regressions to
  // hide on since no component test ever renders it.
  await page.goto('/about.html')
  await expect(page.getByRole('heading', { name: 'About Shade Walker' })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])

  await page.getByRole('link', { name: 'back to the map' }).first().click()
  await expect(page).toHaveURL(/\/$|\/\?/)
})

test('address search with no match has no violations', async ({ page }) => {
  await mockGeocode(page)
  await page.goto('/')
  await page.getByLabel('Start point').fill('Nowhere, USA')
  // Enter resolves the typed text (desktop has no FIND_ROUTE button).
  await page.keyboard.press('Enter')
  await expect(page.getByText('NOT_FOUND', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

// axe-core cannot evaluate contrast for text it can't isolate a solid
// background behind (it files the node as "incomplete"/bgOverlap and
// moves on) -- and every check above only asserts `results.violations`,
// which never includes that bucket. The header tagline can slip through
// exactly this gap: a color/opacity change that drops it to 2.61:1
// against its actual background leaves every a11y check here green.
// Compute the real rendered contrast for the two elements that would
// regress that way, instead of trusting axe to catch it for them.
test('header tagline and instructions meet AA text contrast', async ({ page }) => {
  await page.goto('/')

  async function renderedContrast(locator: Locator): Promise<number> {
    const [r, g, b, opacity, bgR, bgG, bgB] = await locator.evaluate((el) => {
      const style = getComputedStyle(el)
      const [r, g, b] = style.color.match(/[\d.]+/g)!.map(Number)

      // Walk up to whatever ancestor actually paints a background --
      // the tagline/instructions elements themselves are transparent.
      let node: Element | null = el
      let bg = 'rgb(255, 255, 255)'
      while (node) {
        const candidate = getComputedStyle(node).backgroundColor
        if (candidate !== 'rgba(0, 0, 0, 0)' && candidate !== 'transparent') {
          bg = candidate
          break
        }
        node = node.parentElement
      }
      const [bgR, bgG, bgB] = bg.match(/[\d.]+/g)!.map(Number)
      return [r, g, b, Number(style.opacity), bgR, bgG, bgB]
    })

    // Composite the element's own `opacity` onto that background -- this
    // is exactly the mechanism that hides a failure (magenta @ 80%
    // opacity reads fine in isolation but fails once blended).
    const composite = (fg: number, bgChannel: number) => opacity * fg + (1 - opacity) * bgChannel
    const fg: [number, number, number] = [composite(r, bgR), composite(g, bgG), composite(b, bgB)]
    const bg: [number, number, number] = [bgR, bgG, bgB]

    const relativeLuminance = ([cr, cg, cb]: [number, number, number]) => {
      const linear = (c: number) => {
        const s = c / 255
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
      }
      return 0.2126 * linear(cr) + 0.7152 * linear(cg) + 0.0722 * linear(cb)
    }
    const [l1, l2] = [relativeLuminance(fg), relativeLuminance(bg)].sort((a, b) => b - a)
    return (l1 + 0.05) / (l2 + 0.05)
  }

  // exact:true matches only the innermost element whose own full text
  // equals the string -- getByText substring matching would otherwise
  // also match the parent <h1>, which contains the tagline's text too.
  const tagline = page.getByText('> find the shadiest walking route in NYC', { exact: true })
  const instructions = page.getByText(
    '> tap the map to set a start and end point, or search two addresses below',
    { exact: true },
  )

  expect(await renderedContrast(tagline)).toBeGreaterThanOrEqual(4.5)
  expect(await renderedContrast(instructions)).toBeGreaterThanOrEqual(4.5)
})

// Windows High Contrast (forced-colors) strips every author background,
// which would leave the checked Shade_priority row marked by font weight
// alone. Chromium is the only engine that emulates the media feature,
// and it's the one project here.
test.describe('forced colors', () => {
  test.use({ forcedColors: 'active' })
  test('the checked preset keeps a fill distinct from the canvas', async ({ page }) => {
    await page.goto('/')
    // A fresh load checks the default, MAX.
    const maximum = page.getByRole('radio', { name: 'Maximum' })
    await expect(maximum).toBeChecked()
    // The row's visible face is the radio's next sibling (the radio
    // itself is invisible, stretched over the row).
    const checkedBg = await maximum.evaluate((el) => getComputedStyle(el.nextElementSibling!).backgroundColor)
    const canvasBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(checkedBg).not.toBe(canvasBg)
  })
})

test.describe('reduced motion', () => {
  test.use({ reducedMotion: 'reduce' })
  test('the pending state is visible: a plain line stands in for the vine', async ({ page }) => {
    await mockGeocode(page)
    // Never fulfilled: the pending state stays up for the whole check.
    await page.route('**/route?*', () => {})
    await page.goto(routeUrl(POINT_A, POINT_B))
    // base.css cancels every animation under reduced motion, so a block
    // that starts at opacity 0 and relies on one to appear shows these
    // users nothing for the whole wait. The vine (a still squiggle, once
    // frozen) is swapped for text; the spoken sentence is unchanged.
    await expect(page.getByText('> finding your route…')).toBeVisible()
    await expect(page.locator('[class*="vineStage"]')).toBeHidden()
    await expect(page.getByText('Finding your route…', { exact: true })).toBeAttached()
  })
})

test('out-of-coverage rejection has no violations', async ({ page }) => {
  // Same reason as the "a drawn route" test above -- start/end from
  // routeUrl() reverse-geocodes on load regardless of coverage.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
  // role="alert" is unique to the panel's error message, so it's both a
  // specific locator and the one that proves the right state loaded.
  await expect(page.getByRole('alert')).toContainText('coverage', { ignoreCase: true })

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})
