"""Observed-time inference for the already frozen team model pair. No fitting."""

import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from nhl_analytics.asof import past_final, read_games, read_player_games
from nhl_analytics.baselines import elo_probability
from nhl_analytics.database import timestamp
from nhl_analytics.live_store import digest
from nhl_analytics.prediction_store import document, verify
from nhl_analytics.pregame import FEATURES, difference, franchise, rates, read_sources

POLICY = dict(version='prospective-observed-v1',cutoff_minutes=60,window_minutes=5,
    result_stability_minutes=10,minimum_interval_weeks=10,minimum_interval_games=100,
    bootstrap_replicates=1000,forecast_mode='prospective_observed',
    feature_policy='Actual stored source observations strictly before feature_as_of; no live player stats or reconstructed raw-feature fallback')
DEFAULT_MODEL_RUN = Path('data/team_models/reconstructed/20261111T150925233807Z')


class FeatureUnavailable(ValueError):
    pass


def load_bundle(model_run=DEFAULT_MODEL_RUN):
    run=Path(model_run).resolve();manifest=document(run/'manifest.json')
    if manifest.get('status')!='complete' or manifest.get('features_only') or manifest.get('production_changed'):
        raise ValueError('A complete frozen team-model comparison is required')
    dataset=document(Path(manifest['dataset'])/'manifest.json')
    verify(Path(manifest['dataset'])/'manifest.json',manifest['dataset_manifest_sha256'])
    games,elo,_,source=read_sources(Path(dataset['evaluation']))
    candidates={int(Path(name).name.split('_')[0]):name for name in manifest['files'] if name.replace('\\','/').startswith('models/') and name.endswith('_logistic_team_context.json')}
    season=max(candidates)
    name=candidates[season]
    verify(run/name,manifest['files'][name]['sha256'])
    logistic=document(run/name)
    if logistic['feature_columns']!=FEATURES or 20252026 in logistic['fold']['training_seasons']:
        raise ValueError('Unexpected logistic schema or reserved-benchmark fitting')
    as_of=timestamp(source['model']['as_of'])
    seeded=[g['game_id'] for g in games if g['is_final'] and g['assumed_available_at']<as_of]
    prior=(season//10000-1)*10000+season//10000
    seeded_set=set(seeded)
    history={str(g['game_id']):dict(home=g['home_team_id'],away=g['away_team_id'],home_rating=float(elo[g['game_id']]['home_rating']),
        away_rating=float(elo[g['game_id']]['away_rating'])) for g in games if g['season_id'] in (prior,season) and g['game_id'] in seeded_set}
    reference=[]
    if 'metrics.csv' in manifest['files']:
        from nhl_analytics.prediction_store import csv_rows
        verify(run/'metrics.csv',manifest['files']['metrics.csv']['sha256'])
        reference=[dict(season_id=int(r['season_id']),role=r['role'],model=r['model'],n=int(r['n']),
            brier=float(r['brier']),log_loss=float(r['log_loss']),accuracy=float(r['accuracy'])) for r in csv_rows(run/'metrics.csv')
            if r['model'] in ('frozen_elo','logistic_team_context')]
    from nhl_analytics.reconstructed import file_hash
    return dict(season_id=season,elo=source['model'],logistic=logistic,seeded_game_ids=seeded,rating_history=history,
        elo_version=file_hash(Path(dataset['evaluation'])/'selected_elo_model.json'),logistic_version=file_hash(run/name),
        references=reference,reference_as_of=source['evaluation']['as_of'],
        artifacts=dict(elo_run=dataset['evaluation'],logistic_run=str(run),logistic_manifest_sha256=file_hash(run/'manifest.json'),
            source_manifest_sha256=dataset['original_files'],training_origin='reconstructed_latest_corrected'),policy=POLICY)


def predict_logistic(model,features):
    """Replay the saved imputer/scaler/coefficients without importing sklearn."""
    raw=[features.get(name) for name in model['feature_columns']]
    missing=[v is None for v in raw]
    values=[float(model['imputer_statistics'][i]) if missing[i] else float(v) for i,v in enumerate(raw)]
    if not all(math.isfinite(v) for v in values):raise FeatureUnavailable('nonfinite_feature')
    values.extend(float(missing[i]) for i in model['missing_indicator_indices'])
    if not len(values)==len(model['scaler_mean'])==len(model['scaler_scale'])==len(model['coefficients']):
        raise FeatureUnavailable('invalid_artifact_shape')
    if any(not math.isfinite(float(v)) for key in ('scaler_mean','scaler_scale','coefficients') for v in model[key]) or any(v<=0 for v in model['scaler_scale']):
        raise FeatureUnavailable('invalid_artifact_scaling')
    z=float(model['intercept'])+sum((v-mean)/scale*coefficient for v,mean,scale,coefficient in zip(values,model['scaler_mean'],model['scaler_scale'],model['coefficients']))
    return 1/(1+math.exp(-z)) if z>=0 else math.exp(z)/(1+math.exp(z))


def observed_elo(bundle,games,as_of,target_id):
    """Frozen state seed, then causal updates from newly observed final results."""
    import heapq
    seed=bundle['elo'];seed_time=timestamp(seed['as_of'])
    ratings=defaultdict(lambda:1500.0,{int(k):float(v) for k,v in seed['franchise_ratings'].items()})
    seen=set(bundle['seeded_game_ids']);context=dict(bundle['rating_history']);pending=[];used=[]
    parameters=seed['parameters']
    for game in sorted((g for gid,g in games.items() if gid not in seen and gid!=target_id and timestamp(g['start_time_utc'])<as_of),key=lambda g:(timestamp(g['start_time_utc']),g['game_id'])):
        pre=max(seed_time,timestamp(game['start_time_utc'])-timedelta(hours=1))
        while pending and pending[0][0]<pre:
            available,gid,h,a,delta=heapq.heappop(pending);ratings[h]+=delta;ratings[a]-=delta;used.append(available)
        h,a=franchise(game['home_team_id']),franchise(game['away_team_id'])
        probability=elo_probability(ratings[h],ratings[a],parameters['elo_home_advantage'])
        context[str(game['game_id'])]=dict(home=game['home_team_id'],away=game['away_team_id'],home_rating=ratings[h],away_rating=ratings[a])
        if past_final(game,as_of):
            known=max(game['available_at'],seed_time+timedelta(microseconds=1))
            won=int(game['winner_team_id']==game['home_team_id'])
            heapq.heappush(pending,(known,game['game_id'],h,a,parameters['elo_k']*(won-probability)))
    while pending and pending[0][0]<as_of:
        available,gid,h,a,delta=heapq.heappop(pending);ratings[h]+=delta;ratings[a]-=delta;used.append(available)
    return ratings,context,used


def load_observed_inputs(connection,campaign,as_of):
    season=campaign['season_id'];prior=(season//10000-1)*10000+season//10000
    games=read_games(connection,as_of,[prior,season],2)
    records,source_meta=read_player_games(connection,as_of,[prior,season],2,games)
    return games,records,source_meta


def observed_features(connection,campaign,fixture,as_of,prepared=None):
    bundle=campaign['bundle'];season=fixture['season_id'];target=fixture['game_id']
    if season!=bundle['season_id']:raise FeatureUnavailable('unsupported_model_season')
    if campaign['registered_at']>=as_of:raise FeatureUnavailable('model_not_available_before_snapshot')
    if fixture['provider_checked_at']>=as_of or as_of-fixture['provider_checked_at']>timedelta(minutes=15):
        raise FeatureUnavailable('schedule_not_recently_confirmed')
    prior=(season//10000-1)*10000+season//10000
    raw_games,raw_records,source_meta=prepared or load_observed_inputs(connection,campaign,as_of)
    games={gid:g for gid,g in raw_games.items() if g['available_at']<as_of}
    games.pop(target,None)
    records=[r for r in raw_records if r['game_id']!=target and r['available_at']<as_of]
    ratings,context,elo_sources=observed_elo(bundle,games,as_of,target)
    players=defaultdict(list)
    for row in records:players[row['game_id'],row['team_id']].append(row)
    teams={franchise(fixture['home_team_id']),franchise(fixture['away_team_id'])}
    history=defaultdict(list);stamps=[campaign['registered_at'],fixture['provider_checked_at'],*elo_sources]
    source_ids=set();source_games=[];missing_ratings=0
    for gid,game in games.items():
        if not past_final(game,as_of):continue
        for side,other in [('home','away'),('away','home')]:
            team=game[side+'_team_id']
            if franchise(team) not in teams:continue
            lines=players[gid,team];other_lines=players[gid,game[other+'_team_id']]
            skaters=[r for r in lines if r['kind']=='skater'];goalies=[r for r in lines if r['kind']=='goalie']
            opposed=[r for r in other_lines if r['kind']=='skater']
            if len(skaters)<15 or len(opposed)<15 or not goalies:
                raise FeatureUnavailable(f'incomplete_known_final_game_{gid}')
            total=lambda rows,key:sum(r['payload'][key] for r in rows) if all(r['payload'].get(key) is not None for r in rows) else None
            shot_for,shot_against=total(skaters,'shots'),total(opposed,'shots')
            sa,ga=total(goalies,'shotsAgainst'),total(goalies,'goalsAgainst')
            saves=sa-ga if sa is not None and ga is not None else None
            if shot_for is None or shot_against is None or sa is None or saves is None:
                raise FeatureUnavailable(f'missing_stat_counts_{gid}')
            if min(shot_for,shot_against,sa,saves)<0 or saves>sa:raise FeatureUnavailable('invalid_stat_counts')
            rating=context.get(str(gid))
            opposition=rating.get(other+'_rating') if rating and rating[side]==team else None
            missing_ratings+=int(opposition is None)
            score=lambda s:game[s+'_score']-int(game.get('result_type')=='SO' and game['winner_team_id']==game[s+'_team_id'])
            available=max([game['available_at'],*(r['available_at'] for r in lines+other_lines)])
            stamps.append(available)
            source_ids.update(str(r['source_id']) for r in lines+other_lines if r.get('source_id'))
            source_games.append(gid)
            history[game['season_id'],franchise(team)].append(dict(game_id=gid,start=timestamp(game['start_time_utc']),available_at=available,
                goals_for=score(side),goals_against=score(other),shots_for=shot_for,shots_against=shot_against,
                goalie_shots=sa,saves=saves,won=int(game['winner_team_id']==team),opponent_rating=opposition))
    row={};sides={};coverage={}
    for side in ('home','away'):
        team=franchise(fixture[side+'_team_id'])
        current=sorted(history[season,team],key=lambda r:r['start']);previous=history[prior,team]
        def safe_rates(values):
            result=rates([dict(r,opponent_rating=r['opponent_rating'] or 0) for r in values])
            if any(r['opponent_rating'] is None for r in values):result['opponent_rating']=None
            return result
        data={w:safe_rates(values) for w,values in [('season',current),('last10',current[-10:]),('prior',previous)]}
        calendar=sorted((g for g in games.values() if team in (franchise(g['home_team_id']),franchise(g['away_team_id'])) and timestamp(g['start_time_utc'])<as_of),key=lambda g:timestamp(g['start_time_utc']))
        if any(not past_final(g,as_of) and as_of-timestamp(g['start_time_utc'])>timedelta(hours=6) for g in calendar if g['season_id']==season):
            raise FeatureUnavailable('unvalidated_past_game')
        last=calendar[-1] if calendar else None
        day=fixture['game_date'] if isinstance(fixture['game_date'],date) else date.fromisoformat(fixture['game_date'])
        gap=(day-date.fromisoformat(last['game_date'])).days if last else 15
        data.update(rest_days=min(14,max(0,gap-1)),back_to_back=int(gap==1),games_last7=sum(as_of-timedelta(days=7)<=timestamp(g['start_time_utc'])<as_of for g in calendar))
        stamps.extend(g['available_at'] for g in calendar)
        sides[side]=data;row[side+'_season_games']=len(current);row[side+'_prior_games']=len(previous)
        coverage[side]=dict(current_games=len(current),prior_games=len(previous))
    if not any(c['current_games'] or c['prior_games'] for c in coverage.values()):
        raise FeatureUnavailable('no_observed_team_history')
    probability=elo_probability(ratings[franchise(fixture['home_team_id'])],ratings[franchise(fixture['away_team_id'])],bundle['elo']['parameters']['elo_home_advantage'])
    row['elo_log_odds']=math.log(probability/(1-probability))
    for name in FEATURES:
        if name in row:continue
        if name.endswith('_back_to_back'):row[name]=sides[name.split('_')[0]]['back_to_back']
        elif name in ('rest_days_diff','games_last7_diff'):row[name]=sides['home'][name[:-5]]-sides['away'][name[:-5]]
        else:
            window,metric=name.split('_',1);row[name]=difference(sides['home'][window][metric[:-5]],sides['away'][window][metric[:-5]])
    latest=max(stamps)
    if latest>=as_of:raise FeatureUnavailable('source_not_before_snapshot')
    from nhl_analytics.reconstructed import file_hash
    provenance=dict(data_mode='prospective_observed',feature_as_of=as_of.isoformat(),max_source_available_at=latest.isoformat(),
        source_game_ids=sorted(set(source_games)),game_log_sources=source_meta,coverage=coverage,
        target_game_excluded=True,live_statistics_used=False,historical_raw_features_substituted=False,
        rating_context_origin='Frozen historical model-state artifact plus causal observed-result updates; reconstructed training origin explicitly retained',
        model_registered_at=campaign['registered_at'].isoformat(),missing_rating_records=missing_ratings,
        implementation_sha256=file_hash(Path(__file__)))
    return row,probability,predict_logistic(bundle['logistic'],row),latest,provenance
