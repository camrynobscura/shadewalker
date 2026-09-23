import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Locator } from '@playwright/test'
import { mockGeocode, POINT_A, POINT_B, POINT_OUTSIDE_COVERAGE, routeUrl } from './fixtures'

// axe-core catches markup-detectable WCAG issues (missing labels, contrast,
// ARIA misuse) across the app's real, distinct states — not a replacement
// for the manual VoiceOver/Lighthouse passes CLAUDE.md already calls for,
// but a regression net between them.

test('initial load has no violations', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Tap the map', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('a drawn route has no violations', async ({ page }) => {
  // routeUrl() lands with start/end already set, which now reverse-geocodes
  // into the address fields on load -- mock it or this hits live Nominatim.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_B))
  await expect(page.getByText('distance', { exact: true })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
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
  await expect(page.getByText('distance', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Clear start point' }).click()
  await start.click()
  await expect(page.getByRole('listbox', { name: 'Start point recent addresses' })).toBeVisible()
  await page.keyboard.press('ArrowDown') // active option: aria-activedescendant set

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

test('the About page has no violations and links back', async ({ page }) => {
  // A static second page (no React) served from the same build -- easy
  // for regressions to hide on since no component test ever renders it.
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
  // Enter resolves the typed text — the FIND_ROUTE button is gone (2026-09-02).
  await page.keyboard.press('Enter')
  await expect(page.getByText('NOT_FOUND', { exact: false })).toBeVisible()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})

// axe-core cannot evaluate contrast for text it can't isolate a solid
// background behind (it files the node as "incomplete"/bgOverlap and
// moves on) -- and every check above only asserts `results.violations`,
// which never includes that bucket. The header tagline shipped through
// exactly this gap on 2026-07-15: a color/opacity change dropped it to
// 2.61:1 against its actual background while every a11y check here
// stayed green. Compute the real rendered contrast for the two elements
// that regressed that way, instead of trusting axe to catch it for them.
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
    // is exactly the mechanism the original bug used (magenta @ 80%
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
// which used to leave the checked Shade_priority segment marked by font
// weight alone (audit 2026-09-09). Chromium is the only engine that
// emulates the media feature, and it's the one project here.
test.describe('forced colors', () => {
  test.use({ forcedColors: 'active' })
  test('the checked preset keeps a fill distinct from the canvas', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('radio', { name: 'Medium' })).toBeChecked()
    const [checkedBg, canvasBg] = await page.evaluate(() => [
      getComputedStyle(document.querySelector('input[type=radio]:checked + span')!).backgroundColor,
      getComputedStyle(document.body).backgroundColor,
    ])
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
    // The regression this pins (2026-09-23): base.css cancels every
    // animation under reduced motion, and the block used to START at
    // opacity 0 and rely on one to appear — so these users saw nothing
    // for the whole wait. The vine (a still squiggle, once frozen) is
    // swapped for text; the spoken sentence is unchanged.
    await expect(page.getByText('> finding your route…')).toBeVisible()
    await expect(page.locator('[class*="vineStage"]')).toBeHidden()
    await expect(page.getByText('Finding your route…', { exact: true })).toBeAttached()
  })
})

test('out-of-coverage rejection has no violations', async ({ page }) => {
  // Same reason as the "a drawn route" test above -- start/end from
  // routeUrl() now reverse-geocodes on load regardless of coverage.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
  // role="alert" is unique to RouteStats' error message, so it's both a
  // specific locator and the one that proves the right state loaded.
  // (It also outlived the legend's "coverage area" row, which used to
  // make plain getByText('coverage') ambiguous — row deleted 2026-09-03.)
  await expect(page.getByRole('alert')).toContainText('coverage', { ignoreCase: true })

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})
