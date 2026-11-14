"""One fixed season-to-date/prior-shrunk xG ablation, development seasons only."""
import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

from nhl_analytics.baselines import probability_metrics
from nhl_analytics.database import connect, timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import csv_rows, document, verify
from nhl_analytics.pregame import FEATURES, franchise, read_sources
from nhl_analytics.reconstructed import MODE, file_hash
from nhl_analytics.team_ablation import DEVELOPMENT, DEFAULT_CANDIDATE, paired, score_cohort, load_inputs
from nhl_analytics.team_feature_audit import DEFAULT_RUN
from nhl_analytics.team_models import CONFIG, chronological_folds, fit_logistic, predict_saved

PRIOR_GAMES=20
FEATURE='shrunk_season_xg_strength_diff'
MODEL='context_plus_shrunk_xg'


def design():
    return dict(version='1.0.0',role='development_only_research',data_mode=MODE,
        development_seasons=list(DEVELOPMENT),evaluation_seasons=list(DEVELOPMENT[1:]),
        excluded_seasons=[20252026,20262027],feature_columns=[*FEATURES,FEATURE],
        situation='MoneyPuck 5on5-labelled team-game records',xg_definition='Existing raw xGoalsFor minus xGoalsAgainst',
        formula='(sum current-season net xG + 20 * earlier prior-season mean net xG) / (current games + 20)',
        prior_pseudo_games=PRIOR_GAMES,empty_prior='Fixed neutral zero net xG; source flag and counts retained',
        missing_source='Any missing net xG in known current/prior history makes the feature missing; no silent deletion',
        selection='One fixed formula and weight; no tuning, grid search, recalibration or promotion',
        preprocessing='Existing training-only mean imputation, missing indicators, standardisation and C=1 L2 logistic',
        parameters=CONFIG,baseline='Reuse verified original per-fold context model; identical training IDs and game cohort',
        availability='Frozen NHL next-day assumed availability; reconstructed only, not observed historical publication times',
        source='Pin original MoneyPuck file identities from the previous audit; no download or shot scan',
        production_models_changed=False)


def strength(current,prior,target,cutoff):
    eligible=lambda rows:[r for r in rows if r['game_id']!=target and r['start']<cutoff and r['available_at']<cutoff]
    current=eligible(current);prior=eligible(prior)
    origin='known_prior_season' if prior else 'neutral_zero_net_xg'
    complete=all(r['net_xg'] is not None for r in current+prior)
    prior_mean=sum(r['net_xg'] for r in prior)/len(prior) if prior and complete else 0. if not prior else None
    value=(sum(r['net_xg'] for r in current)+PRIOR_GAMES*prior_mean)/(len(current)+PRIOR_GAMES) if complete else None
    stamps=[r['available_at'] for r in current+prior]
    return dict(value=value,current_games=len(current),prior_games=len(prior),prior_origin=origin,
        prior_mean_net_xg=prior_mean,current_weight=len(current)/(len(current)+PRIOR_GAMES),
        complete=complete,max_available_at=max(stamps).isoformat() if stamps else None,
        source_game_ids=sorted({r['game_id'] for r in current+prior}))


def load_pinned_history(connection,candidate_dir,games):
    inventory={};files={}
    for row in csv_rows(candidate_dir/'xg_source_inventory.csv'):
        if int(row['season_id']) not in DEVELOPMENT:continue
        key=int(row['game_id']),int(row['team_id'])
        if key in inventory:raise ValueError('Duplicate pinned source team-game')
        inventory[key]=row['file_id'];files[row['file_id']]=row['file_sha256']
    if not files:raise ValueError('No pinned development xG source')
    by_game={g['game_id']:g for g in games if g['season_id'] in DEVELOPMENT and g['is_final']}
    history=defaultdict(list);snapshot=[];seen=set()
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT r.game_id,r.team_code,r.season_id,r.game_date,r.xgoals_for,r.xgoals_against,
            r.file_id,f.sha256,f.observed_at,f.completed_at FROM analytics.moneypuck_records r
            JOIN analytics.moneypuck_files f USING(file_id)
            WHERE r.kind='team_game' AND r.situation='5on5' AND r.game_type_id=2
            AND r.season_id=ANY(%s) AND r.file_id=ANY(%s::uuid[]) AND f.status='complete'
            ORDER BY r.game_id,r.team_code''',(list(DEVELOPMENT),list(files)))
        for row in cursor:
            game=by_game.get(row['game_id'])
            if not game:raise ValueError('Pinned xG game missing from frozen NHL history')
            side=[s for s in ('home','away') if game[s+'_team_abbrev']==row['team_code']]
            if len(side)!=1 or row['game_date']!=game['game_date']:raise ValueError('Pinned game/team/date mismatch')
            team=franchise(game[side[0]+'_team_id']);key=game['game_id'],team
            if key in seen or inventory.get(key)!=str(row['file_id']):raise ValueError('Duplicate/unpinned xG observation')
            if files[str(row['file_id'])]!=row['sha256']:raise ValueError('Pinned MoneyPuck file hash changed')
            seen.add(key);gf,ga=row['xgoals_for'],row['xgoals_against']
            if any(v is not None and (not math.isfinite(v) or v<0) for v in (gf,ga)):raise ValueError('Invalid xG counts')
            record=dict(game_id=game['game_id'],start=game['start_time_utc'],available_at=game['assumed_available_at'],
                net_xg=gf-ga if gf is not None and ga is not None else None)
            history[game['season_id'],team].append(record)
            snapshot.append(dict(game_id=game['game_id'],season_id=game['season_id'],team_id=team,xgoals_for=gf,xgoals_against=ga,
                assumed_available_at=record['available_at'],file_id=str(row['file_id']),file_sha256=row['sha256'],
                actual_file_observed_at=row['observed_at'],actual_file_completed_at=row['completed_at']))
    if seen!=set(inventory):raise ValueError('Pinned source inventory no longer complete')
    # Preserve missing known games instead of silently shrinking the denominator.
    for game in by_game.values():
        for side in ('home','away'):
            team=franchise(game[side+'_team_id'])
            if (game['game_id'],team) not in seen:
                history[game['season_id'],team].append(dict(game_id=game['game_id'],start=game['start_time_utc'],
                    available_at=game['assumed_available_at'],net_xg=None))
    return history,snapshot


def enrich(rows,history):
    output=[]
    for row in rows:
        if row['season_id'] not in DEVELOPMENT:raise ValueError('Holdout/later rows forbidden')
        cutoff=timestamp(row['prediction_at']);season=row['season_id'];prior=(season//10000-1)*10000+season//10000
        data={}
        for side in ('home','away'):
            team=franchise(row[side+'_team_id'])
            data[side]=strength(history.get((season,team),[]),history.get((prior,team),[]),row['game_id'],cutoff)
        result={k:v for k,v in row.items() if k not in (FEATURE,)}
        a,b=data['home']['value'],data['away']['value']
        result[FEATURE]=a-b if a is not None and b is not None else None
        for side,item in data.items():
            for key,value in item.items():result[side+'_strength_'+key]=json.dumps(value) if isinstance(value,list) else value
        stamps=[timestamp(item['max_available_at']) for item in data.values() if item['max_available_at']]
        if row.get('max_assumed_feature_available_at'):stamps.append(timestamp(row['max_assumed_feature_available_at']))
        latest=max(stamps,default=None)
        if latest and latest>=cutoff:raise ValueError('Strength input reaches prediction cutoff')
        result['max_assumed_feature_available_at']=latest.isoformat() if latest else None
        output.append(result)
    return output


def evaluate(rows,labels,source,original,model_run,output):
    if any(r['season_id'] not in DEVELOPMENT for r in rows+labels):raise ValueError('Holdout/later seasons forbidden')
    predictions=[];folds=[];model_hashes={};truth={r['game_id']:r['home_win'] for r in labels}
    original_files={k.replace('\\','/'):v for k,v in original['files'].items()}
    for season,cutoff,train,test,label_map in chronological_folds(rows,labels,timestamp(source['as_of'])):
        ids=sorted(r['game_id'] for r in train);sha=hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        name=f'models/{season}_logistic_team_context.json';path=model_run/name
        verify(path,original_files[name]['sha256']);model_hashes[str(path)]=file_hash(path)
        baseline=document(path)
        if baseline['fold']['training_game_ids_sha256']!=sha or baseline['feature_columns']!=FEATURES or baseline['parameters']!=CONFIG:
            raise ValueError('Original baseline training cohort/settings mismatch')
        latest=max(timestamp(label_map[r['game_id']]['label_available_at']) for r in train)
        if latest>=cutoff:raise ValueError('Training labels reach test period')
        fold=dict(season_id=season,training_games=len(train),test_games=len(test),training_game_ids_sha256=sha,
            training_seasons=sorted({r['season_id'] for r in train}),prediction_start=cutoff,max_training_label_available_at=latest,
            original_baseline_sha256=model_hashes[str(path)])
        folds.append(fold)
        _,candidate=fit_logistic(train,truth,[*FEATURES,FEATURE])
        write_document(output/'research_models'/f'{season}_{MODEL}.json',dict(candidate,fold=fold,role='development_only_research'))
        base=predict_saved(baseline,test);new=predict_saved(candidate,test)
        for i,row in enumerate(test):
            predictions.append(dict(game_id=row['game_id'],season_id=season,game_date=row['game_date'],prediction_at=row['prediction_at'],
                data_mode=MODE,home_win=truth[row['game_id']],frozen_elo=row['frozen_elo_probability'],baseline_context=base[i],
                **{MODEL:new[i]},strength_feature=row[FEATURE],complete_inputs=row[FEATURE] is not None,
                early_season=min(row['home_strength_current_games'],row['away_strength_current_games'])<10))
    metrics=[];comparisons=[];monthly=[];calibration=[];cohorts=[]
    names=['frozen_elo','baseline_context',MODEL]
    for season in [*DEVELOPMENT[1:],0]:
        group=[r for r in predictions if not season or r['season_id']==season]
        if not group:raise ValueError('Missing development evaluation fold')
        metrics.extend(score_cohort(group,season,'all_games',names));comparisons.append(dict(paired(group,MODEL,season),cohort='all_games'))
        for key in ('early_season','complete_inputs'):
            for value in (True,False):
                subset=[r for r in group if r[key]==value]
                if subset:cohorts.extend(score_cohort(subset,season,f'{key}={value}',names))
        if not season:continue
        for month in sorted({r['game_date'][:7] for r in group}):
            monthly.extend(dict(r,month=month) for r in score_cohort([r for r in group if r['game_date'].startswith(month)],season,'all_games',names))
        for name in names:
            for bucket in range(10):
                values=[r for r in group if min(9,int(r[name]*10))==bucket]
                calibration.append(dict(season_id=season,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                    mean_probability=sum(r[name] for r in values)/len(values) if values else None,
                    observed_home_win_rate=sum(r['home_win'] for r in values)/len(values) if values else None))
    for name,values in [('predictions',predictions),('metrics',metrics),('monthly_metrics',monthly),('calibration',calibration),('cohort_metrics',cohorts),('paired_comparisons',comparisons)]:
        save_csv(output/(name+'.csv'),values)
    write_document(output/'metrics.json',metrics);write_document(output/'paired_comparisons.json',comparisons);write_document(output/'folds.json',folds)
    for path,sha in model_hashes.items():verify(Path(path),sha)
    return predictions,metrics,comparisons,folds


def run(model_run,candidate_dir,output):
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty experiment directory')
    output.mkdir(parents=True,exist_ok=True)
    inputs={str(p.resolve()):file_hash(p) for p in [model_run/'manifest.json',candidate_dir/'summary.json',
        candidate_dir/'candidate_features_reconstructed.csv',candidate_dir/'xg_source_inventory.csv']}
    write_document(output/'experiment_plan.json',dict(design(),registered_at=datetime.now(timezone.utc),
        input_sha256=inputs,implementation_sha256=file_hash(Path(__file__))))
    rows,labels,source,original=load_inputs(model_run,candidate_dir)
    games,_,_,provenance=read_sources(Path(source['evaluation']))
    with connect() as connection,connection.transaction():
        connection.execute('SET TRANSACTION READ ONLY')
        history,snapshot=load_pinned_history(connection,candidate_dir,games)
    enriched=enrich(rows,history)
    save_csv(output/'pinned_team_games.csv',snapshot);save_csv(output/'feature_dataset.csv',enriched)
    predictions,metrics,comparisons,folds=evaluate(enriched,labels,source,original,model_run,output)
    lines=['# Fixed longer-horizon, prior-shrunk xG test','',
        'One new feature, fixed before fitting. Production models and the holdout remain untouched.',
        'Team strength = (current-season net 5v5 xG + 20 × prior-season mean net 5v5 xG) / (current games + 20). The added input is home strength minus away strength.',
        'The prior is a fixed neutral zero-net-xG value when no earlier season exists in the source. This applies to the 2021–22 warm-up data and is labelled in the feature metadata.',
        '', '| Season | Model | Games | Brier ↓ | Log loss ↓ | Accuracy |','| --- | --- | ---: | ---: | ---: | ---: |']
    for r in metrics:lines.append(f"| {r['season_id'] or 'Pooled development'} | {r['model']} | {r['n']} | {r['brier']:.6f} | {r['log_loss']:.6f} | {r['accuracy']:.2%} |")
    lines.extend(['','## Paired comparison against context','',
        'Negative Brier delta favours the addition. Calendar-week bootstrap intervals are exploratory and conditional on the fitted models.',
        '', '| Season | Brier delta | Exploratory 95% interval |','| --- | ---: | --- |'])
    for r in comparisons:
        interval=f"[{r['brier_lower']:.6f}, {r['brier_upper']:.6f}]" if r['brier_lower'] is not None else 'Insufficient weeks'
        lines.append(f"| {r['season_id'] or 'Pooled development'} | {r['brier_delta']:+.6f} | {interval} |")
    lines.extend(['','## Interpretation limits','',
        '- Three chronological development folds only: 2022–23 through 2024–25, with earlier seasons training each fold. No 2025–26 or later labels are scored, fitted or used for selection.',
        '- The 20-game prior weight was chosen before this result and not optimised. No alternate windows, weights, xG definitions or combined candidates were tried.',
        '- Existing raw MoneyPuck xG is retained, so units are net xG per game. There is no new per-60 exposure conversion or score/venue adjustment in this test.',
        '- Missing known observations remain in history and make the feature missing; actual publication times are not inferred from game time.',
        '- The dataset is reconstructed with frozen NHL next-day availability assumptions. Actual MoneyPuck import/completion timestamps are preserved separately, not rewritten.',
        '- The original 2021–22 warm-up lacks a prior season. Its neutral prior can produce a first-fold distribution difference. This is recorded, not repaired after scoring.',
        '- Frozen Elo settings were previously selected across development seasons. These are retrospective development comparisons, not a new pristine holdout.',
        '- No model promotion or production forecast changes. Development results alone cannot establish future superiority.',''])
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    for path,sha in inputs.items():verify(Path(path),sha)
    summary=dict(status='complete',role='development_only_research',data_mode=MODE,
        production_changed=False,holdout_scored=False,holdout_used_for_fitting=False,
        evaluation_games=len(predictions),feature_rows=len(enriched),new_features=[FEATURE],prior_pseudo_games=PRIOR_GAMES,
        missing_feature_rows=sum(r[FEATURE] is None for r in enriched),
        neutral_prior_side_rows=sum(r[side+'_strength_prior_origin']=='neutral_zero_net_xg' for r in enriched for side in ('home','away')),
        plan_sha256=file_hash(output/'experiment_plan.json'),source_files=provenance['files'],
        files={p.relative_to(output).as_posix():dict(sha256=file_hash(p)) for p in output.rglob('*') if p.is_file()})
    write_document(output/'manifest.json',summary)
    return {k:summary[k] for k in ('status','evaluation_games','feature_rows','missing_feature_rows','prior_pseudo_games','production_changed','holdout_scored')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-run',type=Path,default=DEFAULT_RUN)
    parser.add_argument('--candidate',type=Path,default=DEFAULT_CANDIDATE)
    parser.add_argument('--output',type=Path,default=Path('data/xg_strength/development')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();print(json.dumps(run(args.model_run,args.candidate,args.output),indent=2))


if __name__=='__main__':main()
