import { useEffect, type ReactNode } from 'react'
import { Header } from './Header.tsx'
import shared from '../shared.module.css'

/* The About page: a second React entry so its header is the same
   component the map page renders — two hand-kept headers would drift,
   and every header redesign would have to land twice. Still an MPA: no
   router; vite.config.ts builds about.html as its own entry, the React
   vendor chunk is shared (cached from the map page). Cost accepted
   knowingly: the prose needs JS to render. RouteStats' caution link
   targets #caution here — keep that id. (Own file rather than inline in
   the about.tsx entry so the react-refresh only-export-components rule
   stays satisfied — the same split main.tsx/App.tsx use.) */
/* A link that leaves the site opens a new tab, so the page a reader came
   from stays put, and assistive tech hears the change of context. Current
   browsers imply rel=noopener on target=_blank; it is stated for older
   ones. Not noreferrer: the linked sites may see where visitors came
   from. */
function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener">
      {children}
      <span className={shared.visuallyHidden}> (opens in a new tab)</span>
    </a>
  )
}

export function AboutPage() {
  // Being a React entry breaks native fragment navigation: the browser
  // retries scrolling to location.hash only until the document's load
  // event, and #root is empty until React commits — whether the target
  // exists in time is a race the fast local build wins and a real
  // network loses (measured: delaying the JS chunk 1.5s reproduces the
  // land-at-top miss every time). Re-do the jump after render, and
  // focus the target so the keyboard/reading position moves with the
  // scroll the way a native fragment jump moves it.
  useEffect(() => {
    const id = window.location.hash.slice(1)
    if (!id) return
    const target = document.getElementById(id)
    if (!target) return
    target.scrollIntoView()
    target.tabIndex = -1
    target.focus({ preventScroll: true })
  }, [])

  return (
    <>
      <Header page="about" />

      <main className="page">
        <h1>About Shade Walker</h1>

        <section>
          <h2>Why I built this</h2>
          <p>
            I live between one of the leafiest neighborhoods in Brooklyn (Carroll Gardens, where sidewalks are
            on average about 55% shaded by trees in the summer) and one of the most tree-sparse ones (Gowanus,
            about 28% shaded), so I'm very aware of the difference it can make to go on a mostly shaded walk
            versus one with almost no shade cover during a hot summer day. I'd always thought it would be cool
            if I could have some sort of alternative map app that would give me the walking directions I
            needed, but could also tell me if there were a slightly shadier route that could still get me to
            my destination with only a small detour.
          </p>
          <p>
            That thought was where the idea for Shade Walker was born. It gives you walking directions in NYC
            to help keep you in the shade as much as possible, so you can stay cooler. The app uses the city's
            own tree data and building outlines to calculate the shadiness of different routes. You pick your
            start and end points, and your willingness to go out of your way to get a shadier route, and the
            app will serve you the best path. The routes account for the shadows buildings cast onto the
            sidewalks, hour by hour, as well as the shadows from trees.
          </p>
        </section>

        <section>
          <h2>A hotter city</h2>
          <p>
            NYC's summers have been getting hotter, and extreme heat is a genuinely dangerous part of them: by
            the city's own count,{' '}
            <ExternalLink href="https://a816-dohbesp.nyc.gov/IndicatorPublic/data-features/heat-report/">
              roughly 500 New Yorkers die prematurely from hot weather each year
            </ExternalLink>
            , and the risk falls hardest on lower-income neighborhoods, which also tend to have the fewest
            trees. We all know how brutal it is walking outside during a heatwave, but shade makes it a little
            more manageable, and that's what this app tries to help with.
          </p>
          <p>
            However, this app is not a stand-in for heat safety. To learn more about how to take care of
            yourself and loved ones in hot weather, check out some resources from the city government: NYC
            Emergency Management's{' '}
            <ExternalLink href="https://www.nyc.gov/site/em/ready/extreme-heat.page">
              Beat the Heat
            </ExternalLink>{' '}
            page, the Health Department's{' '}
            <ExternalLink href="https://www.nyc.gov/site/doh/health/emergency-preparedness/emergencies-extreme-weather-heat.page">
              Hot Weather and Your Health
            </ExternalLink>
            , and the{' '}
            <ExternalLink href="https://finder.nyc.gov/coolingcenters">cooling center finder</ExternalLink>{' '}
            during heat emergencies. If you or someone near you shows signs of heat illness, call 911.
          </p>
        </section>

        <section>
          <h2>Data</h2>
          <p>Shade Walker mainly relies on four public datasets:</p>
          <ul>
            <li>
              <strong>The map and the routing</strong> come from{' '}
              <ExternalLink href="https://www.openstreetmap.org/about">OpenStreetMap</ExternalLink>: all the
              sidewalks, crosswalks, park paths and stairways we route along, with each side of a street as
              its own separate path.
            </li>
            <li>
              <strong>The trees</strong> come from NYC Open Data's{' '}
              <ExternalLink href="https://data.cityofnewyork.us/d/hn5i-inap">
                Forestry Tree Points
              </ExternalLink>{' '}
              dataset, which is the city's record of around 900,000 individually mapped trees, updated
              regularly. For each tree we use its location to put it on our map, the trunk diameter to
              estimate the shade produced by its canopy, and species to decide if its shade survives the
              winter (deciduous vs evergreen).
            </li>
            <li>
              <strong>Park canopy</strong> comes from a 2021 aerial land-cover survey of NYC, which sees
              treetops from above. It fills in some gaps that the forestry dataset from above doesn't cover
              (like Central Park, and a few other areas) so those paths still receive shade credit.
            </li>
            <li>
              <strong>The buildings</strong> come from the city's{' '}
              <ExternalLink href="https://data.cityofnewyork.us/d/5zhs-2jue">
                Building Footprints
              </ExternalLink>{' '}
              dataset, which holds the outline and roof height of more than a million buildings in NYC. With
              the sun's position at a given month and hour and the building height, we can calculate which
              parts of a sidewalk fall under a building's shadow.
            </li>
          </ul>
          <p>
            The background map is drawn by{' '}
            <ExternalLink href="https://carto.com/attributions">CARTO</ExternalLink>, and address search is
            answered by <ExternalLink href="https://photon.komoot.io/">Photon</ExternalLink>, an open geocoder
            from komoot, with{' '}
            <ExternalLink href="https://geosearch.planninglabs.nyc/">GeoSearch</ExternalLink> from NYC
            Planning as a backup when Photon is down. We refresh our copies of the map, the trees, and the
            buildings once a month.
          </p>
        </section>

        <section>
          <h2>Route scoring</h2>
          <p>
            Every block or path in the city gets a shade score derived from how many trees are near it, how
            big they are, how much canopy the aerial survey sees overhead (for parks not covered by the
            original tree dataset) and, by the hour, the shadows of buildings. Given a specific time, the app
            calculates where the sun is and which sidewalks fall under shade from nearby buildings, and it
            also takes into account that certain trees lose their leaves in the fall/winter. After dark, the
            whole city is in shade, so every shade priority setting in the app will just give you the fastest
            route. There's also a selector that lets you choose to get a shade score from different layers, so
            you can include just tree shade (for the leafiest walk) or just building shade, instead of the
            default that includes both.
          </p>
          <p>
            When you ask for a route, the app weighs your walking time and the shade along your potential
            routes. The Shade Priority setting is the exchange rate between them. 'None' ignores shade
            entirely and gives you the plain fastest walk, 'Low' takes a shadier street only when it costs
            nearly nothing, 'Medium' accepts short detours, and 'Maximum' will take the longest detour to keep
            you shaded. Each row shows the route's minutes, distance, and shade, so you can compare them at a
            glance.
          </p>
        </section>

        <section>
          <h2>Gaps</h2>
          <ul>
            <li>
              <strong>Deep park woods.</strong> The city's tree record covers individually cared-for trees. In
              dense woodland, like the middle of Pelham Bay Park's forest, there are no individual records,
              and the aerial survey fills it less precisely because we don't get the same amount of data from
              it (like species, diameter, condition) and it's also slightly outdated.
            </li>
            <li>
              <strong>The park canopy is a 2021 snapshot.</strong> The gaps in the forestry data are filled in
              by a 2021 aerial survey, which is now slightly outdated, so a park tree that's been planted or
              lost since then still holds the 2021 data.
            </li>
            <li>
              <strong>The "trees along the way" number.</strong> This number under a route counts only
              individually mapped trees, not park canopy, so a route shaded mostly by canopy can show a small
              number and still be shady.
            </li>
            <li>
              <strong>Some shadows are missing.</strong> The building data only includes outlines and roof
              heights, so every building is treated as a plain block on flat ground. The building shadows are
              also just from buildings, not other structures that also cast shadow, like elevated tracks,
              bridges, scaffolding and sidewalk sheds (we don't have that data).
            </li>
            <li>
              <strong>Weather isn't in the data.</strong> The shade percentages assume a sunny day. Clouds,
              haze and the sun going in and out are not something the app knows about.
            </li>
            <li>
              <strong>Some places can't be routed</strong>, and that's due to data limitations from our
              reliance on OpenStreetMap. We rely on OSM for our information about NYC's sidewalks, paths and
              crossings, and because OSM is built by volunteers, its sidewalk coverage is uneven. The thinnest
              coverage areas are in the Bronx, Staten Island and the outer edges of the city. The good news is
              that anyone can fix this.{' '}
              <ExternalLink href="https://www.openstreetmap.org/fixthemap">
                Map a missing sidewalk on OpenStreetMap
              </ExternalLink>{' '}
              and it will show up in Shade Walker within a month or two, after we do our next monthly map
              refresh.
            </li>
          </ul>
        </section>

        <section id="caution">
          <h2>Trust your eyes</h2>
          <p>
            Shade Walker is built from data, and data is not always accurately reflected in real life. NYC is
            an ever-changing creature, and our data might not take into account scaffolding that's gone up,
            sidewalks that are closed for construction, or a storm taking down a tree. If a route tells you
            one thing and the street in front of you says another, believe the street.
          </p>
          <p>
            Treat the routes as suggestions, not instructions: stay aware of your surroundings, cross where
            it's safe rather than exactly where the line crosses, and use your own judgment about where to
            walk. The app knows where the trees and the buildings are, but you know everything else.
          </p>
        </section>

        <section>
          <h2>Credits</h2>
          <p>
            Map data ©{' '}
            <ExternalLink href="https://www.openstreetmap.org/copyright">
              OpenStreetMap contributors
            </ExternalLink>
            , available under the Open Database License. Tree data and building footprints from{' '}
            <ExternalLink href="https://opendata.cityofnewyork.us/">NYC Open Data</ExternalLink> (NYC Parks
            Forestry and the Office of Technology and Innovation). Park and path canopy from{' '}
            <ExternalLink href="https://doi.org/10.5281/zenodo.14053441">
              New York City Land Cover (2021)
            </ExternalLink>{' '}
            © The Nature Conservancy, developed under contract by the University of Vermont Spatial Analysis
            Laboratory, used under{' '}
            <ExternalLink href="https://creativecommons.org/licenses/by-nc-sa/4.0/">
              CC BY-NC-SA 4.0
            </ExternalLink>
            : we sample it along each path to score its shade, and it is provided as-is, without warranty.
            Basemap tiles by <ExternalLink href="https://carto.com/attributions">CARTO</ExternalLink>.
            Geocoding by <ExternalLink href="https://photon.komoot.io/">Photon</ExternalLink>, from komoot,
            with <ExternalLink href="https://geosearch.planninglabs.nyc/">GeoSearch</ExternalLink> from NYC
            Planning as the backup. Shade Walker is open source, and you can{' '}
            <ExternalLink href="https://github.com/camrynobscura/shadewalker">
              read the code on GitHub
            </ExternalLink>
            .
          </p>
        </section>
      </main>

      <footer className="footer">
        <p>
          <a className={shared.pageLink} href="/">
            Back to the map <span aria-hidden="true">→</span>
          </a>
        </p>
      </footer>
    </>
  )
}
