"""Append observed daily predictions, preserving frozen reconstructed benchmarks."""

import gzip
import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from nhl_analytics.asof import past_final, read_games, read_player_games
from nhl_analytics.baselines import PARAMETERS, elo_predictions, player_projection
from nhl_analytics.database import connect, timestamp
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import PredictionStore, checksum, discover_run, document, instant, verify
from nhl_analytics.reconstructed import assumed_available
from nhl_analytics.uncertainty import MODELS, NOMINALS, TARGETS, interval

ROOT = Path('data/predictions/daily')
MODE = 'observed_daily'


def frozen_pools(run, season, cutoff):
    manifest = document(run / 'manifest.json')
    path = run / 'interval_calibration.json'
    verify(path, manifest['files']['interval_calibration.json']['sha256'])
    data = document(path)
    if data['holdout_excluded'] != 20252026:
        raise ValueError('Expected frozen calibration excluding the 2025–26 benchmark')
    pools = {}
    for fit in data['fits']:
        if fit['evaluation_season'] != season:
            continue
        latest = timestamp(fit['max_calibration_label_available_at'])
        if latest >= cutoff or any(s >= season or s == 20252026 for s in fit['calibration_seasons']):
            raise ValueError('Calibration includes unavailable or holdout labels')
        pools[fit['model'], fit['target'], fit['remaining_bucket'], fit['prior_records']] = dict(
            n_players=fit['unique_calibration_players'], eligible=fit['eligible'],
            radii={0.8: fit['radius80'], 0.95: fit['radius95']}, seasons=fit['calibration_seasons'], latest=latest)
    return pools


def player_rows(records, games, season, cutoff, pools):
    groups = defaultdict(list)
    for row in records:
        if row['kind'] == 'skater':
            groups[row['season_id'], row['player_id']].append(row)
    played, remaining = Counter(), Counter()
    for game in games.values():
        if game['season_id'] != season:
            continue
        (played if past_final(game, cutoff) else remaining).update((game['home_team_id'], game['away_team_id']))
    inputs, projections, bands = [], [], []
    prior_season = (season // 10000 - 1) * 10000 + season // 10000
    for (row_season, player), rows in sorted(groups.items()):
        if row_season != season or len(rows) < PARAMETERS['min_player_games']:
            continue
        rows = sorted(rows, key=lambda r: (r['game_date'], r['game_id']))
        prior = groups.get((prior_season, player), [])
        team = rows[-1]['team_id']
        current_payloads = [r['payload'] for r in rows]
        prior_payloads = [r['payload'] for r in prior]
        totals = {t: sum(r[t] for r in current_payloads) for t in ('goals', 'assists', 'points')}
        if totals['goals'] + totals['assists'] != totals['points']:
            raise ValueError('Recorded scoring totals disagree')
        available = max(r['available_at'] for r in rows + prior)
        if available >= cutoff:
            raise ValueError('Daily source is not available before prediction date')
        row = dict(player_id=player, season_id=season, prediction_at=instant(cutoff), data_mode=MODE,
            games_played=len(rows), **totals, last_played_team_id=team, team_games_remaining=remaining[team],
            prior_games=len(prior), max_assumed_available_at=instant(available))
        prediction = player_projection(current_payloads, prior_payloads, max(played[team], len(rows)), remaining[team], PARAMETERS['prior_pseudo_games'])
        inputs.append(row)
        projections.append(dict(player_id=player, **prediction))
        for model in MODELS:
            for target in TARGETS:
                observed = row['games_played' if target == 'games' else target]
                point = prediction['projected_games' if target == 'games' else f'{model}_{target}']
                for nominal in NOMINALS:
                    bands.append(dict(player_id=player, model=model, target=target, nominal=nominal, observed=observed,
                        prediction=point, **interval(row, prediction, target, model, nominal, pools)))
    if not inputs:
        raise ValueError('No eligible players; refuse to publish an empty daily forecast')
    return inputs, projections, bands


def collect_payload(connection, run, season, cutoff):
    previous = (season // 10000 - 1) * 10000 + season // 10000
    games = read_games(connection, cutoff, [previous, season], 2)
    records, sources = read_player_games(connection, cutoff, [previous, season], 2, games)
    current = [g for g in games.values() if g['season_id'] == season]
    if not current or any(not g['start_time_utc'] for g in current):
        raise ValueError('Missing current regular-season schedule or start times')
    inputs, projections, bands = player_rows(records, games, season, cutoff, frozen_pools(run, season, cutoff))
    baseline = document(Path(document(run / 'manifest.json')['original_baseline']) / 'manifest.json')
    history = Path(baseline['history'])
    verify(history / 'games.parquet', document(history / 'manifest.json')['artifacts']['games']['sha256'])
    historical = [g for batch in pq.ParquetFile(history / 'games.parquet').iter_batches(batch_size=1000)
        for g in batch.to_pylist() if g['season_id'] < season]
    replay = []
    for game in current:
        item = dict(game, game_date=date.fromisoformat(game['game_date']), start_time_utc=timestamp(game['start_time_utc']))
        # Rating replay uses historical next-day assumptions where observations
        # did not exist; newly observed finals may be used immediately today.
        item['assumed_available_at'] = min(assumed_available(item['game_date']), game['available_at'])
        replay.append(item)
    parameters = document(run / 'selected_elo_model.json')['parameters']
    elo, _ = elo_predictions(historical + replay, cutoff, parameters)
    upcoming = {g['game_id'] for g in replay if not g['is_final'] and g['start_time_utc'] >= cutoff}
    return dict(version=1, day=cutoff.date().isoformat(), season=season, prediction_at=instant(cutoff), data_mode=MODE,
        source_policy='Current/previous-season game logs and schedules observed strictly before cutoff; frozen historical residual calibration; reconstructed historical Elo replay.',
        inputs=inputs, projections=projections, intervals=bands, fixtures=current,
        games=[r for r in elo if r['game_id'] in upcoming], source_diagnostics=sources)


def apply_payload(connection, payload):
    season, cutoff = payload['season'], payload['prediction_at']
    if len(payload['intervals']) != len(payload['inputs']) * 16 or len(payload['projections']) != len(payload['inputs']):
        raise ValueError('Incomplete daily projection or interval coverage')
    if connection.execute('SELECT 1 FROM prediction_origins WHERE season=? AND prediction_at=?', (season, cutoff)).fetchone():
        raise ValueError('Daily prediction timestamp already exists; refusing to overwrite')
    connection.execute('INSERT INTO prediction_origins VALUES (?,?,?)', (season, cutoff, MODE))
    for row in payload['inputs']:
        if instant(row['max_assumed_available_at']) >= cutoff:
            raise ValueError('Source availability reaches daily prediction timestamp')
        connection.execute('INSERT INTO inputs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (season, cutoff, row['player_id'], row['last_played_team_id'],
            row['games_played'], row['goals'], row['assists'], row['points'], row['team_games_remaining'], row['prior_games'], row['max_assumed_available_at']))
    for row in payload['projections']:
        for model in MODELS:
            connection.execute('INSERT INTO projections VALUES (?,?,?,?,?,?,?,?)', (season, cutoff, model, row['player_id'],
                row['projected_games'], *(row[f'{model}_{t}'] for t in ('goals', 'assists', 'points'))))
    for row in payload['intervals']:
        available = row['max_calibration_label_available_at']
        if available and instant(available) >= cutoff:
            raise ValueError('Calibration availability reaches daily prediction timestamp')
        connection.execute('INSERT INTO intervals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (season, cutoff, row['model'], row['player_id'],
            row['target'], round(row['nominal'] * 100), row['observed'], row['prediction'], int(row['available']), row['lower'], row['upper'],
            row['calibration_players'], row['calibration_seasons'], instant(available) if available else None, row['calibration_scope'], row['radius']))
    for game in payload['fixtures']:
        connection.execute('''INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(game) DO UPDATE SET
            game_date=excluded.game_date,start=excluded.start,home=excluded.home,away=excluded.away,
            home_abbrev=excluded.home_abbrev,away_abbrev=excluded.away_abbrev,is_final=excluded.is_final''',
            (game['game_id'], season, game['game_date'], instant(game['start_time_utc']), game['home_team_id'], game['away_team_id'],
             game['home_team_abbrev'], game['away_team_abbrev'], int(game['is_final'])))
    for row in payload['games']:
        connection.execute('''INSERT INTO games VALUES (?,?,?,?,?,?,?) ON CONFLICT(game) DO UPDATE SET
            prediction_at=excluded.prediction_at,home_rating=excluded.home_rating,away_rating=excluded.away_rating,
            home_probability=excluded.home_probability,comparator=excluded.comparator,available_at=excluded.available_at''',
            (row['game_id'], cutoff, row['home_rating'], row['away_rating'], row['home_win_probability'],
             row['historical_home_win_probability'], instant(row['max_assumed_available_at']) if row['max_assumed_available_at'] else None))


def load_daily(root=ROOT):
    pointer = document(root / 'latest.json')
    path = Path(pointer['database'])
    verify(path, pointer['sha256'])
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        metadata = json.loads(connection.execute("SELECT value FROM metadata WHERE key='bundle'").fetchone()[0])
    if metadata['bundle_sha256'] != pointer['bundle_sha256']:
        raise ValueError('Published daily bundle identity mismatch')
    return PredictionStore(path, metadata)


def publish_archive(run, archive, root=ROOT, cache_dir=Path('.work/api-cache')):
    run, archive, root = Path(run).resolve(), Path(archive).resolve(), Path(root).resolve()
    base = PredictionStore.load(run, cache_dir)
    paths = sorted((root / 'archive').glob('*.json.gz'))
    for path in paths:
        manifest = path.with_suffix('.manifest.json')
        if not manifest.exists() and path == archive:
            write_document(manifest, dict(status='complete', sha256=checksum(path), base_sha256=base.metadata['bundle_sha256']))
        saved = document(manifest)
        verify(path, saved['sha256'])
        if saved['base_sha256'] != base.metadata['bundle_sha256']:
            raise ValueError('Archive manifest belongs to a different frozen benchmark')
    digest = hashlib.sha256((base.metadata['bundle_sha256'] + ''.join(checksum(p) for p in paths)).encode()).hexdigest()
    target = root / 'serving' / f'{archive.stem[:10]}-{digest}.sqlite3'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix('.tmp')
        temporary.unlink(missing_ok=True)
        try:
            with closing(base.connect()) as source, closing(sqlite3.connect(temporary)) as connection:
                source.backup(connection, pages=256)
                connection.execute('PRAGMA foreign_keys=ON')
                with connection:
                    for path in paths:
                        with gzip.open(path, 'rt', encoding='utf-8') as stream:
                            payload = json.load(stream)
                        if payload['base_sha256'] != base.metadata['bundle_sha256']:
                            raise ValueError('Daily archive belongs to a different frozen benchmark')
                        apply_payload(connection, payload)
                    metadata = dict(base.metadata, bundle_id='daily-' + payload['day'], bundle_sha256=digest,
                        data_mode='operational_daily_with_reconstructed_history', as_of=payload['prediction_at'],
                        availability_policy=payload['source_policy'], daily_archive=str(archive.resolve()),
                        snapshots=[dict(season_id=s, prediction_at=t, players=n) for s, t, n in connection.execute(
                            'SELECT season,prediction_at,count(*) FROM inputs GROUP BY season,prediction_at ORDER BY season,prediction_at')],
                        row_counts=dict(player_snapshots=connection.execute('SELECT count(*) FROM inputs').fetchone()[0],
                            intervals=connection.execute('SELECT count(*) FROM intervals').fetchone()[0],
                            games=connection.execute('SELECT count(*) FROM games').fetchone()[0]),
                        limitations=['Daily player inputs use actual observation cutoffs; historical rows remain reconstructed and are tagged separately.',
                            'Elo uses reconstructed historical replay and frozen development-selected parameters; no daily retuning.',
                            'Intervals use frozen earlier-season residual pools excluding the holdout; they are marginal, not guaranteed coverage.',
                            'Schedules update daily; this is not a live score feed. Upcoming games are filtered against the current clock.'])
                    metadata['seasons'] = sorted({row['season_id'] for row in metadata['snapshots']})
                    connection.execute("UPDATE metadata SET value=? WHERE key='bundle'", (json.dumps(metadata),))
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(target)) as connection:
        metadata = json.loads(connection.execute("SELECT value FROM metadata WHERE key='bundle'").fetchone()[0])
    pointer = dict(database=str(target.resolve()), sha256=checksum(target), bundle_sha256=digest,
        as_of=metadata['as_of'], day=metadata['bundle_id'][6:], base_run=str(run.resolve()), archive=str(archive.resolve()))
    write_document(root / 'latest.json', pointer)
    # Archives are permanent. Only disposable serving generations are bounded.
    old = sorted((root / 'serving').glob('*.sqlite3'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in old[2:]:
        if path != target:
            try:
                path.unlink()
            except OSError:  # A previous API generation can still be in use on Windows.
                pass
    return pointer


def publish_daily(season, env_file, base_run=None, root=None):
    root = Path(root or os.environ.get('NHL_DAILY_ROOT', str(ROOT)))
    root.mkdir(parents=True, exist_ok=True)
    prior = document(root / 'latest.json') if (root / 'latest.json').exists() else None
    run = Path(base_run or (prior['base_run'] if prior else discover_run())).resolve()
    base = PredictionStore.load(run, Path('.work/api-cache'))
    cutoff = datetime.now(timezone.utc)
    archive = root / 'archive' / f'{cutoff.date().isoformat()}.json.gz'
    if not archive.exists():
        with connect(env_file) as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            payload = collect_payload(connection, run, season, cutoff)
        payload['base_sha256'] = base.metadata['bundle_sha256']
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix('.tmp')
        with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
            json.dump(payload, stream, default=lambda v: v.isoformat(), separators=(',', ':'))
        temporary.replace(archive)
    return publish_archive(run, archive, root)
