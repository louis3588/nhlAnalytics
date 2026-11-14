"""Pre-registered development-only xG/goalie ablations. Production artifacts are read-only."""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from nhl_analytics.baselines import probability_metrics
from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import csv_rows, document, verify
from nhl_analytics.pregame import FEATURES, HOLDOUT
from nhl_analytics.reconstructed import MODE, file_hash
from nhl_analytics.team_feature_audit import DEFAULT_RUN, CANDIDATE
from nhl_analytics.team_models import CONFIG, chronological_folds, fit_logistic, predict_saved, paired_week_bootstrap

DEVELOPMENT=(20212022,20222023,20232024,20242025)
ADDITIONS={'context_plus_xg':CANDIDATE[:2], 'context_plus_goalie':CANDIDATE[2:]}
DEFAULT_CANDIDATE=Path('data/team_feature_audits/20261112-final')


def design():
    return dict(version='1.0.0',data_mode=MODE,role='development_only_research',
        development_seasons=list(DEVELOPMENT),evaluation_seasons=list(DEVELOPMENT[1:]),
        excluded_holdout=HOLDOUT,exclude_all_later_seasons=True,
        models={name:[*FEATURES,*extra] for name,extra in ADDITIONS.items()},
        parameters=CONFIG,baseline='Reuse original per-fold team-context artifacts, verify original training IDs',
        fitting='Expanding earlier development seasons; all training labels before first test cutoff',
        selection='Fixed ablations, no parameter search, feature selection, calibration or production promotion',
        cohorts='Identical all-game cohorts for headline results; complete-added-input cohorts reported separately',
        intervals='1000 calendar-week bootstrap replicates, paired to team-context baseline; exploratory',
        xg='5v5 last ten team games, minimum five complete games; reconstructed availability only',
        goalie='Last-known-starter team-season proxy, minimum five appearances for save rate; not confirmed target starter',
        production_models_changed=False)


def numeric(row,columns):
    for name in columns:
        value=row.get(name)
        row[name]=None if value in (None,'') else float(value)
        if row[name] is not None and not math.isfinite(row[name]):raise ValueError('Nonfinite candidate feature')
    return row


def load_inputs(model_run,candidate_dir):
    """Filter seasons BEFORE parsing values or labels from holdout/later rows."""
    original=document(model_run/'manifest.json');audit=document(candidate_dir/'summary.json')
    if audit['source_model_manifest_sha256']!=file_hash(model_run/'manifest.json'):
        raise ValueError('Candidate belongs to a different frozen comparison')
    dataset=Path(original['dataset']);manifest=document(dataset/'manifest.json')
    verify(dataset/'manifest.json',audit['source_dataset_manifest_sha256'])
    if audit.get('candidate_data_mode')!=MODE or audit.get('models_fitted') or manifest.get('feature_columns')!=FEATURES:
        raise ValueError('Invalid candidate provenance/schema')
    for name in ('pregame_features','game_labels'):
        verify(dataset/f'{name}.csv',manifest['artifacts'][name]['csv']['sha256'])
    baseline={}
    for row in csv_rows(dataset/'pregame_features.csv'):
        if int(row['season_id']) not in DEVELOPMENT:continue
        numeric(row,FEATURES+['frozen_elo_probability'])
        baseline[int(row['game_id'])]=row
    rows=[];seen=set()
    for row in csv_rows(candidate_dir/'candidate_features_reconstructed.csv'):
        if int(row['season_id']) not in DEVELOPMENT:continue
        row=numeric(row,[*FEATURES,*CANDIDATE,'frozen_elo_probability'])
        gid=int(row['game_id']);base=baseline.get(gid)
        if gid in seen or base is None:raise ValueError('Duplicate/unmatched candidate game')
        seen.add(gid)
        if any(row.get(k)!=base.get(k) for k in [*FEATURES,'prediction_at','data_mode','home_team_id','away_team_id','frozen_elo_probability']):
            raise ValueError('Candidate changed an original baseline input')
        cutoff=timestamp(row['prediction_at'])
        for side in ('home','away'):
            stamp=row.get(side+'_max_feature_available_at')
            if stamp and timestamp(stamp)>=cutoff:raise ValueError('Candidate source reaches cutoff')
            if gid in json.loads(row[side+'_source_game_ids']):raise ValueError('Target game in candidate sources')
        row.update(game_id=gid,season_id=int(row['season_id']),home_team_id=int(row['home_team_id']),away_team_id=int(row['away_team_id']))
        rows.append(row)
    if seen!=set(baseline):raise ValueError('Candidate lost development games')
    labels=[]
    for row in csv_rows(dataset/'game_labels.csv'):
        if int(row['season_id']) not in DEVELOPMENT:continue
        row.update(game_id=int(row['game_id']),season_id=int(row['season_id']),home_win=int(row['home_win']))
        labels.append(row)
    if len(rows)!=sum(len([r for r in rows if r['season_id']==s]) for s in DEVELOPMENT):
        raise ValueError('Nondevelopment row entered experiment')
    return rows,labels,manifest,original


def paired(rows,name,season):
    # Existing bootstrap accepts a named candidate and frozen_elo reference.
    # Pass a temporary view with context as reference; saved predictions remain intact.
    view=[dict(r,frozen_elo=r['baseline_context']) for r in rows]
    result=paired_week_bootstrap(view,name,seed=42+season)
    return dict(result,season_id=season,reference_model='baseline_context')


def score_cohort(rows,season,cohort,models):
    return [dict(season_id=season,cohort=cohort,model=name,
        **probability_metrics((r[name],r['home_win']) for r in rows)) for name in models] if rows else []


def evaluate(rows,labels,manifest,model_run,original,output):
    if any(r['season_id'] not in DEVELOPMENT for r in rows+labels):raise ValueError('Holdout/later seasons are forbidden')
    predictions=[];folds=[];frozen_hashes={}
    truth={r['game_id']:r['home_win'] for r in labels}
    for season,cutoff,train,test,label_map in chronological_folds(rows,labels,timestamp(manifest['as_of'])):
        if season not in DEVELOPMENT[1:] or any(r['season_id']>=season for r in train):raise ValueError('Invalid development fold')
        ids=sorted(r['game_id'] for r in train)
        train_sha=hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        name=f'models/{season}_logistic_team_context.json';path=model_run/name
        original_files={key.replace('\\','/'):value for key,value in original['files'].items()}
        verify(path,original_files[name]['sha256']);frozen_hashes[str(path)]=file_hash(path)
        baseline=document(path)
        if baseline['fold']['training_game_ids_sha256']!=train_sha or baseline['feature_columns']!=FEATURES or baseline['parameters']!=CONFIG:
            raise ValueError('Saved baseline was fitted on different training data')
        latest=max(timestamp(label_map[r['game_id']]['label_available_at']) for r in train)
        if latest>=cutoff:raise ValueError('Training label reaches evaluation period')
        fold=dict(season_id=season,training_games=len(train),test_games=len(test),prediction_start=cutoff,
            max_training_label_available_at=latest,training_game_ids_sha256=train_sha,
            training_seasons=sorted({r['season_id'] for r in train}),baseline_sha256=frozen_hashes[str(path)])
        folds.append(fold)
        probabilities={'baseline_context':predict_saved(baseline,test)}
        for candidate,extra in ADDITIONS.items():
            _,model=fit_logistic(train,truth,[*FEATURES,*extra])
            probabilities[candidate]=predict_saved(model,test)
            write_document(output/'research_models'/f'{season}_{candidate}.json',dict(model,fold=fold,role='development_only_research'))
        for i,row in enumerate(test):
            predictions.append(dict(game_id=row['game_id'],season_id=season,prediction_at=row['prediction_at'],
                game_date=row['game_date'],data_mode=MODE,home_win=truth[row['game_id']],
                frozen_elo=row['frozen_elo_probability'],**{name:p[i] for name,p in probabilities.items()},
                xg_complete=all(row[k] is not None for k in ADDITIONS['context_plus_xg']),
                goalie_complete=all(row[k] is not None for k in ADDITIONS['context_plus_goalie'])))
    metrics=[];comparisons=[];monthly=[];calibration=[]
    names=['frozen_elo','baseline_context',*ADDITIONS]
    for season in [*DEVELOPMENT[1:],0]:
        group=[r for r in predictions if not season or r['season_id']==season]
        if not group:raise ValueError('Missing development fold')
        metrics.extend(score_cohort(group,season,'all_games',names))
        for candidate,key in [('context_plus_xg','xg_complete'),('context_plus_goalie','goalie_complete')]:
            comparisons.append(dict(paired(group,candidate,season),cohort='all_games'))
            complete=[r for r in group if r[key]]
            metrics.extend(score_cohort(complete,season,'complete_'+key,names[:2]+[candidate]))
            if complete:comparisons.append(dict(paired(complete,candidate,season),cohort='complete_'+key))
        if not season:continue
        for month in sorted({r['game_date'][:7] for r in group}):
            monthly.extend(dict(r,month=month) for r in score_cohort([r for r in group if r['game_date'].startswith(month)],season,'all_games',names))
        for name in names:
            for bucket in range(10):
                values=[r for r in group if min(9,int(r[name]*10))==bucket]
                calibration.append(dict(season_id=season,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                    mean_probability=sum(r[name] for r in values)/len(values) if values else None,
                    observed_home_win_rate=sum(r['home_win'] for r in values)/len(values) if values else None))
    for name,values in [('predictions',predictions),('metrics',metrics),('paired_comparisons',comparisons),('monthly_metrics',monthly),('calibration',calibration)]:
        save_csv(output/(name+'.csv'),values)
    write_document(output/'folds.json',folds);write_document(output/'metrics.json',metrics)
    write_document(output/'paired_comparisons.json',comparisons)
    for path,sha in frozen_hashes.items():verify(Path(path),sha)
    return metrics,comparisons,folds


def run(model_run,candidate_dir,output):
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty experiment directory')
    output.mkdir(parents=True,exist_ok=True)
    inputs={str(p.resolve()):file_hash(p) for p in [model_run/'manifest.json',candidate_dir/'summary.json',candidate_dir/'candidate_features_reconstructed.csv']}
    plan=dict(design(),registered_at=datetime.now(timezone.utc),input_sha256=inputs,
        implementation_sha256=file_hash(Path(__file__)))
    write_document(output/'experiment_plan.json',plan)  # Persist BEFORE reading labels or fitting.
    rows,labels,manifest,original=load_inputs(model_run,candidate_dir)
    metrics,comparisons,folds=evaluate(rows,labels,manifest,model_run,original,output)
    for path,sha in inputs.items():verify(Path(path),sha)
    summary=dict(status='complete',data_mode=MODE,role='development_only_research',production_changed=False,
        holdout_scored=False,holdout_used_for_fitting=False,development_seasons=list(DEVELOPMENT),
        evaluation_seasons=[f['season_id'] for f in folds],source_rows=len(rows),evaluation_games=sum(f['test_games'] for f in folds),
        original_models_reused=True,plan_sha256=file_hash(output/'experiment_plan.json'),
        files={p.relative_to(output).as_posix():dict(sha256=file_hash(p)) for p in output.rglob('*') if p.is_file()})
    write_document(output/'manifest.json',summary)
    lines=['# Development-only feature ablations','',
        'Production models remain frozen. No 2025–26 holdout or 2026–27 monitoring outcomes were fitted, scored or used for selection.',
        'Settings and feature groups were fixed before fitting. Earlier seasons train each fold; original context baseline artifacts are reused.',
        'Historical data is reconstructed with assumed next-day availability, not verified historical publication times. Frozen Elo parameters were previously selected across development seasons; these development comparisons are retrospective.',
        '', '| Season | Model | Games | Brier ↓ | Log loss ↓ | Accuracy |','| --- | --- | ---: | ---: | ---: | ---: |']
    for r in metrics:
        if r['cohort']=='all_games':lines.append(f"| {r['season_id'] or 'Pooled development'} | {r['model']} | {r['n']} | {r['brier']:.5f} | {r['log_loss']:.5f} | {r['accuracy']:.1%} |")
    lines.extend(['','## Paired additions versus context baseline','',
        'Negative deltas favour the addition. Calendar-week bootstrap intervals are exploratory, conditional on these fitted models; they do not cover all training or team dependence.',
        '', '| Season | Addition | Brier delta | Exploratory 95% interval |','| --- | --- | ---: | --- |'])
    for r in comparisons:
        if r['cohort']=='all_games':
            interval=f"[{r['brier_lower']:.5f}, {r['brier_upper']:.5f}]" if r['brier_lower'] is not None else 'Insufficient weeks'
            lines.append(f"| {r['season_id'] or 'Pooled development'} | {r['model']} | {r['brier_delta']:+.5f} | {interval} |")
    lines.extend(['','## Coverage and interpretation','',
        'Headline comparisons use identical games, including unavailable added features with training-only imputation and missing indicators. Complete-added-input cohorts are secondary diagnostics in metrics.csv; they are not a replacement headline cohort.',
        'Goalie identity is a last-known-starter team-season proxy, never the actual target starter. xG source publication times are reconstructed; daily observed downloads are separate and do not rewrite this pinned experiment.',
        'No combined candidate, parameter search, automated winner selection, holdout evaluation, production fitting or promotion was performed.',
        'Inspect monthly/calibration/complete-input cohorts before proposing a new fixed experiment. Development improvements alone do not establish prospective superiority.',''])
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-run',type=Path,default=DEFAULT_RUN)
    parser.add_argument('--candidate',type=Path,default=DEFAULT_CANDIDATE)
    parser.add_argument('--output',type=Path,default=Path('data/team_ablations/development')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();print(json.dumps(run(args.model_run,args.candidate,args.output),indent=2))


if __name__=='__main__':main()
