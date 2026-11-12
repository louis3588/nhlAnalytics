"""Bounded pregame feature reconstruction from frozen NHL game-level sources."""

import heapq
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pyarrow.parquet as pq

from nhl_analytics.baselines import FRANCHISE_MAP
from nhl_analytics.database import timestamp
from nhl_analytics.prediction_store import csv_rows, document, verify
from nhl_analytics.reconstructed import MODE

VERSION = '1.0.0'
HOLDOUT = 20252026
RATE_METRICS = ('goal_diff_per_game', 'shot_share', 'save_pct', 'shots_for_per_game', 'shots_against_per_game', 'win_rate', 'opponent_rating')
FEATURES = ['elo_log_odds', *[f'{window}_{metric}_diff' for window, metrics in (
    ('season', ('goal_diff_per_game','shot_share','save_pct')),
    ('last10', RATE_METRICS), ('prior', ('goal_diff_per_game','shot_share','save_pct')))
    for metric in metrics], 'rest_days_diff', 'home_back_to_back', 'away_back_to_back', 'games_last7_diff',
    'home_season_games', 'away_season_games', 'home_prior_games', 'away_prior_games']


def franchise(team):
    return FRANCHISE_MAP.get(team, team)


def ratio(a, b):
    return a / b if b else None


def difference(a, b):
    return a - b if a is not None and b is not None else None


def read_sources(run):
    """Verify only relevant files; never load player prediction/shot archives."""
    run = Path(run).resolve()
    evaluation = document(run / 'manifest.json')
    if evaluation.get('status') != 'complete' or evaluation.get('data_mode') != MODE or evaluation.get('holdout') != HOLDOUT:
        raise ValueError('Expected the frozen reconstructed evaluation with the reserved 2025–26 benchmark')
    baseline = Path(evaluation['original_baseline'])
    verify(baseline / 'manifest.json', evaluation['original_manifest_sha256'])
    original = document(baseline / 'manifest.json')
    history = Path(original['history'])
    verify(history / 'manifest.json', evaluation['history_manifest_sha256'])
    source = document(history / 'manifest.json')
    if source.get('data_mode') != MODE or source.get('status') != 'complete' or source.get('game_type_id') != 2:
        raise ValueError('Expected a complete regular-season reconstruction')
    if original.get('status')!='complete' or original['history_manifest_sha256'] != evaluation['history_manifest_sha256']:
        raise ValueError('Baseline and frozen model use different histories')
    files = {str(run / 'manifest.json'): None, str(baseline / 'manifest.json'): evaluation['original_manifest_sha256'],
        str(history / 'manifest.json'): evaluation['history_manifest_sha256']}
    for name in ('selected_elo_model.json', 'elo_selection.json', 'selected_elo_predictions.csv'):
        files[str(run / name)] = evaluation['files'][name]['sha256']
    for name in ('games','skaters','goalies'):
        files[str(history / f'{name}.parquet')] = source['artifacts'][name]['sha256']
    from nhl_analytics.reconstructed import file_hash
    files[str(run / 'manifest.json')] = file_hash(run / 'manifest.json')
    for path, sha in files.items():
        verify(Path(path), sha)
    model = document(run / 'selected_elo_model.json')
    selection = document(run / 'elo_selection.json')
    verify(run / 'elo_selection.json', model['selection_sha256'])
    if not selection['excluded_holdout_from_selection'] or selection['parameters'] != model['parameters']:
        raise ValueError('Frozen Elo selection/provenance mismatch')
    if model.get('data_mode')!=MODE or original.get('data_mode')!=MODE or timestamp(model['as_of'])!=timestamp(evaluation['as_of']) or timestamp(original['as_of'])!=timestamp(evaluation['as_of']):
        raise ValueError('Frozen model and source clocks/modes differ')
    games = []
    for batch in pq.ParquetFile(history / 'games.parquet').iter_batches(batch_size=1000,use_threads=False):
        games.extend(batch.to_pylist())
    if len(games) != source['artifacts']['games']['rows'] or len({g['game_id'] for g in games}) != len(games):
        raise ValueError('Duplicate or incomplete schedule history')
    elo = {}
    for row in csv_rows(run / 'selected_elo_predictions.csv'):
        gid = int(row['game_id'])
        if gid in elo or row['data_mode'] != MODE:
            raise ValueError('Duplicate/mixed frozen Elo predictions')
        elo[gid] = row
    if set(elo) != {g['game_id'] for g in games}:
        raise ValueError('Frozen Elo game cohort differs from schedules')
    return games, elo, history, dict(source=source, evaluation=evaluation, model=model, files=files,
        as_of=timestamp(evaluation['as_of']), baseline=original)


def aggregate_players(history, games, manifests):
    """Stream player rows into ~16k team-game aggregates, not 246k dictionaries."""
    by_game = {g['game_id']:g for g in games}
    output = defaultdict(lambda:dict(skaters=0, goalies=0, shots=0, shots_valid=True, goalie_shots=0, saves=0, goalie_valid=True))
    coverage = {}
    columns = {
        'skaters':['player_id','game_id','season_id','home_road','team_abbrev','shots'],
        'goalies':['player_id','game_id','season_id','home_road','team_abbrev','shots_against','saves'],
    }
    for kind, fields in columns.items():
        seen = set()
        count = 0
        for batch in pq.ParquetFile(history / f'{kind}.parquet').iter_batches(batch_size=2000, columns=fields,use_threads=False):
            for row in batch.to_pylist():
                count += 1
                key = row['player_id'], row['game_id']
                game = by_game.get(row['game_id'])
                if key in seen or not game or not game['is_final'] or row['season_id'] != game['season_id'] or row['home_road'] not in ('H','R'):
                    raise ValueError(f'Invalid or duplicate {kind} player-game link')
                seen.add(key)
                side = 'home' if row['home_road'] == 'H' else 'away'
                if row['team_abbrev'] != game[side + '_team_abbrev']:
                    raise ValueError('Player/game side mismatch')
                item = output[row['game_id'], side]
                if kind == 'skaters':
                    item['skaters'] += 1
                    if row['shots'] is None:
                        item['shots_valid'] = False
                    elif row['shots'] < 0:
                        raise ValueError('Negative shot count')
                    else:
                        item['shots'] += row['shots']
                else:
                    item['goalies'] += 1
                    sa, saves = row['shots_against'], row['saves']
                    if sa is None or saves is None:
                        item['goalie_valid'] = False
                    elif not 0 <= saves <= sa:
                        raise ValueError('Invalid goalie save counts')
                    else:
                        item['goalie_shots'] += sa; item['saves'] += saves
        if count != manifests['artifacts'][kind]['rows']:
            raise ValueError('Player source row count mismatch')
        coverage[kind] = count
    for game in games:
        if not game['is_final']:
            continue
        for side in ('home','away'):
            row = output[game['game_id'],side]
            if row['skaters'] < 15 or row['goalies'] < 1:
                raise ValueError(f"Incomplete team player coverage: {game['game_id']}/{side}")
            row['shots'] = row['shots'] if row['shots_valid'] else None
            if not row['goalie_valid']:
                row['goalie_shots'] = row['saves'] = None
    coverage['team_games'] = len(output)
    return dict(output), coverage


def rates(records):
    n = len(records)
    if not n:
        return dict.fromkeys(RATE_METRICS)
    sf = sum(r['shots_for'] for r in records) if all(r['shots_for'] is not None for r in records) else None
    sa = sum(r['shots_against'] for r in records) if all(r['shots_against'] is not None for r in records) else None
    goalie_shots = sum(r['goalie_shots'] for r in records) if all(r['goalie_shots'] is not None for r in records) else None
    saves = sum(r['saves'] for r in records) if all(r['saves'] is not None for r in records) else None
    return dict(goal_diff_per_game=sum(r['goals_for']-r['goals_against'] for r in records)/n,
        shots_for_per_game=sf/n if sf is not None else None, shots_against_per_game=sa/n if sa is not None else None,
        shot_share=ratio(sf,sf+sa) if sf is not None and sa is not None else None,
        save_pct=ratio(saves,goalie_shots) if goalie_shots is not None and saves is not None else None,
        win_rate=sum(r['won'] for r in records)/n, opponent_rating=sum(r['opponent_rating'] for r in records)/n)


def build_dataset(games, elo, aggregates, as_of):
    """One cutoff-ordered sweep. A target's own result is enqueued only after it."""
    ordered = sorted(games, key=lambda g:(timestamp(elo[g['game_id']]['prediction_at']),g['start_time_utc'],g['game_id']))
    history = defaultdict(list)
    calendar = defaultdict(list)
    pending = []
    features, labels = [], []
    for game in ordered:
        gid, season = game['game_id'], game['season_id']
        frozen = elo[gid]
        cutoff = timestamp(frozen['prediction_at'])
        if cutoff > as_of or game['start_time_utc'] - timedelta(hours=1) > as_of:
            # This is an evaluation dataset, not a batch of distant future forecasts.
            continue
        if cutoff != game['start_time_utc'] - timedelta(hours=1):
            raise ValueError('Unexpected frozen pregame cutoff')
        available = game['assumed_available_at']
        if available <= game['start_time_utc']:
            raise ValueError('Result availability precedes game start')
        while pending and pending[0][0] < cutoff:
            _, _, entries = heapq.heappop(pending)
            for key, record in entries:
                history[key].append(record)
        row = dict(game_id=gid, season_id=season, prediction_at=cutoff.isoformat(), data_mode=MODE,
            home_team_id=game['home_team_id'], away_team_id=game['away_team_id'], game_date=game['game_date'].isoformat(),
            frozen_elo_probability=float(frozen['home_win_probability']),
            historical_home_probability=float(frozen['historical_home_win_probability']))
        p = row['frozen_elo_probability']
        if not 0 < p < 1:
            raise ValueError('Invalid frozen probability')
        row['elo_log_odds'] = math.log(p/(1-p))
        stamps = [timestamp(frozen['max_assumed_available_at'])] if frozen['max_assumed_available_at'] else []
        sides = {}
        for side in ('home','away'):
            team = franchise(game[side + '_team_id'])
            current = sorted(history[season,team], key=lambda r:(r['start'],r['game_id']))
            prior_season = (season//10000-1)*10000+season//10000
            prior = history[prior_season,team]
            snapshots = {w:rates(records) for w,records in [('season',current),('last10',current[-10:]),('prior',prior)]}
            # Rest/workload use earlier scheduled starts, never whether their
            # result was already published; last night's results may be delayed.
            previous = [g for g in calendar[team] if g['start_time_utc'] < cutoff]
            last = previous[-1] if previous else None
            gap = (game['game_date']-last['game_date']).days if last else 15
            snapshots.update(rest_days=min(14,max(0,gap-1)),back_to_back=int(gap==1),
                games_last7=sum(cutoff-timedelta(days=7)<=g['start_time_utc']<cutoff for g in previous))
            sides[side] = snapshots
            row[side+'_season_games'] = len(current)
            row[side+'_prior_games'] = len(prior)
            row[side+'_last_stats_game_id'] = current[-1]['game_id'] if current else None
            row[side+'_last_stats_available_at'] = max((r['available_at'] for r in current),default=None)
            stamps.extend(r['available_at'] for r in current+prior)
        for name in FEATURES:
            if name == 'elo_log_odds' or name.endswith('_season_games') or name.endswith('_prior_games'):
                continue
            if name.endswith('_back_to_back'):
                row[name] = sides[name.split('_')[0]]['back_to_back']
            elif name in ('rest_days_diff','games_last7_diff'):
                key = name[:-5]
                row[name] = sides['home'][key]-sides['away'][key]
            else:
                window, metric = name.split('_',1)
                metric = metric[:-5]
                row[name] = difference(sides['home'][window][metric],sides['away'][window][metric])
        latest = max(stamps,default=None)
        if latest and latest >= cutoff:
            raise ValueError('Pregame input availability reaches prediction timestamp')
        row['max_assumed_feature_available_at'] = latest.isoformat() if latest else None
        for side in ('home','away'):
            stamp = row[side+'_last_stats_available_at']
            row[side+'_last_stats_available_at'] = stamp.isoformat() if stamp else None
        features.append(row)
        for side in ('home','away'):
            calendar[franchise(game[side+'_team_id'])].append(game)
        if game['is_final']:
            if game['winner_team_id'] not in (game['home_team_id'],game['away_team_id']) or game['home_score']==game['away_score']:
                raise ValueError('Invalid binary game result')
            labels.append(dict(game_id=gid,season_id=season,data_mode=MODE,home_win=int(game['winner_team_id']==game['home_team_id']),
                label_available_at=available.isoformat()))
            entries = []
            for side,other in [('home','away'),('away','home')]:
                a,b = aggregates[gid,side],aggregates[gid,other]
                # The shootout deciding goal affects the winner, not scoring rates.
                score = lambda s:game[s+'_score']-int(game.get('result_type')=='SO' and game['winner_team_id']==game[s+'_team_id'])
                record = dict(game_id=gid,start=game['start_time_utc'],available_at=available,goals_for=score(side),goals_against=score(other),
                    shots_for=a['shots'],shots_against=b['shots'],goalie_shots=a['goalie_shots'],saves=a['saves'],
                    won=int(game['winner_team_id']==game[side+'_team_id']),opponent_rating=float(frozen[other+'_rating']))
                entries.append(((season,franchise(game[side+'_team_id'])),record))
            heapq.heappush(pending,(available,gid,entries))
    return features, labels
