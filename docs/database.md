# Local PostgreSQL + TimescaleDB

The Compose configuration follows the official
[TimescaleDB repository](https://github.com/timescale/timescaledb) and uses the
`timescale/timescaledb-ha:pg18` image. This is one local PostgreSQL server with
TimescaleDB available. The image name does not configure a replicated HA cluster.
No Timescale/Tiger Cloud account is needed to self-host it.

## Before the first start

1. Install/start Docker Desktop with Linux containers and Docker Compose v2.
2. In Docker Desktop settings, place the Linux VM/disk image storage on D: before
   pulling the image. This image includes extra tools and can take considerable
   space. A named volume lives inside Docker's storage; putting this repo on D:
   alone does **not** move the database there.
3. If `.env` is not present, copy `.env.example` to `.env` in this project's root.
   Edit `.env` locally and set `POSTGRES_PASSWORD` to a long, unique password.
   Do not post it in chat or commit it. `.env` is already ignored by Git.

For example, the setting has this form; choose your own value:

```dotenv
POSTGRES_PASSWORD='your-own-long-unique-password'
```

Single quotes preserve dollar signs and other literal characters for Compose
interpolation. Avoid embedded single quotes for this simple format. See
[Docker's environment-file guidance](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).
This is a local development credential stored as plain text, not an encrypted
secret vault. Compose refuses to start if the password is unset or empty. An
existing shell `POSTGRES_PASSWORD` overrides the file, so avoid unintended overrides.

## Start and verify (after setting the password)

Run in the repository root:

```powershell
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose exec timescaledb psql -U postgres -d nhl_analytics -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';"
```

Use `config --quiet` for validation: ordinary `docker compose config` prints the
resolved configuration, including the password. The health check confirms server
readiness; the SQL command separately verifies that the extension is enabled.
The initialization script enables TimescaleDB and creates an empty `analytics`
schema. Python's migration command creates the application tables and hypertables
described below; initialization scripts are not rerun on an existing volume.

The `timescaledb-permissions` helper runs as root once before the database service
to give the named volume's `/pgdata` directory to the image's `postgres` user.
Without it, a newly created root-owned volume can cause `initdb` permission errors
and repeated restarts. The helper changes only the directory's ownership and mode;
it does not delete data or recursively change file ownership. Its successful
`Exited (0)` status in `docker compose ps -a` is expected.

`POSTGRES_USER` defaults to `postgres`; if you set a different name in `.env`, use
that name in database clients and `psql -U` commands. The health check uses the
configured name automatically.

## Connect from PyCharm / a database client

| Setting | Value |
| --- | --- |
| Driver | PostgreSQL |
| Host | `127.0.0.1` |
| Port | `6543` |
| Database | `nhl_analytics` |
| User | `postgres` |
| Password | The value you entered in `.env` |

The port binds only to localhost. The local development account is PostgreSQL's
administrator; a later application connection should use a separate limited role.

## Persistence and later changes

`docker compose stop` stops the server. `docker compose start` starts it again.
`docker compose down` removes the container/network while preserving its named
volume. Do not use `down -v` unless you intend to delete the database data.

`database/init.sql` and `POSTGRES_PASSWORD` initialize a new database only. Editing
them does not modify an already initialized volume or change an existing password.
For a password change, connect with `psql`, use `\password postgres` interactively,
then update `.env` to match. Future schema changes will use migrations.

The `pg18` image tag tracks updates rather than an immutable build. Once the first
working instance is verified, record/pin its image digest for reproducibility.
Never switch the PostgreSQL major version on an existing data volume without an
upgrade plan. A volume provides persistence; maintain separate backups as data grows.

The local container and TimescaleDB extension have been verified. Keep using your
existing `.env` credentials; they are not generated or overwritten by migrations.

## Python imports and schema

Run `scripts/setup.ps1` again to install the PostgreSQL driver (`psycopg`) and
environment-file support. It retains the D: cache/temp configuration.
The Python process reads `.env` from the working directory by default. Shell
environment variables take precedence. Optional settings are `POSTGRES_HOST`
(default `127.0.0.1`), `POSTGRES_PORT` (`6543`), `POSTGRES_DB` (`nhl_analytics`) and
`POSTGRES_USER` (`postgres`). `POSTGRES_PASSWORD` is required. Passwords are passed
as connection parameters, so URL escaping is unnecessary. Credentials are not logged.

From the project root:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.database migrate
.\.venv\Scripts\python.exe -m nhl_analytics.database load data/20262027/2/<run-timestamp>
.\.venv\Scripts\python.exe fetch_rosters.py --season 20262027 --database
```

Replace `<run-timestamp>` with an existing completed snapshot directory.
`nhl-db --env-file D:\path\to\.env load <snapshot>` uses an alternate settings file;
the Python module accepts the same options. `--database` is optional, so JSON-only
imports continue to work without PostgreSQL. If a database write fails, the
completed JSON snapshot remains available for retry with `nhl-db load`.

| Table/view in `analytics` | Grain/purpose |
| --- | --- |
| `schema_migrations` | Version/checksum of each applied migration |
| `seasons` | NHL season ID |
| `players` | NHL player ID and available names/birth information |
| `teams` | NHL team ID and latest imported name/abbreviation/conference/division |
| `import_runs` | Source, season, game type, collection timestamp; content checksum and manifest |
| `roster_observations` | Run, player, team, roster observation time; Timescale hypertable |
| `skater_season_stats` | Run, player, collection time; Timescale hypertable |
| `goalie_season_stats` | Run, player, collection time; Timescale hypertable |
| `latest_skater_season_stats` / `latest_goalie_season_stats` | Latest observed row per player/season/game type |

Roster rows retain jersey number, position/group, handedness, physical measurements,
headshot and original player JSON. Statistics expose common metrics as typed columns
and retain every source field under `payload` JSONB. For example, skater `ppPoints`
is available as `payload->>'ppPoints'`. Rates remain fractional and ice time remains
in seconds. Team abbreviations in season totals may combine multiple teams, so
these rows deliberately have no single team foreign key. They are not team splits.
Birth information is nullable for players absent from rosters.

Each complete snapshot is validated and loaded atomically. Failed rows roll back
the entire load, including identities, rosters and run metadata. The same snapshot
is skipped on reload; changing the files for an already imported observation is
rejected by checksum. A new collection timestamp creates history. Only JSON inputs
from this importer are accepted; the downloaded MoneyPuck CSVs are not loaded yet.

Hypertables use timezone-aware timestamps and one-month chunks. Keys include the
time column, as Timescale requires. Snapshot collection time is not game time or
proof of what was known historically. Roster observations are not transaction
dates, and season roster membership is not a current-roster assertion. Reused
roster files retain their original `rosterObservedAt` timestamp.

Migrations are packaged under `src/nhl_analytics/migrations/`. The migration runner
applies pending files in order, in a transaction under a database lock. Applied
SQL is checksum protected: add a new numbered migration instead of editing an
already applied file. Database loads automatically apply pending migrations first.
The configured user currently needs DDL privileges; separating migration and
application roles is future work.

Example queries in PyCharm's database console:

```sql
SELECT p.player_id, p.full_name, s.games_played, s.goals, s.assists, s.points
FROM analytics.latest_skater_season_stats s
JOIN analytics.players p USING (player_id)
WHERE s.season_id = 20262027 AND s.game_type_id = 2
ORDER BY s.points DESC NULLS LAST;

SELECT player_id, collected_at, points
FROM analytics.skater_season_stats
WHERE player_id = 8478402 AND season_id = 20262027 AND game_type_id = 2
ORDER BY collected_at;

SELECT hypertable_name, num_chunks
FROM timescaledb_information.hypertables
WHERE hypertable_schema = 'analytics';
```
