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
