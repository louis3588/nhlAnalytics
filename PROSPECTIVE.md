# Prospective game prediction tracking

This is the forward test of the existing frozen Elo and team-context logistic
models. Neither model is retrained, tuned or promoted by this service. Existing
daily production forecasts and historical reconstructed evaluations stay intact.

## Run and monitor

Start Timescale, run the normal daily NHL refresh so game logs and schedules are
current, and start the API from the repository root:

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m nhl_analytics.api
```

The unpinned API starts both the existing GameCenter collector and the prospective
worker. Keep Docker, the API and this computer awake through the prediction
windows and games. No new paid service or account is required. Starting the API
with `--run` pins a historical archive and disables these workers by default.
`NHL_PROSPECTIVE_ENABLED=0` disables tracking explicitly. Configuration examples
are in `.env.example`; credentials remain in your ignored `.env`.

Open the dashboard normally (`cd dashboard; npm run dev`), then **Model
performance** or `/models`. The overview lists upcoming prospective forecasts;
each GameCenter page shows both models, versions, generation time, cutoff and
validated final scores/Brier contributions when available. Existing production
Elo is labelled **Archived production Elo** separately. Times use the browser's
time zone, including GMT/BST for UK users.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8000/api/prospective/performance | ConvertTo-Json -Depth 8
# A one-off recovery/report pass; safe alongside the API (one owner):
.\.venv\Scripts\python.exe -m nhl_analytics.prospective --once
# Optional standalone worker when the API is not running tracking:
.\.venv\Scripts\python.exe -m nhl_analytics.prospective
```

The standalone worker still needs the existing GameCenter collector running to
refresh upcoming schedule confirmations and final outcomes. It does not create
another NHL polling implementation. `/health.prospective` reports ownership,
last successful check, errors, next cutoff and coverage. The worker retries database
outages every 30 seconds and uses a session advisory lock, separate from the live
collector's lock, to prevent concurrent generation. Logs record campaign
registration, deferred features, seals, missing forecasts and report publication.

## Timing and tonight's launch

The cutoff is scheduled start minus 60 minutes. Normal generation occurs during
the five minutes immediately **before** that cutoff (T−65 to T−60). This avoids
claiming perfect scheduling precision on a local Windows computer. The database
uses its real clock to stamp each seal and rejects inserts at or after cutoff;
the true generation time is always visible. There is no retrospective backfill.

On 11 November 2026, all six upcoming games were registered before their cutoffs.
The first normal sealing window opens at **22:55 GMT**, for the 00:00 GMT start
on 12 November. At initial registration there were **zero sealed or evaluated
pairs**, deliberately: generation will happen in the normal window. The final
status must be checked after those windows; an offline PC cannot capture them.

An explicit one-off `--once --seal-early` bootstrap option exists for the current
NHL calendar date, but is not the default and was **not used** for this launch.
Such forecasts retain their actual earlier times, are labelled `early`, and have
separate evaluation cohorts. They cannot replace a previously sealed forecast.

Postponed/unconfirmed schedules, already-started games and missed cutoffs receive
immutable missing records with reasons. Feature failures before cutoff are logged
and retried; after cutoff the missing forecast remains missing. A changed start
time, date, schedule status or team identity creates a new fixture revision.
Older forecasts remain queryable but are excluded from scoring the new fixture;
the new revision can be sealed only if its own cutoff is still in the future.

## Features and frozen models

The first campaign registration verifies the existing artifact manifests/hashes
and embeds the frozen model bundle in the immutable campaign record. Restarts
reuse that exact bundle; changing `NHL_PROSPECTIVE_MODEL_RUN` does not silently
change an existing campaign. A different model requires an explicitly new campaign.

- Elo: the selected frozen state and parameters from
  `data/evaluations/reconstructed/20261110T210453233933Z`.
- Logistic: saved `models/20262027_logistic_team_context.json` in
  `data/team_models/reconstructed/20261111T150925233807Z`. Its fit excludes the
  reserved 2025–26 benchmark. Inference replays its imputer, missing indicators,
  scaler and coefficients with ordinary Python; sklearn is not loaded by the worker.
- Raw feature inputs: actual stored schedule and player-game response observations
  strictly earlier than the feature snapshot, across the current and prior season.
  Target-game records, live player statistics and future observations are excluded.
  The upcoming schedule must have been confirmed within 15 minutes of the snapshot.
- Source IDs, selected game IDs, coverage counts, availability maximum, feature
  values/hash and feature implementation hash are persisted with each pair.
  Missing or incomplete known-final skater/goalie lines defer generation; they are
  not filled with retrospectively reconstructed raw statistics.

Historical training and Elo state have **reconstructed training provenance**.
This is declared model lineage, not a claim that historical raw features were
observed at their old game times. The frozen rating seed and saved historical
opponent ratings are model artifacts known before registration. Elo subsequently
updates only when new final schedule observations have actually become available.
Both models use the same feature snapshot. No historical observation timestamps
are rewritten, and no next-day availability assumptions enter the observed raw
feature path.

The normal daily importer remains responsible for refreshing final game logs and
schedule observations. GameCenter schedule checks keep target fixtures current,
but do not substitute provisional live totals into training features. If daily
imports stop, new final games without complete observed logs prevent forecasts
instead of silently omitting those games. This intentionally favors correctness
over coverage.

## Database and result corrections

Migration `009_prospective.sql` adds ordinary PostgreSQL tables inside Timescale:

| Table | Purpose |
| --- | --- |
| `prospective_campaigns` | Frozen model bundle, hashes, registration and policy |
| `prospective_fixtures` | Append-only schedule identities and their cutoffs |
| `prospective_fixture_heads` | Mutable pointer to the current schedule revision |
| `prospective_batches` | One atomic pair, or a missing record, per campaign/fixture |
| `prospective_attempts` | Append-only failed/deferred attempts |
| `prospective_result_samples` | Preliminary, conflicting and final provider checks |
| `prospective_validated_results` | Append-only stable final outcomes and corrections |

Ledger updates, deletes and truncation are rejected by triggers. The batch's
unique key and atomic insertion ensure a crash cannot save only one model.
As with any application-level ledger, a database administrator able to disable
triggers remains trusted; this is not cryptographic tamper-proof storage.

The existing shared collector supplies both raw box scores and play-by-play.
Final validation requires matching NHL IDs, teams, season, start time and score,
both feeds in FINAL/OFF state, matching period descriptors and a non-tied result.
Regulation requires an expired clock; sudden-death overtime and shootout finals
require a stopped clock and a one-goal margin (remaining overtime time is normal).
Two consecutive matching observations must span at least ten minutes. Live scores
are never scored as final. A subsequent conflicting observation suspends scoring;
a stable correction appends another result revision. Original probabilities stay
unchanged, while derived reports reflect the newest validated outcome.

After an outage, older unresolved sealed games are queued in bounded batches
through the existing collector's request mechanism. Current/recent games continue
using its normal final polling. Corrections outside that collector's recent window
are processed when that game is requested again through GameCenter; the worker
does not continuously re-download every past game of the season.

## Evaluation and API

Only sealed pairs with a matching current fixture and validated final result enter
scoring. Both models always use identical games. Reports include Brier score,
log loss, favourite accuracy, season/month breakdowns, reliability bins, daily
scores by NHL game date, cumulative curves and logistic-minus-Elo paired Brier
differences. Early and standard cohorts are filterable separately.

The exploratory 95% paired-difference interval uses a deterministic 1,000-replicate
calendar-week block bootstrap, preserving within-week game dependence. It is
withheld until at least 100 games across ten weeks. It remains conditional on
coverage and the frozen models; it does not establish general superiority.
Pending, missing and temporarily unscored forecasts have explicit coverage counts.
An empty prospective cohort has unavailable scores, not synthetic performance.

| GET endpoint | Contents |
| --- | --- |
| `/api/prospective/games` | Cached current fixture forecasts; season/team filters, `upcoming=true`, pagination |
| `/api/prospective/games/{game_id}` | Both models, status, outcome and retained schedule revisions |
| `/api/prospective/performance` | Cached cumulative, calibration, daily and monthly results plus separate historical references |

Requests read the worker's cache; they do not fit a model, generate a forecast,
read the full database or contact NHL. React polls this cache every 30 seconds;
existing live-game SSE remains independent and keeps scores/player stats current.
Reports are appended under `data/prospective/reports/<campaign>/<UTC timestamp>/`
with JSON, CSV and Markdown plus an atomic `latest.json` pointer. They are
reproducible derivatives, not the authoritative immutable forecast store. Back up
the Timescale volume to preserve the ledger permanently. Existing live snapshot
retention does not delete these ordinary ledger tables.

## Validation

```powershell
$env:NHL_DB_TESTS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd dashboard
npm test
npm run build
# With preview/API running (4173 is the default preview port):
$env:DASHBOARD_URL='http://127.0.0.1:4173'
node tests/prospective.mjs
npm run test:live
```

Database tests use rollback transactions; synthetic forecasts/results are never
published into the real campaign. Tests cover cutoff boundaries, late/backdated
rejection, immutable/idempotent seals, reschedules, actual feature availability,
target exclusion, frozen numeric inference equivalence, final agreement and
correction revisions, paired scoring and cached API responses. Browser fixtures
exercise sealed/completed/missing states and unchanged probabilities during live
SSE updates. Real prospective outcome performance can only be assessed after the
newly sealed games actually finish.
