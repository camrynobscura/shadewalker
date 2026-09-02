import { Header } from './Header.tsx'

/* The About page: a second React entry (decided 2026-09-01, reversing
   2026-08-30's plain-HTML call) so its header is the SAME component the
   map page renders — the two hand-kept headers had already drifted, and
   every header redesign would have had to land twice. Still an MPA: no
   router; vite.config.ts builds about.html as its own entry, the React
   vendor chunk is shared (cached from the map page). Cost accepted
   knowingly: the prose now needs JS to render; it was static HTML.
   RouteStats' caution link targets #caution here — keep that id.
   (Own file rather than inline in the about.tsx entry so the
   react-refresh only-export-components rule stays satisfied — the
   same split main.tsx/App.tsx use.) */
export function AboutPage() {
  return (
    <>
      <Header page="about" />

      <main className="page">
        <h1>About Shade Walker</h1>

        <section>
          <h2>Why I built this</h2>
          <p>
            I live in between one of the leafiest neighborhoods in Brooklyn (Carroll Gardens, 8.2 trees per acre) and one of the
            most tree sparse ones (Gowanus, 4.1 trees per acre) so I'm very aware of the difference it can make to go on a
            shady walk vs an unshady one during the heights of our increasingly brutal summers. I'd always thought it would
            be cool if I could have some sort of alternative map app that would give me directions I needed, but could tell
            me if there were a slightly shadier way for me to get to my destination with just a slightly longer walk.
          </p>
          <p>
            So that is where the idea for Shade Walker was born. It gives you directions in NYC that help keep you underneath
            some trees so you can stay cooler, using the city's own tree data to calculate the shadiness of different
            routes. You pick your start and end points, and your willingness to go out of your way to get a shadier route,
            and the app will serve you the best path. I'm planning on adding shade data from the shade cast by buildings
            onto the sidewalks as the next step of this app, so stay tuned for that.
          </p>
        </section>

        <section>
          <h2>A hotter city</h2>
          <p>
            NYC's summers have been getting hotter, and extreme heat is a
            genuinely dangerous part of them: by the city's own count, roughly
            500 New Yorkers die prematurely from hot weather each year, and
            that risk falls hardest on the neighborhoods with the least
            cooling, which are often the ones with the fewest trees
            (<a href="https://a816-dohbesp.nyc.gov/IndicatorPublic/data-features/heat-report/">2026
            heat-related mortality report</a>). We all know how taxing it is walking out in the direct sun on a hot, humid day, but some shade makes things a little more manageable, and that's what this app tries to help with.
          </p>
          <p>
            However, an app is not heat safety. For the real thing, the city
            government has resources that can help: NYC Emergency Management's
            <a href="https://www.nyc.gov/site/em/ready/extreme-heat.page"> Beat
            the Heat</a> page, the Health Department's
            <a href="https://www.nyc.gov/site/doh/health/emergency-preparedness/emergencies-extreme-weather-heat.page"> Hot
            Weather and Your Health</a>, and the
            <a href="https://finder.nyc.gov/coolingcenters"> cooling center
            finder</a> during heat emergencies. If you or someone near you shows
            signs of heat illness, call 911.
          </p>
        </section>

        <section>
          <h2>Data</h2>
          <p>Shade Walker mainly relies on three public datasets:</p>
          <ul>
            <li>
              <strong>The map and the routing</strong> come from
              <a href="https://www.openstreetmap.org/about"> OpenStreetMap</a>:
              every sidewalk, crosswalk, park path and stairway we route
              along, with each side of a street as its own path.
            </li>
            <li>
              <strong>The trees</strong> come
              from the <a href="https://tree-map.nycgovparks.org/">NYC Tree
              Map</a>, the city's constantly updated record of roughly 900,000
              individually mapped street and park trees. For each tree we use its location to put it on our map, the trunk
              diameter to estimate the shade produced by its canopy, and species to decide if its shade
              survives the winter (deciduous vs evergreen).
            </li>
            <li>
              <strong>Park canopy</strong> comes from a 2021 aerial
              land-cover survey of NYC, which sees treetops from above. It fills in some gaps where the tree dataset doesn't
              cover (like Central Park, and a few other areas) so those paths still receive shade credit.
            </li>
          </ul>
          <p>
            The background map is drawn by
            <a href="https://carto.com/attributions"> CARTO</a>, and address
            search is answered by
            <a href="https://photon.komoot.io/"> Photon</a>, an open geocoder
            from komoot. We refresh our copies of the map and the tree dataset once a
            month.
          </p>
        </section>

        <section>
          <h2>Route scoring</h2>
          <p>
            Every block or path in the city gets a shade score derived from how many
            trees are near it, how big they are, and how
            much canopy the aerial survey sees overhead (for parks not covered by the original tree dataset). It also takes
            into account that certain trees lose their leaves in the fall/winter, so the same street scores shadier in July
            than in April.
          </p>
          <p>
            When you ask for a route, the app weighs your walking time
            and the shade along your potential routes. The Shade_priority setting is the exchange rate between them. NONE
            ignores trees entirely and gives you the plain fastest walk. LOW takes a shadier street only when it costs
            nearly nothing. MED accepts short detours. MAX will take the longest detour to stay under the trees. The panel
            below the priority switcher shows
            what the shadier choice costs you in minutes/distance but also gets you in terms of shade/trees.
          </p>
        </section>

        <section>
          <h2>Gaps</h2>
          <ul>
            <li>
              <strong>Deep park woods.</strong> The city's tree record covers
              individually cared-for trees. In dense woodland, like the middle
              of Pelham Bay Park's forest, there are no individual records, and
              the aerial survey fills it less precisely because we don't get the same amount of data from it (like
              species, diameter, condition) and it's also slightly outdated.
            </li>
            <li>
              <strong>Young trees.</strong> Since the aerial survey is from 2021
              anything that's been planted since then is undercounted.
            </li>
            <li>
              <strong>The tree count.</strong> It counts only individually mapped
              trees, not park canopy, so a route shaded mostly by canopy won't
              show one.
            </li>
            <li>
              <strong>Some places can't be routed</strong>, and that's due to data limitations from our reliance on
              OpenStreetMap.
              We rely on OSM for our information about NYC's sidewalks, paths and
              crossings, and because OSM is built by volunteers, its
              sidewalk coverage is uneven. The thinnest coverage areas are in the Bronx,
              Staten Island and the outer edges of the city.
            </li>
            <li>
              The good news is that anyone can fix this.
              <a href="https://www.openstreetmap.org/fixthemap"> Map a missing
              sidewalk on OpenStreetMap</a> and it will show up in Shade Walker
              within about a month, when we do our monthly map refresh.
            </li>
          </ul>
        </section>

        <section id="caution">
          <h2>Trust your eyes</h2>
          <p>
            Shade Walker is built from data, and data is always a step behind the
            street. Scaffolding goes up, sidewalks close, a storm takes down a
            tree, and our map refreshes monthly, so none of that reaches the app
            right away. If a route tells you one thing and the street in front
            of you says another, believe the street.
          </p>
          <p>
            Treat the routes as suggestions, not instructions: stay aware of
            your surroundings, cross where it's safe rather than exactly where
            the line crosses, and use your own judgment about where to walk.
            The app knows where the trees are. You know everything else.
          </p>
        </section>

        <section>
          <h2>Credits</h2>
          <p>
            Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap
            contributors</a>, available under the Open Database License. Tree
            data from <a href="https://opendata.cityofnewyork.us/">NYC Open
            Data</a> and NYC Parks Forestry. Basemap tiles by
            <a href="https://carto.com/attributions"> CARTO</a>. Geocoding by
            <a href="https://photon.komoot.io/"> Photon</a>, from komoot.
            Shade Walker is open source, and you can
            <a href="https://github.com/camrynobscura/shadewalker"> read the
            code on GitHub</a>.
          </p>
        </section>
      </main>

      <footer className="footer">
        <p><a href="/"><span aria-hidden="true">&#62; </span>back to the map</a></p>
      </footer>
    </>
  )
}
