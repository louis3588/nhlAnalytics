"""Shared bounded development data and evaluation for Phase 2 research."""
import hashlib
import importlib.metadata
import json
import shutil
from collections import defaultdict
from pathlib import Path

from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import document, verify, csv_rows
from nhl_analytics.pregame import FEATURES, franchise, read_sources
from nhl_analytics.reconstructed import file_hash, MODE
from nhl_analytics.team_ablation import DEVELOPMENT, DEFAULT_CANDIDATE, load_inputs
from nhl_analytics.team_feature_audit import DEFAULT_RUN
from nhl_analytics.team_models import CONFIG, chronological_folds, fit_logistic, predict_saved, paired_week_bootstrap

XG_RUN=Path('data/xg_strength/development/20261113-fixed-prior20')


def inputs():
    paths=[DEFAULT_RUN/'manifest.json',DEFAULT_CANDIDATE/'summary.json',DEFAULT_CANDIDATE/'candidate_features_reconstructed.csv',
        XG_RUN/'manifest.json',XG_RUN/'pinned_team_games.csv']
    return {str(p.resolve()):file_hash(p) for p in paths}


def load_data():
    rows,labels,source,original=load_inputs(DEFAULT_RUN,DEFAULT_CANDIDATE)
    all_games,_,history,_=read_sources(Path(source['evaluation']))
    games={g['game_id']:g for g in all_games if g['season_id'] in DEVELOPMENT}
    manifest=document(XG_RUN/'manifest.json')
    verify(XG_RUN/'pinned_team_games.csv',manifest['files']['pinned_team_games.csv']['sha256'])
    sides=defaultdict(dict)
    for r in csv_rows(XG_RUN/'pinned_team_games.csv'):
        if int(r['season_id']) not in DEVELOPMENT:continue
        gid=int(r['game_id']);team=int(r['team_id'])
        sides[gid][team]=dict(net=float(r['xgoals_for'])-float(r['xgoals_against']),available=timestamp(r['assumed_available_at']))
    xg=[]
    for gid,g in games.items():
        if not g['is_final']:continue
        h,a=franchise(g['home_team_id']),franchise(g['away_team_id'])
        pair=sides.get(gid,{})
        if h not in pair or a not in pair:raise ValueError('Incomplete pinned 5v5 team pair')
        if abs(pair[h]['net']+pair[a]['net'])>1e-3:raise ValueError('Home/away xG exposure mismatch')
        available=max(pair[h]['available'],pair[a]['available'],g['assumed_available_at'])
        if available<=g['start_time_utc']:raise ValueError('Source availability precedes game start')
        xg.append(dict(game_id=gid,season_id=g['season_id'],home=h,away=a,
            start=g['start_time_utc'],available=available,net=(pair[h]['net']-pair[a]['net'])/2))
    return rows,labels,games,xg,history,source,original


def compare(rows,name,reference,season):
    view=[dict(r,frozen_elo=r[reference]) for r in rows]
    return dict(paired_week_bootstrap(view,name,seed=42+season),season_id=season,reference_model=reference)


def evaluate(rows,labels,source,original,candidates,output):
    """Same folds, metrics and bootstrap as existing studies; no benchmark/later seasons."""
    from nhl_analytics.baselines import probability_metrics
    if any(r['season_id'] not in DEVELOPMENT for r in rows+labels):raise ValueError('Nondevelopment input forbidden')
    truth={r['game_id']:r['home_win'] for r in labels};predictions=[];folds=[]
    files={k.replace('\\','/'):v for k,v in original['files'].items()}
    for season,cutoff,train,test,label_map in chronological_folds(rows,labels,timestamp(source['as_of'])):
        sha=hashlib.sha256(json.dumps(sorted(r['game_id'] for r in train)).encode()).hexdigest()
        name=f'models/{season}_logistic_team_context.json';path=DEFAULT_RUN/name
        verify(path,files[name]['sha256']);baseline=document(path)
        if baseline['fold']['training_game_ids_sha256']!=sha or baseline['parameters']!=CONFIG:raise ValueError('Frozen baseline cohort mismatch')
        fold=dict(season_id=season,training_seasons=sorted({r['season_id'] for r in train}),training_games=len(train),test_games=len(test),
            training_game_ids_sha256=sha,prediction_start=cutoff,
            max_training_label_available_at=max(timestamp(label_map[r['game_id']]['label_available_at']) for r in train))
        folds.append(fold);probabilities={'baseline_context':predict_saved(baseline,test)}
        for candidate,columns in candidates.items():
            _,model=fit_logistic(train,truth,columns);probabilities[candidate]=predict_saved(model,test)
            write_document(output/'research_models'/f'{season}_{candidate}.json',dict(model,fold=fold,role='development_only_research'))
        for i,row in enumerate(test):
            predictions.append(dict(game_id=row['game_id'],season_id=season,game_date=row['game_date'],prediction_at=row['prediction_at'],
                home_win=truth[row['game_id']],frozen_elo=row['frozen_elo_probability'],home_rate=float(row['historical_home_probability']),
                **{k:p[i] for k,p in probabilities.items()}))
    names=['home_rate','frozen_elo','baseline_context',*candidates];metrics=[];paired=[];monthly=[];calibration=[]
    for season in [*DEVELOPMENT[1:],0]:
        group=[r for r in predictions if not season or r['season_id']==season]
        if not group:raise ValueError('Missing development fold')
        for name in names:
            metrics.append(dict(season_id=season,model=name,**probability_metrics((r[name],r['home_win']) for r in group)))
            if season:
                for month in sorted({r['game_date'][:7] for r in group}):
                    monthly.append(dict(season_id=season,month=month,model=name,**probability_metrics((r[name],r['home_win']) for r in group if r['game_date'].startswith(month))))
                for bucket in range(10):
                    values=[r for r in group if min(9,int(r[name]*10))==bucket]
                    calibration.append(dict(season_id=season,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                        mean_probability=sum(r[name] for r in values)/len(values) if values else None,
                        observed_home_win_rate=sum(r['home_win'] for r in values)/len(values) if values else None))
        for name in ['home_rate',*candidates]:
            for reference in ('frozen_elo','baseline_context'):paired.append(compare(group,name,reference,season))
    for name,values in [('predictions',predictions),('metrics',metrics),('monthly',monthly),('calibration',calibration),('paired',paired)]:
        save_csv(output/(name+'.csv'),values)
    write_document(output/'metrics.json',metrics);write_document(output/'paired.json',paired);write_document(output/'folds.json',folds)
    return metrics,paired,predictions


def artifacts(output):return {str(p.resolve()):file_hash(p) for p in Path(output).rglob('*') if p.is_file()}


def freeze_implementation(output,*modules):
    folder=Path(output)/'implementation';folder.mkdir(parents=True,exist_ok=True)
    for module in modules:
        path=Path(module.__file__);shutil.copyfile(path,folder/path.name)
    write_document(folder/'runtime.json',{p:importlib.metadata.version(p) for p in ('numpy','scipy','scikit-learn')})


def score_table(metrics):
    lines=['| Season | Model | Games | Brier ↓ | Log loss ↓ | Accuracy |','| --- | --- | ---: | ---: | ---: | ---: |']
    for r in metrics:lines.append(f"| {r['season_id'] or 'Pooled development'} | {r['model']} | {r['n']} | {r['brier']:.6f} | {r['log_loss']:.6f} | {r['accuracy']:.2%} |")
    return '\n'.join(lines)
