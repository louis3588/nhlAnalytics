# Public beta: static Pages, local data engine

## Decision and repository audit

Use **Cloudflare Pages Free with Wrangler Direct Upload**, serving the React/Vite
build and selected JSON snapshots. Keep Python ingestion, TimescaleDB, training,
research and the prospective worker local. No hosted database, Python server,
Worker Function, Redis, paid service or custom domain is required for this beta.

The audit found that player/team/profile screens already consume small API DTOs
from a saved SQLite serving cache; they do not need database queries in production.
The previous frontend made same-origin `/api` requests, while live GameCenter
also opened SSE connections. The display catalog contained local source paths.
Vite's normal public-directory copy would have included that catalog, so the new
public build uses an explicit file allowlist instead. The existing custom SPA
pathname routing remains in place; no router replacement or redesign was needed.

| Feature | Public beta | Local development |
| --- | --- | --- |
| Player projections, recorded stats, 80%/95% prediction intervals | Latest published current season; both existing baselines | Existing API and saved seasons |
| Player profiles and history graphs | Up to 32 most recent saved dates per player, loaded on demand | Full saved API history |
| Team pages, filters, logos and headshots | Preserved; latest exported standings added | Preserved |
| Archived production Elo and schedules/results | Saved current-season snapshot, original prediction timestamps | Existing archives/API |
| Elo/logistic prospective comparisons | Only genuinely sealed pairs; pending/missing states preserved | Worker seals new pairs locally |
| Prospective evaluation and historical benchmarks | Saved summaries in separate sections; no hosted inference | Existing live cache and reports |
| Live scores, live player stats, play-by-play and SSE | **Deferred**; read-only saved game page instead | Existing collector/GameCenter unchanged |
| Training, ingestion, historical queries, raw MoneyPuck/EDGE files | **Excluded** | Local only |

These are prediction intervals, not confidence intervals. No model is retrained,
retuned or promoted by publication. An outcome correction can change evaluation
scores, but publication only reads the immutable forecast ledger and preserves
its probabilities, versions, IDs and generation times.

## Generate a public snapshot

From `D:\Code\Snakes\nhlAnalytics`, with the existing environment and Timescale
available, complete the normal daily refresh first. Keep the API/prospective
worker running independently for tonight's seals. Then:

```powershell
$env:TEMP='D:\Code\Snakes\nhlAnalytics\.work\tmp'
$env:TMP=$env:TEMP
$env:npm_config_cache='D:\Code\Snakes\nhlAnalytics\.work\npm-cache'
.\.venv\Scripts\python.exe -m nhl_analytics.publication
```

This loads the validated saved serving cache directly, not an HTTP API. It reads
standings, current schedule outcomes and the prospective ledger inside one
read-only repeatable-read database transaction. It never migrates or writes the
database. The exporter uses the existing API DTO conversion functions, so the
Python `api` extra is required locally; it is not uploaded.

For an explicitly reduced artifact-only publication without Timescale, use
`--offline`. This intentionally omits standings and prospective evaluation and
does not silently invent replacements. The full beta snapshot should normally
use the database-backed command above. `--history-limit 16` can reduce payloads;
the default is 32 saved dates and the maximum is 64.

Output is ignored by Git under `dashboard/public/publication/`:

```text
latest.json
releases/<content hash>/manifest.json
releases/<content hash>/metadata.json
releases/<content hash>/catalog.json
releases/<content hash>/players.json
releases/<content hash>/intervals.json
releases/<content hash>/fixtures.json
releases/<content hash>/standings.json
releases/<content hash>/prospective.json
releases/<content hash>/players/<NHL player ID>.json
```

Only the latest season is public. Historical game-level research data remains
local; historical **aggregate model benchmarks** retain their reconstructed
labels. Identities are limited to projected players and current-season teams.
Goalie projection models do not exist yet, so this does not claim goalie forecasts.

The exporter checks identity links, DTO schemas, scoring consistency, uncertainty
records, probability bounds, source/calibration timing and sealed cutoff timing.
It rejects private fields, filesystem paths, nonfinite values and payload budget
violations. Files have SHA-256 hashes and deterministic gzip sidecars. Identical
inputs produce the same immutable release ID; partial failures leave `latest.json`
unchanged. A release has an 8 MiB per-file and 150 MiB total uncompressed budget.
An initial full snapshot contained 667 skaters and approximately 19 MB of JSON;
profile history is fetched only when needed. Pages negotiates network compression;
the upload excludes the transfer-oriented `.gz` sidecars.

Validate an existing release by substituting the ID from `latest.json`:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.publication --validate dashboard/public/publication/releases/RELEASE_ID
```

## Build and preview without Python services

```powershell
cd dashboard
npm ci
npm run build:public
npm run preview:public -- --port 4174
```

Open `http://127.0.0.1:4174`. The public preview has **no API proxy**. It can serve
the app with FastAPI and Timescale completely unavailable. Do not stop the real
local tracker just to preview: browser tests block API calls and verify independence
without interrupting pregame capture.

`build:public` uses `.env.public` (`VITE_DATA_MODE=static`) and produces
`dashboard/dist-public`. It refuses an API-mode override. Only that directory is
deployable. It includes bundled React/CSS/fonts, reviewed images, Cloudflare
headers/routing files and validated public snapshots. The local display catalog,
Python source, `.env`, database files, model coefficients, raw feeds and research
archives are excluded. Generated public datasets remain ignored by default.

Normal `npm run dev` / `npm run build` still use FastAPI and live GameCenter.
Wrangler is pinned as a development dependency; no Wrangler code is served to
visitors. Vite exposes only the data-mode environment prefix, not arbitrary
`VITE_*` settings. Never put secrets in browser environment variables.

## Consistency, caching and freshness

Automatic publication is enabled after daily refreshes, pregame batch seals and
validated final results. See [automation, limits and monitoring](AUTO_PUBLISH.md).
It reuses a pinned tested frontend and isolated packaging directories, with its
own 16-date public history cap; manual exports still default to 32 dates.

The browser reads the no-store pointer and pins one release manifest. Every JSON
request uses that release's versioned URL and checks the file hash before using
it. Filters and pagination reuse these cached DTOs. Every visible page checks for
a new release once per minute and reloads the whole page when the pointer changes,
so players, intervals and game evidence switch together. Hidden tabs pause checks
and check again on return. This is polling static metadata, not NHL endpoints.

Pages deployment itself switches the whole built site atomically. The build
retains two previous releases for open tabs. Very old tabs may need a reload if
their release is no longer retained. Asset/release URLs are immutable; HTML is
revalidated and the pointer is no-store. There is no service worker or alternate
cache layer. Original prediction timestamps are preserved; age/staleness is
computed in the browser, not reset to the upload time.

If data looks old, inspect the displayed source/prediction time, run the local
refresh, export again, build again, then deploy the new directory. A static site
does **not** cause your local importer or tracker to run. Forecasts sealed after
the last upload become visible only after another publication/deployment.
Missing predictions remain missing. A publication is not a retrospective seal.

If data is missing, check `publication/latest.json`, the matching manifest and
file hashes. Missing data is shown as unavailable, not replaced by live API calls.
Never fix it by copying a research folder into the website.

## Cloudflare deployment and account setup

The public beta is deployed at **https://nhl-rinkside-beta.pages.dev/**.
The `nhl-rinkside-beta` Pages project and Wrangler browser authorization were
completed on 11 November 2026 with the user's explicit deployment instruction.
The verified deployment is `14ac2569` (its immutable URL is
`https://14ac2569.nhl-rinkside-beta.pages.dev`). HTTPS, security/cache headers,
seven core JSON hashes, the JavaScript asset hash, four deep-link URL paths and
the full public browser suite passed against the production site.
The published snapshot has 667 skaters and source time
`2026-11-11T12:33:32.348593+00:00`; upload time does not replace prediction time.

Pages' native SPA fallback is used: do not add a top-level `404.html` or
redirect dashboard routes to `/index.html`. Those explicit redirects can
canonicalize the browser URL back to `/`, breaking page selection. Missing JSON
may receive the SPA HTML with HTTP 200; the adapter rejects it as invalid JSON
instead of showing fabricated data. Profile/team/game/model paths retain their
original URLs on refresh.

Cloudflare's [agent onboarding prompt](https://developers.cloudflare.com/agent-setup/prompt.md)
is genuine. Its relevant Wrangler skill was installed locally and project-local
Wrangler is authenticated on this Windows user profile. Cloudflare MCP login
was not needed and remains unconfigured; it is optional for the CLI upload path.
Credentials are stored by Wrangler outside the repository and public build.
Do not copy or publish its authentication files.

For another machine or account, the setup procedure is:

1. Sign in to your free Cloudflare account at `https://dash.cloudflare.com/`.
   No domain purchase, paid plan or payment-enabled resource is needed for this
   static design. Do not add paid services for these instructions.
2. Approve the account connection. We can then run `npx wrangler login` from
   `dashboard`; you review and complete its browser authorization yourself.
   Do not paste passwords, API tokens or OAuth codes into chat.
3. Verify the signed-in account with `npx wrangler whoami`. The existing project
   name in `wrangler.jsonc` is `nhl-rinkside-beta`; reuse it for updates.
4. Review the tested `dist-public` snapshot and explicitly authorize publication.
5. Upload the tested public folder. The project already exists, so ordinary
   updates need only the deployment command:

```powershell
npx wrangler pages deploy dist-public --project-name nhl-rinkside-beta --branch main
```

Wrangler 4.160.1 initially attempted to delegate new-project creation to Workers.
For this existing Pages design, the one-time creation command used
`npx wrangler pages project create nhl-rinkside-beta --production-branch main --force`.
The installed CLI confirms this flag opts out of delegation rather than
overwriting a project. **Do not repeat project creation or use `--force` for
normal updates.** No paid plan or dynamic Worker was provisioned.

The provider returns the actual `pages.dev` URL. Verify the deployment result,
deep-link refresh, headers, images, data hashes and timestamps on that URL. A local
preview cannot prove Cloudflare has served the expected headers or connected an
account. For manual updates, export/build/test and repeat only the upload. Normal
automated data updates use the queue instead. Pin a newly tested frontend
explicitly when changing the UI.
Wrangler's login/config files are outside `dist-public`; do not upload them.

After each deployment, check the actual hosted site against the local package:

```powershell
# From dashboard:
node scripts/verify-deployment.mjs https://nhl-rinkside-beta.pages.dev
$env:PUBLIC_DASHBOARD_URL='https://nhl-rinkside-beta.pages.dev'
npm run test:public
```

The HTTP check verifies data and JS hashes, CSP/cache headers and deep-link URL
preservation. Its machine-readable result is in ignored
`dashboard/test-results/deployment.json`. The browser test then exercises the
actual hosted pages without API, SSE or NHL data-feed requests.

### GitHub compatibility

Wrangler Direct Upload does not require connecting the engine repository. Code
and lockfiles remain ordinary GitHub-compatible source. Cloudflare's root/output
settings for a future Git build would be root `dashboard`, build
`npm ci && npm run build:public`, output `dist-public`, Node 24. A clean checkout
intentionally fails the public build unless an approved publication snapshot has
been supplied: GitHub cannot access your local Timescale engine.

If CI is desired later, supply only the reviewed publication directory as an
artifact or use a separate publication repository; keep all local research data
ignored. Authentication would then use narrow Cloudflare secrets in GitHub's
secret store, never committed or embedded in Vite. No repository, workflow secret
or GitHub permission has been connected in this task. Pages Direct Upload and
native Git integration are separate project choices; switching to native Git
integration requires a new Pages project. Wrangler can also upload from CI.

## Limits and later live hosting

Cloudflare documents free, unlimited static-asset requests. Its Pages Free limits
include 500 builds/month, 20,000 files per site and 25 MiB per asset; Wrangler
supports up to 20,000 uploaded files, while dashboard drag-and-drop supports only
1,000. This package is far below the per-file limit, but retained releases count
toward the file count. Monitor deployment quotas and do not assume unlimited
high-frequency updates. These are current provider limits, not a guarantee they
will never change.

Sources: [Pages pricing](https://developers.cloudflare.com/pages/functions/pricing/),
[limits](https://developers.cloudflare.com/pages/platform/limits/),
[Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[headers](https://developers.cloudflare.com/pages/configuration/headers/),
[SPA serving](https://developers.cloudflare.com/pages/configuration/serving-pages/).

For this beta, no browser polls NHL data feeds. Existing NHL-hosted logo/headshot
URLs remain image resources, not GameCenter API requests. The exported statistics
are NHL-derived; MoneyPuck CSV archives and raw EDGE measurements are not shipped.
This preparation does not grant new rights to third-party names/images/data.

Full live GameCenter later needs an appropriately hosted shared collector and
durable latest-state store, plus a push-capable API or an assessed alternative.
A free service that sleeps cannot promise timely pregame sealing or continuous
SSE. Cloudflare Workers could serve a small latest-state cache if an authorized
collector publishes to it, but free request/storage/connection limits, credentials,
provider access conditions and outage handling would need evaluation first.
Neon or Render is not required for this beta and has not been provisioned.
Do not expose the local database or tunnel the whole research engine publicly.

## Checks

```powershell
# Repository root; database is optional for publication unit tests.
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_publication.py -v
# Full existing suite, including database integration when Timescale is running:
$env:NHL_DB_TESTS='1'
$env:OPENBLAS_NUM_THREADS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd dashboard
npm test
npm run build
npm run build:public
npm run test:public
npm audit
```

The public browser suite expects the static preview on port 4174. It visits team,
player, model and game deep links, compares intervals to saved data, checks mobile
layout, verifies release pinning and missing-data states, and fails if an API,
SSE or NHL data-feed request occurs. Existing API-mode live/SSE tests remain
separate. Cloudflare routing/header files are checked locally; repeat the hosted
smoke checks after an authorized deployment.

The public build caps Node heap/semi-space and uses a single build worker lane.
The static preview uses built-in Node modules, not Vite's development server.
If Windows paging capacity is exhausted, close unused applications and temporary
previews before rebuilding; raising the heap limit does not fix exhausted system
commit capacity. No Windows memory/pagefile settings are changed by these scripts.
