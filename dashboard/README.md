# Rinkside dashboard

A separate React + JavaScript project for the saved NHL prediction API. Custom
CSS, locally bundled Inter fonts, labelled charts, floating hover details and a
responsive layout; no component framework or chart-library dependency.

## Team identities and dedicated pages

Use **Team pages** in the sidebar to open `/teams`, a season-aware team
directory. Every card links to a dedicated, shareable projection page, e.g.
`/teams/12?season=20262027` for Carolina. Team pages retain the season/model/
target/interval controls, player table and matching fixtures, styled with that
team's colours. Game logos and the player-focus team name also link to these
pages. Direct links and browser reloads work through Vite's SPA fallback; a
separately deployed static host needs the same index.html fallback for `/teams/*`.

The team ID stays explicit: `/teams/59?season=20242025` is Utah Hockey Club,
while `/teams/68?season=20262027` is Utah Mammoth. Selecting a season in which
that ID is absent shows no matching forecasts rather than silently substituting
another identity. Choose the appropriate team from the directory/dropdown.

Player portraits use the forecast's own season, last-played team abbreviation,
and NHL ID:

```text
https://assets.nhle.com/mugs/nhl/{season}/{teamAbbreviation}/{playerId}.png
```

If the generated image fails, a distinct saved `headshot` string from the local
roster catalog is tried. That fallback may be a more recent portrait. If no
usable image remains, a neutral silhouette replaces the image, with no retry
loop or fabricated portrait. No database lookup is needed. Headshots load
directly from NHL assets; table portraits are lazy-loaded.

Team logos are official NHL SVGs bundled in `public/team-logos/`, including
season-specific variants where our saved metadata supplies them. Representative
team colours in `src/teamBranding.js` coordinate the team header, chart, selected
rows and player focus. Matchup probability segments use the competing clubs'
colours; matching primary colours use an available secondary colour, and away
segments have subtle stripes so labels/patterns also distinguish them. Palette
themes do not claim to reproduce every historical or alternate jersey.

Logos do not require remote requests during ordinary use. To explicitly refresh
the official assets after adding new seasons/identities:

```powershell
npm.cmd run assets
```

This opt-in command downloads only official NHL SVGs, at most two concurrently;
normal dev/build does not fetch assets. `palette-sources.json` records colours
found in the source logo files. Saved roster headshot strings are display
metadata and never become prediction features. Forecast values and probability
segment widths still come directly from the unchanged API.

## Run locally

Requirements: Node 22.12+ (tested with 24.22.1), npm, and the existing FastAPI
service. Timescale does not need to run to view saved forecasts.

If the API is not already running, start it from the **Python project root**:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.api
```

Then open another terminal:

```powershell
cd D:\Code\Snakes\nhlAnalytics\dashboard
npm.cmd ci
npm.cmd run dev
```

Visit **http://127.0.0.1:5173**. Use Ctrl+C in that terminal to stop the dashboard.
`npm.cmd` avoids PowerShell's script execution-policy restriction on `npm.ps1`;
no policy changes are necessary. In PyCharm, select this folder's `package.json`
and run its `dev` npm script, independently of the Python API configuration.

The project `.npmrc` directs npm's cache to the parent's `.work/npm-cache` on D:.
For browser tests/build helpers, keep temporary files on D: as well:

```powershell
$env:TEMP = Join-Path (Split-Path $PWD) '.work/tmp'
$env:TMP = $env:TEMP
```

Vite proxies `/api` to `http://127.0.0.1:8000`. This avoids browser CORS setup
for local development and preview. To use another API port/address, copy
`.env.example` to `.env.local`, change `API_TARGET`, and restart Vite. It is a
server-side proxy setting, not a browser credential. Do not put database
credentials in the JavaScript project.

## Explore

- **Season and team:** affect player projections, intervals and games. Team
  filters use last-played team for skaters, and home/away team for fixtures.
  Historical Utah Hockey Club (59) and Utah Mammoth (68) remain distinct.
- **Points / goals / assists / games:** changes the chart, interval target,
  median statistic and initial table ordering.
- **Prior-season blend / current pace:** selects the corresponding saved player
  model. Neither action trains or runs a model.
- **80% / 95%:** selects saved nominal marginal prediction intervals. A missing
  historical calibration is explicitly unavailable; a failed request is shown
  separately from missing calibration.
- **Player focus:** click a chart row or a table player's name/arrow to inspect
  their recorded total, projected total, interval and games context. Chart axes,
  units, endpoints, recorded/projected legends and hover values are labelled.
- **Search and table:** search names, NHL IDs or team names; sort via column
  headings, and page through ten players at a time. The full filtered cohort is
  fetched in API pages before calculating leaders or medians, so these are not
  misleading first-page statistics.
- **Games:** both teams' win probabilities are labelled. Dates/times use the
  viewer's browser timezone, including daylight saving. `Upcoming` uses the current clock;
  `All saved` also displays completed fixtures' pregame predictions. Historical
  seasons honestly have no upcoming games, with a button to view saved forecasts.
- **Refresh:** rereads the saved API bundle; it does not fetch NHL data or train.
  Saved dates and stale-data notices remain visible. Aborted/older filter
  requests cannot overwrite a new selection, and mixed bundle pages fail clearly.

Prediction intervals can be wide and do not imply joint coverage across goals,
assists and points. The expandable methodology note explains these limitations
without exposing backend implementation details in the forecast controls.

## Display names

The current prediction API intentionally returns player/team IDs rather than
player names. `scripts/build-catalog.mjs` creates `public/catalog.json` from
small, already-saved NHL game-log participant plans and schedule snapshots in
the parent project. It reads no shot CSV, uses no database, and makes no NHL
requests. Current catalog: 1,691 player names and 34 historical team identities.

This is display metadata only; all projections, observed counts, ranges and
probabilities come from FastAPI. `predev`/`prebuild` refresh the catalog. The
bundled catalog also permits a frontend checkout without the raw directories;
unknown future IDs are labelled honestly rather than assigned guessed names.
Catalog names are not prediction-time model features.

## Build and test

```powershell
npm.cmd test
npm.cmd run build
npm.cmd run preview
```

The production build is in `dist/`; preview is at http://127.0.0.1:4173 and also
proxies `/api`. A separately deployed static host will need its own `/api`
reverse proxy to the Python service; the build does not embed that service.

Browser integration checks use an installed Edge browser, with no browser
download. Keep the API and dev server running, then:

```powershell
npm.cmd run test:browser
npm.cmd run test:teams
```

For another installed Chromium browser, set `BROWSER_EXECUTABLE` to its absolute
executable path. `DASHBOARD_URL` can target a different local preview URL.
The test runs one browser page to limit memory and always closes the browser.
Screenshots and the machine-readable report are in ignored `test-results/`.

Verified: production build, six data-handling tests and eight browser check
groups covering real API data, filters, search, model/target/nominal selection,
pagination, player focus, historical unavailable intervals, Utah IDs, hover
details, 390px mobile overflow, API recovery and partial interval-request failure.
No browser JavaScript errors were reported. Python sources and saved prediction
artifacts were not modified by this frontend work.

Team identity verification adds three data tests (nine total) and five browser
check groups: directory and team-specific data/theme, real portraits/logos,
deep links/reloads, historical IDs, failed-image fallbacks and mobile layouts.
The original eight dashboard browser check groups also pass. Team screenshots
include `test-results/carolina-team-page.png` and `test-results/team-page-mobile.png`.

### Player pages and local game clocks

Click a player's name, portrait in the table, or comparison-chart row to open
`/players/{nhl_id}?season=20262027`. The small table arrow still selects the
dashboard's quick preview. Player pages show the last played team's colours,
headshot, recorded goals/assists/points/games, projected final totals, and saved
80% or 95% marginal prediction intervals. Season, model, graph statistic and
interval selections persist in the URL for sharing and reloading.

The labelled history graph reads saved forecasts through
`GET /api/predictions/players/{player_id}/history`. Optional parameters are
`season`, `model` (`pace` or `prior_shrinkage`), `target` (`goals`, `assists`,
`points`, `games`), `nominal` (`0.8` or `0.95`), `limit` (maximum 200), and
`offset`. Results are chronological `{projection, interval}` pairs with bundle
provenance. Unlike the overview's global latest snapshot, history includes a
player's earlier snapshots even if absent from the latest cohort. An unknown
player/season returns 404. This endpoint reads the existing immutable serving
cache; no model training, CSV scans or Timescale requests occur per request.

Date buttons update the cards to that snapshot. Exact values are also available
in an accessible table. On narrow screens the graph scrolls horizontally so
axis labels stay readable. The original 2026–27 bundle contained one snapshot;
daily publications now append new points. Historical seasons have multiple snapshots. No invented daily
trend or future labels are added. Recorded totals are current **as of the
selected saved date**, rather than a live NHL feed. Prediction intervals retain
their original historical calibration and reconstructed provenance.

Game cards use `Intl.DateTimeFormat().resolvedOptions().timeZone`, with local
date, time and zone abbreviation. UK devices automatically show GMT in winter
and BST in summer. This uses the device timezone setting and requires no GPS,
IP lookup, browser permission or location storage. Source UTC timestamps and
prediction-date labels remain unchanged. A misconfigured device timezone must
be corrected in the operating system/browser.

Run `npm.cmd run test:players` with the API and dev server running for player
navigation, history, filters, error states, mobile layout and London clock checks.
Unit tests also cover UK daylight saving and a US date rollover.

### Daily publications

The API now follows daily saved forecasts when started without `--run` (and with
`NHL_PREDICTIONS_RUN` blank). Visible dashboard tabs check for a new bundle each
minute and when returning to the tab. Game lists refresh each minute against the
current clock, rather than the original bundle's date. Device-local times still
apply. Player history preserves the older reconstructed points and appends new
observed daily points; their provenance is described on the player page.

Display identities now come from `/api/catalog`, with a bundled-catalog fallback
inside the API, so a production build can show newly imported players without
rebuilding JavaScript. The large model/shot datasets are never sent to the browser.
As the history grows, graph axis labels stay bounded and a saved-date selector
provides access to older snapshots; the exact data table retains every point.

See [daily automation and the proposed live-game scope](../docs/DAILY_REFRESH.md)
for scheduled-task setup, failure recovery and the free GameCenter polling/SSE
design. Daily forecasts are separate from the planned live score service.

`npm.cmd run test:refresh` adds three browser check groups for minute-based game
refresh, automatic coherent bundle updates, and a graph with 30 daily points.

### Live game center

`/games/{nhl_id}` pages show GameCenter scores/clock, team comparisons,
skater/goalie tables and a corrected event timeline. Open them from **Live &
recent games** or a forecast card's **Open game center** link. The unpinned API
uses one shared collector with Timescale durability, while React receives full
replacement states over EventSource. Reconnects retain cached scores, missing
pregame stats stay unavailable, and saved Elo remains explicitly pregame.
Device-local game times still apply. `npm.cmd run test:live` covers real data,
reloads/mobile layout and controlled SSE update/correction/recovery paths.
See [the live-game guide](../docs/LIVE_GAMES.md) for endpoints and retention.
