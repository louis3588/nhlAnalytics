# Automatic public snapshot publishing

Publishing is enabled for **https://nhl-rinkside-beta.pages.dev/** using the
current Windows user's existing Wrangler login. No paid service, hosted engine,
model training or database migration is introduced.

## Triggers and execution

- Completed daily refreshes enqueue the saved prediction-bundle hash. Re-running
  a completed refresh repairs queueing without repeating any imports.
- Pregame seals enqueue their immutable batch ID. Reconciliation also captures
  missing-forecast records so public coverage stays honest.
- Validated final outcomes and corrections enqueue their result IDs, refreshing
  model evaluation. Provisional live scores never trigger a publication.

The publisher reconciles IDs from the database and daily pointer on every pass.
A crash between sealing and enqueueing does not lose an update. It reads small
identifier tables, not the MoneyPuck archive.

The unpinned API polls the outbox every 30 seconds. **NHL Analytics Public
Publisher**, a Windows task every five minutes, provides recovery when the API is
stopped. One shared file lock and SQLite outbox under ignored
`data/publication_automation/` prevent concurrent exports/uploads.

Events batch for 60 quiet seconds, with at most 120 seconds of batching. Uploads
are at least ten minutes apart. Normal delay is batching plus export/package/
upload time; resource waits, cooldown or failures can extend it. Publication never
generates, reconstructs or backdates a missed pregame forecast.

## Data-only deployment

Each job exports validated presentation data and packages it with a **pinned copy
of the tested static frontend**. Ordinary working-tree React edits are not
automatically compiled and released. Automated player history is capped at **16
recent saved dates** to bound growth; complete histories remain local. Original
timestamps, source modes, intervals, model versions and sealed probabilities are
preserved. Forecasts, training files and database records are never pruned.

Generated jobs live in `.work/public-publisher/<job ID>/site`, separately from the
manual `dashboard/dist-public` review build. Previously verified releases are
retained for open tabs. Generated job/cache retention is bounded and confined to
checked task directories. Export/packaging waits while daily ingestion holds its
lock, and low Windows memory/paging capacity defers heavy work.

Before upload, the worker verifies the existing account and `pages:write` scope.
It runs only `pages deploy` for the configured project and `main` branch. It does
not create projects, use `--force`, open login pages, migrate to Workers or upgrade
a plan. Hosted verification checks HTML/JS/data hashes, security/cache headers
and deep-link URLs. Already hosted content is acknowledged without another upload.

An uncertain upload is reconciled before retrying. Persisted leases prevent an
immediate second upload if an earlier child might still be running. Only captured
events are acknowledged; arrivals during upload remain queued. Failures back off
from five minutes to at most an hour and leave the existing website intact.

## Free-tier safeguards

Local caps are **12 upload attempts per UTC day and 360 per UTC month**, including
failed/uncertain attempts. Queue entries wait when caps are reached; none are
dropped and no paid tier is enabled. Manual deployments are outside these local
counters, so leave headroom for other account activity.

These are conservative application limits, not a claim that Wrangler uploads and
Cloudflare's Git-build allowance are identical. Cloudflare currently documents
free unlimited static requests, 500 Free Git builds/month, 20,000 site files and
25 MiB per asset. Packaging checks the file/count limits. Sources:
[pricing](https://developers.cloudflare.com/pages/functions/pricing/),
[limits](https://developers.cloudflare.com/pages/platform/limits/).

Keep the PC awake and signed in as the same Windows user, with Docker/Timescale
available. The API/collector must run to create new seals and validated finals.
The fallback can publish recorded updates without FastAPI but cannot manufacture
missed predictions. The public site remains online with its last snapshot when
the PC is off; generation timestamps do not change to disguise stale data.

## Monitor and recover

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --status
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 7
Get-ScheduledTaskInfo -TaskName 'NHL Analytics Public Publisher'
# One recovery pass; safe alongside the API:
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --once
# Skip batching only, never budgets, cooldown or leases:
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --once --flush
```

`/health.public_publishing` shows the current stage, pending events, counters,
retry time and last verified release. It follows the scheduled worker's persisted
status when another process owns the job. Stored times are UTC Unix seconds.
Stages include `idle`, `batching`, `exporting`, `packaging`, `uploading`,
`verifying`, `published`, `unchanged`, `cooldown`, `resource_wait`, `budget_wait`,
`recovering`, `retry_wait` and `auth_required`.

Logs are in `.work/logs/public-publisher/` and existing API logs. Health errors
report exception class/phase, not provider response bodies or credentials.
Wrangler log sanitization is enabled. Ignored
`data/publication_automation/config.json` stores the non-secret account ID,
project, hostname, enabled flag and history limit. OAuth tokens stay in Wrangler's
Windows user profile, outside the repository and uploaded package.

Set `enabled` to `false` in that config to stop future uploads. An in-flight
upload may finish. Re-enable it to resume the queue. Malformed config fails closed
without taking down the API/collector. `auth_required` needs a deliberate Wrangler
browser login under the same user; background jobs never prompt for credentials.
For stale data check Docker, daily completion, pending/retry/resource/budget
status and last success. Do not rerun imports solely because an upload failed or
delete the outbox to bypass counters.

## Releasing frontend changes

After intentionally building and testing a new public frontend:

```powershell
cd dashboard
npm run build:public
npm run preview:public
# Run the public browser checks against that preview, then return to repo root:
cd ..
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --pin-frontend
```

Pinning stores immutable reviewed UI files and queues their identity. It rejects
API/SSE-mode builds and excludes snapshot data from the UI template. Data jobs
keep using the earlier pin until this step. Avoid concurrent unrelated manual
Wrangler uploads. For an automatic artifact use the last-success job ID and its
`.work/public-publisher/<ID>/site` directory; manual `dist-public` may be older.

The fallback task is installed by
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-public-publish.ps1`.
It uses limited privilege, interactive-user credentials, StartWhenAvailable,
IgnoreNew overlap handling and a 20-minute limit. No token is embedded in its
action. Unit tests cover deduplication, restarts, ambiguous uploads, leases,
batching, in-flight events, budgets, cooldown, resource/auth/config/target failures,
ingestion exclusion, completed-refresh recovery and frontend pinning:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_public_refresh.py -v
```


## Recovery after the 12 November outage

The daily refresh stopped on NHL feed timeouts. Resume it with
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/daily-refresh.ps1 --workers 1`.
The day's manifest skips completed stages; player game logs also resume their saved plan.
Do not delete manifests or replay historical imports to fix a publishing error.

Wrangler OAuth expired and could not refresh. Background jobs never open login pages.
Under the same Windows user, run the project-local login, then verify access:

```powershell
node dashboard/node_modules/wrangler/bin/wrangler.js login
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --check-auth
.\.venv\Scripts\python.exe -m nhl_analytics.public_refresh --once --flush
```

`--check-auth` checks the existing account, Pages scope and project under the publisher
lock, then clears retry backoff only on success. It does not upload or change budgets,
leases or forecasts. Publishing health retains a safe failure reason across retries.
Repeated OAuth failures need interactive recovery; unattended OAuth renewal is not guaranteed.

## API supervision independent of PyCharm

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-api-service.ps1
Get-ScheduledTaskInfo -TaskName 'NHL Analytics API'
Invoke-RestMethod http://127.0.0.1:8000/health
```

The limited-privilege interactive-user task starts at sign-in and is checked every minute.
Its supervisor restarts an exited API after 30 seconds, with a mutex and port check
preventing overlapping launches. Task Scheduler owns it independently of the IDE.
Keep Windows signed in, the PC awake and Docker/Timescale running. This is process
recovery, not a guarantee against upstream outages or a hung process. Logs are under
`.work/logs/api-service/`; temporary files stay on D:. The existing daily refresh and
publisher tasks remain separate. Stopping the supervisor is not a supported way to
stop its separately launched child; stop the exact API process too when doing maintenance.

The shared collector prioritises schedule checks during T-75 to T-60 and isolates
individual GameCenter connection/timeouts so one game does not stall all others.
Provider rate limits still apply globally. Forecast cutoff and source freshness rules
are unchanged; missed forecasts are never generated retrospectively.


### Modern Standby and screen locking

On 12 November, Kernel-Power events 506/507 recorded idle-triggered Modern Standby
from 01:02 until keyboard wake at 14:59. A powered-on laptop with a dark screen is
not necessarily executing ordinary background processes continuously.

The API CLI now holds scoped Windows `PowerRequestSystemRequired` and
`PowerRequestExecutionRequired` requests for its lifetime. The display can still
turn off and Windows can remain locked. Requests are released when the process
exits, including crashes; no global power plan is modified. Set `NHL_KEEP_AWAKE=0`
in the launch environment to opt out. Failed power requests log a warning and do
not prevent the API from starting.

Keep the laptop plugged in and the lid open. Windows can override requests on
battery or for explicit Sleep/lid-close/power-button actions. This is not a claim
that every laptop/firmware combination will stay awake overnight; check the next
morning's collector timestamps and power events.
Microsoft reference: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest

Recovery completed on 12 November: all five refresh stages finished, a new daily
forecast was archived at 17:06 UTC, and public release
`0ce5e3f30e9ba0fd6564fe430a17093722f940bfb9feb5dfcf25234c983b95ff`
was remotely verified at about 17:09 UTC. No historical imports or model fitting
were repeated, and the publication queue drained to zero.
