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
  await page.getByRole('button', { name: 'FIND ROUTE' }).click()
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

test('out-of-coverage rejection has no violations', async ({ page }) => {
  // Same reason as the "a drawn route" test above -- start/end from
  // routeUrl() now reverse-geocodes on load regardless of coverage.
  await mockGeocode(page)
  await page.goto(routeUrl(POINT_A, POINT_OUTSIDE_COVERAGE))
  // Plain getByText('coverage') is ambiguous once the map's own coverage-
  // area legend entry is loaded (same word, unrelated element) -- role="alert"
  // is unique to RouteStats' error message, so it's both the more specific
  // locator and the one that actually proves the right state loaded.
  await expect(page.getByRole('alert')).toContainText('coverage', { ignoreCase: true })

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations).toEqual([])
})
