import { expect, test } from '@playwright/test'
import { POINT_A, POINT_B, routeUrl } from './fixtures'

/** Route colour is applied by CSS class (MapView.module.css on the theme
 * tokens), and the class only reaches Leaflet's SVG element if it is a
 * top-level prop on the react-leaflet component -- inside pathOptions it
 * is applied by setStyle() after the element exists and silently does
 * nothing. React's StrictMode double mount hides that in dev, and this
 * suite runs the production build, which is exactly where it shows
 * (routes render in Leaflet's default blue). So the assertion is on the
 * computed stroke, not on the class name. */
test('the selected and baseline routes draw in their token colours, not Leaflet blue', async ({ page }) => {
  await page.goto(routeUrl(POINT_A, POINT_B, 15))
  const paths = page.locator('path.leaflet-interactive')
  await expect(paths).toHaveCount(4) // baseline casing + line, selected casing + line

  const selected = page.locator('path.leaflet-interactive[class*="routeSelected"]')
  const baseline = page.locator('path.leaflet-interactive[class*="routeBaseline"]')
  await expect(selected).toHaveCount(2)
  await expect(baseline).toHaveCount(2)
  for (const path of await selected.all()) await expect(path).toHaveCSS('stroke', 'rgb(0, 168, 107)') // --green
  for (const path of await baseline.all()) await expect(path).toHaveCSS('stroke', 'rgb(255, 43, 214)') // --magenta
})
