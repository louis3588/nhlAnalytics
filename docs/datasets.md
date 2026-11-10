# Dataset and database plan

## Collect now

Use NHL data as the identity and traditional-statistics backbone. The importer
requires no manual dataset download. Start with 2026–27 regular season (`20262027`,
game type `2`), then completed seasons. Keep playoff data (`3`) separate.

| Dataset | Record grain | Source / status |
| --- | --- | --- |
| Players | NHL player ID | Roster fields plus partial identities from season summaries; implemented |
| Roster observations | Player, team, observation/run | NHL roster API; implemented export, not historical transaction dates |
| Skater season summary | Player, season, game type, collection run | NHL Stats skater summary; implemented |
| Goalie season summary | Player, season, game type, collection run | NHL Stats goalie summary; implemented |
| Raw responses | Request and retrieval timestamp | Implemented; retain to reproduce transformations |

API families used: `https://api-web.nhle.com/v1` and
`https://api.nhle.com/stats/rest/en`. Team discovery checks standings season IDs;
past seasons use April 1 standings rather than assuming today's teams existed then.
Arbitrary future seasons or seasons without available standings are rejected.

Skater summaries include games played, goals, assists, points, shots, shooting
percentage, average ice time, special-team scoring and other source fields.
Goalie summaries include games/starts, wins/losses, saves, shots/goals against,
save percentage, goals-against average, shutouts and ice time.
Source percentages are fractional values (e.g. `0.915` means 91.5%); ice time fields
are seconds. Null means missing/not applicable, not zero. Additional metrics such
as hits, blocks, primary assists and PP ice time need additional reports.
We preserve the source fields rather than claiming the summary contains everything.

## Add next

| Priority | Dataset | Why |
| --- | --- | --- |
| 1 | NHL schedules/results and player/team game statistics | Build dated histories, recent form, rest and remaining schedules |
| 2 | MoneyPuck skater, goalie and team game-level downloads | Add xG and situational advanced metrics |
| 3 | MoneyPuck season files and player biography reference | Season comparisons and identity enrichment |
| 4 | NHL EDGE tracking | Optional speed/distance/shot measurements after verifying coverage |
| 5 | MoneyPuck shot-event files | Shot-quality research and training our own xG models |

Download MoneyPuck files only from its [published data page](https://moneypuck.com/data.htm)
and retain its data dictionaries. Start with a few recent completed seasons and
the current season's game-level files, rather than all historical shots at once.
The page provides season, game-level, biography and shot datasets. MoneyPuck permits
non-commercial use with attribution and asks users to contact it for other uses;
unapproved scraping is not permitted. Keep provider attribution with derived outputs.

Verify player/game ID compatibility on samples before joining providers. Record
provider-specific IDs in mappings when needed; never match solely by player name.
Preserve strength/situation columns: an `all` row must not be summed with its 5v5,
power-play and penalty-kill components. Keep MoneyPuck xG distinct from any model
we train ourselves. EDGE is optional and missing historical measurements stay null.

## Time and training correctness

Keep game time, retrieval time and prediction cutoff as separate concepts.
Today’s download of a historical season summary contains final totals, not what
was known in November of that season. We will reconstruct historical cumulative
features from game-level records and use dated snapshots going forward. Retain
source correction/revision timestamps where available. Evaluate on later seasons
than those used for training; never use end-of-season totals as midseason inputs.

Manual imports currently create snapshots; daily scheduling has not been installed.
Timestamped files are the initial capture layer. Once database ingestion exists,
PostgreSQL becomes the canonical structured store, with Parquet as training exports.

## PostgreSQL and TimescaleDB

TimescaleDB is an extension installed into PostgreSQL. The same server can contain
ordinary relational tables and time-partitioned hypertables. Python will connect
with a normal PostgreSQL driver and SQLAlchemy. See the official
[TimescaleDB installation guide](https://www.tigerdata.com/docs/get-started/choose-your-path/install-timescaledb).

You do not need a Tiger Cloud account to self-host TimescaleDB. A managed cloud
service requires an account and service provisioning; it handles more of the
server operations. Self-hosting means we handle storage, backups and upgrades.
Local Compose, versioned schema migrations and Python snapshot loading are now
implemented; see [database setup](database.md). The JSON importer only requires a
running database when `--database` is selected.

Our recommended logical layout:

| Ordinary PostgreSQL tables | Candidate TimescaleDB hypertables |
| --- | --- |
| Players, teams, seasons, games | Player/team statistical snapshots by `observed_at` |
| Player-team membership history | Prediction history by `generated_at` |
| Canonical player/team game statistics | Shot/event observations by event time, if volume warrants |

Hypertables divide large timestamped tables into time chunks. We can join snapshots
to ordinary player/team tables in SQL. Time-based unique keys must include the
partitioning timestamp, alongside player/season/game type/source as appropriate.
The current season summary export combines teams; per-team database records will
need explicit split data, not guessed team IDs.

We need time-aware records immediately; a specialised time-series engine is not
required to store them. Ordinary PostgreSQL can handle the initial workload.
TimescaleDB is a good fit when automatic time partitioning and time-window queries
help, while preserving our relational schema. We do not need a separate InfluxDB
service for this plan.

For local setup, install a compatible PostgreSQL/TimescaleDB combination or use an
official TimescaleDB Docker image. Install the server extension first, then enable
it in our database with `CREATE EXTENSION IF NOT EXISTS timescaledb;`. A regular
PostgreSQL install does not acquire the extension simply by running that statement.
Follow the official guide for supported versions and server configuration.

Given the current space constraint on C:, place database storage on D:. Docker
Desktop also stores image layers and its VM disk separately from database bind
mounts: move/configure that storage on D: before pulling large images. Alternatively
use a remote PostgreSQL host that explicitly supports TimescaleDB. We will add
game-level/advanced dataset tables in future migrations. Identity upserts and
atomic snapshot loading are implemented; no database credentials or account are
needed for JSON-only imports.

The supplied CSV downloads have had a bounded header/row inspection; see
[downloaded-data findings](downloaded-data.md). They have not yet been imported.
