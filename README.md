# NHL Analytics

Python data foundation for player, game and season predictions. Currently imports
NHL rosters and skater/goalie season summary statistics into linked JSON snapshots.
PostgreSQL/TimescaleDB storage and chunked MoneyPuck CSV imports are supported. Scheduling, EDGE,
Parquet and ML are future work.

## Setup on Windows / PyCharm

Requires Python 3.12 or newer. No database, API key or paid data account is required
for this importer. Keep the repository and generated data on D:.

From the project root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

The script uses the existing `.venv` when present, installs the project in editable
mode, and places pip caches/build temp files in `.work` on the project drive.
The execution-policy override applies to this invocation only. It does not alter
the machine policy. The script's temp/cache settings affect its process and children.

In PyCharm choose `.venv\Scripts\python.exe` as the project interpreter. Create a
Python run configuration for `fetch_rosters.py`, set the working directory to this
repository, and set parameters to `--season 20262027`.

## Run

Fetch rosters and regular-season statistics:

```powershell
.\.venv\Scripts\python.exe fetch_rosters.py --season 20262027
```

To also load a new import into the running database using `.env` credentials:

```powershell
.\.venv\Scripts\python.exe fetch_rosters.py --season 20262027 --database
```

Apply database migrations, or load an existing completed snapshot without fetching again:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.database migrate
.\.venv\Scripts\python.exe -m nhl_analytics.database load data/20262027/2/<run-timestamp>
```

The installed `nhl-db` command provides the same subcommands. See
[database loading](docs/database.md#python-imports-and-schema) for tables and SQL examples.

Reuse the supplied roster export (team IDs are resolved from NHL):

```powershell
.\.venv\Scripts\python.exe fetch_rosters.py --season 20262027 --roster-file nhl-rosters.json
```

Historical season or playoffs:

```powershell
.\.venv\Scripts\python.exe fetch_rosters.py --season 20252026
.\.venv\Scripts\python.exe fetch_rosters.py --season 20252026 --game-type 3
```

Use `--output D:\path\to\datasets` to change the destination. The installed CLI
`.\.venv\Scripts\nhl-fetch.exe` accepts the same arguments.

Each run creates `data/<seasonId>/<gameTypeId>/<UTC-run-timestamp>/` containing:

| File | Contents |
| --- | --- |
| `raw/*.json` | Original response payloads, URLs and retrieval timestamps |
| `nhl-rosters.json` | Teams and nested roster players, with resolved NHL team IDs |
| `players.json` | Unique identities, including season participants absent from rosters |
| `player_season_stats.json` | Separate `skaters` and `goalies` arrays |
| `manifest.json` | Counts and completion marker, written last |

Join `players[].id` to `skaters[].playerId` or `goalies[].playerId`.
Only consume snapshots with a completed manifest. Failed runs can leave raw files
and partially written outputs; previous completed snapshots are never overwritten.
Re-running creates a new observation, not duplicate rows within an existing file.

The supplied roster file remains unchanged. Its observation time is retained in
`rosterObservedAt`; a newer stats import does not make an older roster current.
Season-specific roster endpoints may include season roster members and must not
be interpreted as precise current membership or trade history.

Statistics are whole-season player totals for the selected game type. A traded
player's `teamAbbrevs` may list several teams: do not assign all their season points
to their current roster team. Per-team splits require a later importer.
Players found only through statistics have partial identities (ID/full name), not
invented birth dates or current teams. Roster jersey/position/physical fields are
observations, not immutable biographical facts.

## Verification and troubleshooting

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

To include live database tests (fixture inserts are rolled back):

```powershell
$env:NHL_DB_TESTS = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:NHL_DB_TESTS
```

Default tests use mocked HTTP results and do not require network access. Live verification
requires running an import and checking its manifest. The NHL endpoints are not a
guaranteed developer contract: 403 responses fail clearly; 429/server errors retry
with backoff and each request has a timeout. Empty season results fail rather than
publishing a misleading complete dataset. Check season/game type and connectivity.

Generated data, caches and `.venv` are ignored by Git. Previously tracked `.idea`
files remain tracked; this setup does not remove or alter your staged IDE changes.

See [the dataset and database guide](docs/datasets.md) for sources, grains,
time-series design and TimescaleDB options.

Local database configuration is ready in [compose.yaml](compose.yaml). Follow
[the database setup guide](docs/database.md), set your password in the ignored
`.env` file, and configure Docker storage on D: before starting it. Database
startup is separate; `--database` optionally loads completed importer snapshots.

See [the downloaded CSV inspection](docs/downloaded-data.md) for sample findings,
including a possible prior-season lines file mismatch and CSV header/ID caveats.

Import downloaded MoneyPuck CSVs (safe to rerun/resume):

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.moneypuck data --chunk-size 3000
```

See [MoneyPuck loading and queries](docs/moneypuck.md) for supported grains,
player links, checkpoints and complete/latest views.

Fetch and load NHL player game logs for both regular seasons:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.game_logs --seasons 20252026 20262027
```

See [game-log imports and queries](docs/game-logs.md) for resume commands,
player/game relationships, duplicate prevention and correction history.

Refresh league schedules and results in the database, retaining JSON snapshots:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.schedules --seasons 20252026 20262027
```

See [schedules and results](docs/schedules.md) for status fields, final scores,
upcoming games and replay commands. Regular season and available playoffs are included.

Refresh standings and optional team tracking features:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.team_context --seasons 20252026 20262027 --edge
```

See [team standings, EDGE and name cleanup](docs/team-context.md) for table names,
metric units, historical dates, name-inference flags and example queries.

Generate dataset health reports and reusable point-in-time features:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.health
.\.venv\Scripts\python.exe -m nhl_analytics.features
```

Feature export writes player, goalie and team CSV/Parquet datasets plus health
reports and a provenance manifest. See [feature definitions and cutoff policy](docs/features-and-health.md).
Historical cutoffs before our first recorded imports intentionally produce no
eligible data; downloaded season finals are never backdated into earlier predictions.

Historical backfill and explicitly reconstructed baseline models:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.game_logs --seasons 20212022 20222023 20232024 20242025
.\.venv\Scripts\python.exe -m nhl_analytics.schedules --seasons 20212022 20222023 20232024 20242025
.\.venv\Scripts\python.exe -m nhl_analytics.reconstructed
.\.venv\Scripts\python.exe -m nhl_analytics.baselines --history data/reconstructed/RUN_TIMESTAMP
```

See [backfill, player projections and Elo](docs/baselines.md). Reconstruction
uses a labelled next-day availability assumption and writes outside the strict
feature directory. Baseline inputs, future labels, forecasts, evaluation metrics
and model parameters are separate artifacts. Keep collecting real observation
history for strict point-in-time evaluation.

Add monthly player diagnostics, development-only Elo experiments, reliability
diagrams and historical residual prediction intervals without replacing the
original baseline artifacts:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[analysis]'
.\.venv\Scripts\python.exe -m nhl_analytics.evaluation --baseline data/baselines/reconstructed/20261110T193519626194Z
```

See [evaluation methodology, findings and uncertainty limits](docs/evaluation.md).
The 2025–26 benchmark is excluded from parameter selection and interval fitting.
New reports live under `data/evaluations/reconstructed/`; `--no-plots` omits the
optional plotting dependency.

Serve saved player projections, intervals and improved Elo predictions:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[api]'
.\.venv\Scripts\python.exe -m nhl_analytics.api
```

Open <http://127.0.0.1:8000/docs>. The API indexes saved artifacts at startup and
uses read-only queries on a disposable D: cache, with no request-time training,
CSV scans, NHL requests or Timescale dependency. See [endpoints, filters and refresh instructions](docs/api.md).

The React dashboard is a separate JavaScript project in [dashboard/](dashboard/README.md):

```powershell
cd dashboard
npm.cmd ci
npm.cmd run dev
```

With the API running on port 8000, open <http://127.0.0.1:5173> for season/team
filters, player projections and labelled interval charts, searchable player
tables and upcoming game probabilities. Its local proxy connects to FastAPI;
fonts and display names are bundled locally. See the dashboard README for
configuration, production builds and browser checks.

Daily source refresh and append-only forecasts:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/daily-refresh.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-daily-refresh.ps1 -Time '09:30'
```

The Windows task is installed for the signed-in user. It refreshes current-season
rosters, skater/goalie season stats and game logs, schedules/results, standings,
team EDGE and display identities. Forecast archives retain previous days; the
API follows new publications automatically when started without `--run`.
See [daily automation, recovery and free live-data design](DAILY_REFRESH.md).

Live GameCenter collection now runs with the unpinned API. Open **Live & recent
games** or a forecast card's **Open game center** link for scores, player stats,
team comparisons and play-by-play pushed through Server-Sent Events. Changed
snapshots and event corrections are saved separately in Timescale. See
[live-game setup, endpoints, retention and tests](LIVE_GAMES.md).

Compare a pregame logistic model with frozen Elo using the existing seasons:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-compile -e '.[models]'
.\.venv\Scripts\python.exe -m nhl_analytics.team_models
```

See [team feature definitions, chronology and comparison results](TEAM_MODELS.md).
The experiment keeps labels separate, uses training-only preprocessing, preserves
the reserved benchmark and does not replace production forecasts.

The unpinned API also runs the [prospective model tracker](PROSPECTIVE.md):
both frozen models are sealed before each game's cutoff and scored only after
validated finals. Open `/models` for the forward test, kept separate from the
historical benchmarks. Keep the API, Docker and the computer running through
the cutoff windows; missed predictions are recorded rather than backfilled.

For the £0/month public beta, see [static publishing and Cloudflare setup](PRODUCTION.md).
`python -m nhl_analytics.publication` exports reviewed public snapshots;
`npm run build:public` inside `dashboard` builds the server-independent Pages site.
Account sign-in and public deployment remain separate, explicit steps.

Public beta: **https://nhl-rinkside-beta.pages.dev/**. Wrangler authentication and
the initial deployment are complete. The website serves published snapshots;
the local engine and prospective tracker remain responsible for new data.

[Automatic publishing](AUTO_PUBLISH.md) queues daily refreshes, sealed pregame
forecasts and validated finals, then updates Pages with batching, deduplication,
retries and conservative free-tier caps. Keep the PC signed in, Docker available
and the local collector running for fresh source data.
