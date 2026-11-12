# Pregame team features and logistic regression

The first comparison uses existing frozen NHL history; no new seasons or
MoneyPuck shot archives were downloaded/scanned. Production Elo, daily prediction
archives, strict feature exports and the frontend remain unchanged.

## Completed comparison

The reusable dataset contains **6,845 game rows** and **22 numeric features**:
2021–22 through 2025–26, plus the 285 eligible 2026–27 games in the frozen source.
The original source clock is **10 November 2026, 19:35:19 UTC**. Later daily/live
imports are intentionally excluded so comparisons use the same frozen inputs.
The first season supplies training/warmup; 5,533 games are scored across the
remaining seasons. The 2026–27 portion is partial reconstructed monitoring,
not prospectively sealed future predictions.

| Completed evaluation season | Frozen Elo Brier ↓ | Team-context logistic Brier ↓ |
| --- | ---: | ---: |
| 2022–23 | 0.23429 | 0.23197 |
| 2023–24 | 0.23490 | 0.23636 |
| 2024–25 | 0.23832 | 0.23532 |
| 2025–26 reserved benchmark | 0.24857 | 0.24691 |

For the 1,312-game 2025–26 benchmark, log loss improves from **0.69047 to
0.68690** and accuracy from **52.7% to 53.4%**. The paired Brier difference is
−0.00167, with an exploratory calendar-week bootstrap interval of
**[−0.00509, +0.00192]**. It includes zero: this is a modest candidate improvement,
not convincing proof of superiority. Performance is not better every season.

A second logistic model uses only Elo log odds to separate recalibration from
the richer features. Its benchmark Brier is 0.24922, worse than frozen Elo;
the team-context result is not simply a successful rescaling of Elo. The current
partial-season result is also reported, but must not drive model selection.

Actual artifacts on this workspace:

- Dataset: `data/team_models/reconstructed/20261111T150506479124Z/dataset/`
- Comparison: `data/team_models/reconstructed/20261111T150925233807Z/`
- Readable findings: the comparison directory's `report.md`.

## Method and time policy

This is explicitly **`reconstructed_latest_corrected`** history, matching our
existing baseline. Historical data was collected retrospectively, so actual
historical publication times cannot be verified. We retain the existing
assumption that results/player stats become available at noon America/New_York
on the following NHL game date. We do not backdate real observation timestamps
or relabel these exports as strict point-in-time observations.

Each row's prediction cutoff is **one hour before scheduled puck drop**, matching
the saved frozen Elo forecasts exactly. Only games with that cutoff at or before
the source clock enter this evaluation dataset. New predictions for distant
future fixtures are not mixed into the evaluation cohort.

Feature construction sweeps forward through cutoffs. Past results are released
from a pending queue only when their assumed availability is **strictly less
than** the cutoff. Equality is excluded. A target's own result is enqueued after
its feature row is generated. Outcomes live in separate label files.

Schedule-based rest uses earlier scheduled starts, even if the previous game's
result is not published yet. Latest corrected historical schedules remain a
reconstruction limitation: we cannot establish that an old postponement was
known at every historical cutoff. Rest is capped at 14 days; preseason workload
and injuries are not reconstructed.

Features include:

- Frozen Elo log odds, read directly from the verified saved predictions.
- Home-minus-away season and last-ten scoring, shot and save-rate measures.
- Last-ten win rate and mean opponent pregame rating.
- Previous-season scoring/shot/save rates, separately from current form.
- Rest difference, home/away back-to-backs and last-seven-day game counts.
- Each side's available current/prior-season game counts.

Rate statistics cover all NHL game situations. The shootout deciding goal is
removed from scoring-rate numerators but still determines the binary winner.
Team save percentage pools past goalies' saves and shots against; the actual
target-game starting goalie is never selected from its box score. Missing stats
stay missing, rather than becoming zero. Last-ten windows reset each season;
separate prior rates and Elo provide carryover. Existing Arizona/Utah franchise
continuity is preserved while raw NHL team IDs remain in output rows.

No target outcomes, actual target starters/lineups, current injuries/rosters,
live stats, target-season final standings or retrospectively substituted season
totals enter the model. xG and special-teams splits are deferred until historical
coverage and timing can be audited; this first test uses the complete NHL core.

## Training and evaluation

The experiment plan is written before scoring. Both logistic models use fixed
**L2 regularisation, C=1, lbfgs, max_iter=1000, tol=1e-7**. There is no grid search,
feature selection, threshold tuning, class balancing or post-hoc calibration.
Missing-value imputation, missing indicators, scaling and coefficients are fitted
only on the training fold, following the
[scikit-learn pipeline approach](https://scikit-learn.org/stable/modules/generated/sklearn.pipeline.Pipeline).

Training expands chronologically:

| Evaluation | Training |
| --- | --- |
| 2022–23 | 2021–22 |
| 2023–24 | 2021–22 through 2022–23 |
| 2024–25 | 2021–22 through 2023–24 |
| 2025–26 | 2021–22 through 2024–25 |
| Partial 2026–27 | Same non-holdout training as 2025–26 |

Every training label must predate the evaluation season's **first** cutoff.
2025–26 labels are excluded from every fit, including the partial later season.
Earlier game results may update later feature state, as they would operationally.
The same training cohort is not fitted repeatedly when a later fold reuses it.

The Elo K/home/carryover parameters are never retuned. They were previously
selected using 2022–23 through 2024–25, so those development comparisons are
retrospective and not independent evaluations of Elo parameter selection.
2025–26 was previously inspected and is not called pristine unseen data.

All models use identical test games. Reports include Brier, log loss, accuracy,
monthly breakdowns, fixed-bin reliability tables and paired differences. The
bootstrap resamples paired calendar-week blocks 1,000 times with a fixed seed.
Intervals are conditional/exploratory: they omit training uncertainty and cannot
fully model cross-week/team dependence. Fewer than ten weeks yields no interval.
No winner is automatically deployed. The next genuine test should seal forecasts
before future games begin.

## Reuse and commands

From the project root, keep temporary files/cache on D: and numerical threads at
one. The optional `models` dependency was installed and checked successfully.

```powershell
$env:TEMP = Join-Path $PWD '.work/tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $PWD '.work/pip-cache'
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
.\.venv\Scripts\python.exe -m pip install --no-compile -e '.[models]'
```

To train/reproduce a new comparison using the already completed dataset:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.team_models --dataset data/team_models/reconstructed/20261111T150506479124Z/dataset
```

To build a new dataset and compare in one command, or export only:

```powershell
.\.venv\Scripts\python.exe -m nhl_analytics.team_models
.\.venv\Scripts\python.exe -m nhl_analytics.team_models --features-only
```

The installed entry point is `nhl-team-models`. `--evaluation PATH` selects an
explicit frozen evaluation; the default finds the newest complete evaluation.
`--output PATH` changes the root. Every invocation creates a new timestamped
directory and refuses to overwrite a nonempty experiment. Dataset reuse verifies
its CSV/Parquet hashes and original source hashes before training.

Player Parquet files are streamed in 2,000-row batches into team-game aggregates;
the full 246,350-row skater table is not materialised as Python dictionaries.
Models operate on a few thousand rows, with numerical threads capped at one.
Portable JSON saves imputer/scaler parameters and coefficients, avoiding pickle.
Saved predictions are checked against the fitted sklearn pipeline to 1e-12.

Artifacts include:

- `dataset/pregame_features.csv` and `.parquet`: features and timing/ID audit fields.
- `dataset/game_labels.csv` and `.parquet`: separate outcomes and assumed availability.
- Dataset manifest: source hashes, missing-value counts, provenance and coverage.
- `experiment_plan.json`: fixed features/settings and implementation hashes.
- `folds.json`: training seasons/counts, cutoff and latest eligible label time.
- `predictions.csv`: paired per-game probabilities and evaluation labels.
- `metrics.csv/json`, `monthly_metrics.csv`, `calibration.csv` and paired comparison tables.
- `coefficients.csv` and `models/*.json`: portable fitted models, versions and coefficient diagnostics.
- `report.md` and final manifest: findings, limitations and artifact hashes.

For reused datasets, the manifest points to their original location rather than
copying/rebuilding them. These artifacts live outside strict feature exports,
production serving caches and original evaluation directories.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_team_models.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
```

New tests cover target/future-outcome invariance, cutoff equality, rest before
result publication, shootout handling, franchise continuity, label timing,
holdout label/feature mutation invariance for every fitted artifact, training-only
preprocessing, portable prediction parity, paired uncertainty and source hash
validation. No database migrations, frontend changes, imports or model promotion
are required for this experiment.
