# Live GameCenter games

Open **Live & recent games** in the dashboard, or **Open game center** on a
forecast card. Reloadable `/games/{nhl_id}` pages show team colours/logos, local
start time, score and provider clock, team comparisons, skater/goalie tables and
corrected play-by-play. Skater names link to player projections. Saved Elo odds
are explicitly pregame; they do not change with the live score.

## Run

Keep Docker Desktop/Timescale healthy. From the Python project root:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.api
```

From another terminal:

```powershell
cd dashboard
npm.cmd run dev
```

The unpinned API starts collection in its background lifespan. There is no paid
data account, new npm dependency, public webhook URL or Redis requirement.
Existing ignored `.env` database credentials are reused. The PC/API must stay
running during games; the daily Windows task does not restart the API after a
reboot. Start Docker before the API for immediate saved-state recovery.

Optional `.env` settings:

```dotenv
NHL_LIVE_ENABLED=1
NHL_LIVE_POLL_SECONDS=20
```

Set `NHL_LIVE_ENABLED=0` for saved forecasts only. Pinned historical benchmark
instances (`--run`/`NHL_PREDICTIONS_RUN`) default to live disabled. Source polling
is clamped to at least 15 seconds. Temporary/cache/test files remain on D:.

## Collector and sources

The verified unauthenticated GameCenter feeds are:

- `https://api-web.nhle.com/v1/score/{YYYY-MM-DD}`
- `https://api-web.nhle.com/v1/gamecenter/{gameId}/boxscore`
- `https://api-web.nhle.com/v1/gamecenter/{gameId}/play-by-play`

This is near-live polling, not guaranteed provider push. Feeds can change or be
delayed; scoreboard and player stats may briefly reflect different update instants.
EDGE tracking remains a separate daily source. The provider clock is displayed
as received, without inventing a continuously ticking countdown.

The collector discovers current and preceding **America/New_York** dates every
minute so UK midnight does not drop an ongoing game. `LIVE`/`CRIT`/`PRE` games
poll about every 20 seconds; future games near puck drop every minute, other
future games every five minutes. Finals slow to 15 minutes, then an hour once
their start is over eight hours old. Manual admissions expire after six hours.
Preseason, regular-season and playoff IDs are supported; prediction availability
still depends on the existing models.

A session-level PostgreSQL advisory lock elects one collector. A second API or
standalone process follows saved database updates instead of fetching NHL again;
manual admissions are shared in the database. The development API still uses one
worker. No browser independently polls GameCenter. Provider calls are sequential,
with at most two game pairs per tick, request pacing, connect/read timeouts,
exponential failure backoff and HTTP 429 `Retry-After` handling. Fresh saved games
seed the cache after restart rather than immediately refetching completed work.
Caches hold at most 64 games, admission queues 16 IDs and SSE subscribers 64 per
API process. Slow viewers do not create an unbounded update queue.

Full observations are saved **before** publication to viewers. If the database
is unavailable, the collector does not fetch unpersistable source updates; last
memory-cached states remain visible with a warning. Mismatched games, duplicate
event IDs, missing play arrays and an empty midgame reset after events existed
are rejected. Valid removals are recorded as tombstones. Changed scorers/assists
create revisions, not duplicate events. Missing pregame stats remain unavailable,
not zero.

## Timescale tables and retention

| Table | Purpose |
| --- | --- |
| `analytics.live_games` | Latest complete raw/normalized envelope, content hash, content-observed and last-checked times. |
| `analytics.live_game_snapshots` | Changed envelopes in a Timescale hypertable. |
| `analytics.live_game_events` | Current `(game_id,event_id)` event, NHL actor ID and deletion/first-seen/last-seen fields. |
| `analytics.live_game_event_revisions` | Durable corrected/deleted/reinstated observations in a hypertable. |
| `analytics.live_game_requests` | Shared expiring game admissions. |

Migration `007_live_games.sql` adds these tables. `008_live_preseason.sql` permits
preseason shells in `analytics.games`; existing training/export queries retain
their explicit regular-season/playoff filters. Games and event actors link to
canonical NHL IDs. Only missing identity/game shells are inserted. Provisional
scores do not overwrite final game logs or schedule observations. Daily imports
continue to provide final validated data. Live rows do not silently enter strict
point-in-time or reconstructed training exports, and no models retrain here.

Occurrence period/clock stays separate from actual observation time. Content
hashes deduplicate unchanged responses/versions; last-check/last-seen times can
advance without duplicate history. Raw box-score/play-by-play responses are kept
in saved envelopes.

**Raw changed snapshots have a 30-day Timescale retention policy.** Latest/final
complete game states and event revision history are retained independently.
Daily prediction archives and older player forecasts are unaffected. Review raw
snapshot retention separately if a future model needs every intermediate box
score beyond 30 days.

## HTTP and streaming

| Endpoint | Result |
| --- | --- |
| `/api/live/games` | Shared directory and collector health. |
| `/api/live/stream` | Directory Server-Sent Events. |
| `/api/live/games/{id}` | Cached game; queues an admission if missing. |
| `/api/live/games/{id}/stream` | Full game-state Server-Sent Events. |
| `/health` | Existing prediction health plus live status, subscriber/cache counts and upstream request count. |

An initial `status: loading` means collection is queued. Unknown provider IDs
become 404/not-found, malformed IDs return 422, full admission queues 429, and a
disabled/full streaming service 503. Live request handlers do not fetch NHL or
connect to Timescale themselves.

Streams send `event: game` full replacements with content-based IDs, a five-second
retry hint and independent ten-second keepalives. Reconnect sends the latest full
state, not a replay of deltas. React replaces arrays/totals rather than adding
them repeatedly, keeps the last score during reconnects, and distinguishes a
transport interruption from stale provider data. Disconnects release streams.

Vite proxies streams already. A future production proxy must disable SSE
buffering and allow long-lived responses. The API supplies
`X-Accel-Buffering: no` and `Cache-Control: no-cache, no-transform`. Retain the
existing CORS configuration if frontend and API origins differ.

## Tests and troubleshooting

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.live --once --game 2026020286
$env:NHL_DB_TESTS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_live.py
Remove-Item Env:NHL_DB_TESTS
cd dashboard
npm.cmd test
npm.cmd run test:live
npm.cmd run build
```

Timescale integration tests use synthetic IDs in forced-rollback transactions,
covering deduplication, corrections/removals/reinstatements, rollback safety,
observation ordering, empty feeds and preseason linking. Unit/HTTP/SSE tests cover
backoff, shared polling, queues, disabled-service isolation and reconnect cleanup.
Browser checks use a real persisted final plus controlled in-browser fixtures
for updates, corrections, stale recovery and pregame states. No synthetic events
are inserted into real games. There were no active games during daytime
verification; actual in-play provider transitions were not claimed from it.

Check `/health` and the API logs if a page stalls. `degraded` means check Docker
or connectivity. `following` means another process owns the collector lock; zero
upstream requests from a standalone diagnostic in this state is expected. The
primary API collector will handle its queued admission. Stop the API first if
you want the standalone command to become the source collector instead.
The development API PID is in `.work/logs/api-server.pid` and logs under
`.work/logs/`. The collector stops with that API process.
