# Rinkside dashboard

A separate React + JavaScript project for the saved NHL prediction API. Custom
CSS, locally bundled Inter fonts, labelled charts, floating hover details and a
responsive layout; no component framework or chart-library dependency.

## Run locally

Requirements: Node 22.12+ (tested with 24.22.1), npm, and the existing FastAPI
service. Timescale does not need to run to view saved forecasts.

If the API is not already running, start it from the **Python project root**:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.api --run data/evaluations/reconstructed/20261110T210453233933Z
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
- **Games:** both teams' win probabilities are labelled. Dates/times use Eastern
  time, including daylight saving. `Upcoming` is relative to the saved snapshot;
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
