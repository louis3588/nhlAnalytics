"""Chronological pregame logistic-regression comparison against frozen Elo."""

import argparse
import importlib.metadata
import hashlib
import json
import math
import os
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nhl_analytics.baselines import probability_metrics
from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import csv_rows, discover_run, document, verify
from nhl_analytics.pregame import FEATURES, HOLDOUT, VERSION, aggregate_players, build_dataset, read_sources
from nhl_analytics.reconstructed import MODE, file_hash

CONFIG = dict(C=1.0, solver='lbfgs', max_iter=1000, tol=1e-7, random_state=42)
MODELS = {'logistic_elo_only':['elo_log_odds'], 'logistic_team_context':FEATURES}


def plan():
    return dict(version=VERSION,data_mode=MODE,reserved_holdout=HOLDOUT,features=FEATURES,models=MODELS,
        implementation_sha256=file_hash(Path(__file__)),feature_implementation_sha256=file_hash(Path(__file__).with_name('pregame.py')),
        logistic_parameters=CONFIG,cutoff='One hour before scheduled puck drop; matching frozen Elo',
        availability='Historical next-day noon America/New_York assumption; latest corrected fixtures, not verified historical publication times',
        fitting='Expanding earlier seasons; training labels strictly before first test cutoff; exclude 2025–26 from all fitting',
        preprocessing='Training-only mean imputation, missing indicators, standardisation; L2 logistic regression',
        tuning='No search, feature selection, threshold optimisation or probability calibration; settings fixed before evaluation',
        uncertainty='Paired calendar-week block bootstrap; 1000 replicates; seed=42+season; conditional exploratory intervals',
        deployment='Comparison only; no model promotion, API changes or replacement of original forecasts')


def write_table(output, name, rows):
    import pyarrow as pa
    import pyarrow.parquet as pq
    csv = save_csv(output/f'{name}.csv', rows)
    fields=[]
    converted=[]
    for key in rows[0]:
        kind=pa.string()
        if key in FEATURES or key.endswith('_probability'):
            kind=pa.float64()
        elif key.endswith('_id') or key=='home_win':
            kind=pa.int64()
        elif key=='prediction_at' or key.endswith('_available_at'):
            kind=pa.timestamp('us',tz='UTC')
        elif key=='game_date':
            kind=pa.date32()
        fields.append(pa.field(key,kind))
    for row in rows:
        value=dict(row)
        for key in value:
            if value[key] is not None and (key=='prediction_at' or key.endswith('_available_at')):
                value[key]=timestamp(value[key])
            elif key=='game_date':
                value[key]=date.fromisoformat(value[key])
        converted.append(value)
    table=pa.Table.from_pylist(converted,schema=pa.schema(fields).with_metadata({b'data_mode':MODE.encode(),b'version':VERSION.encode()}))
    path=output/f'{name}.parquet'
    pq.write_table(table,path,compression='zstd')
    return dict(csv=csv,parquet=dict(rows=len(rows),sha256=file_hash(path)))


def create_dataset(evaluation,output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new empty dataset directory')
    output.mkdir(parents=True,exist_ok=True)
    games,elo,history,provenance=read_sources(evaluation)
    aggregates,coverage=aggregate_players(history,games,provenance['source'])
    rows,labels=build_dataset(games,elo,aggregates,provenance['as_of'])
    if not rows or not labels:
        raise ValueError('No eligible historical pregame features/labels')
    artifacts={name:write_table(output,name,items) for name,items in [('pregame_features',rows),('game_labels',labels)]}
    manifest=dict(status='complete',version=VERSION,data_mode=MODE,as_of=provenance['as_of'],evaluation=str(Path(evaluation).resolve()),
        original_files=provenance['files'],frozen_elo_parameters=provenance['model']['parameters'],
        completed_seasons=provenance['baseline']['completed_seasons'],feature_columns=FEATURES,artifacts=artifacts,coverage=coverage,
        audit=dict(feature_rows=len(rows),label_rows=len(labels),unique_games=len({r['game_id'] for r in rows}),
            all_stat_inputs_strictly_before_cutoff=True,actual_historical_point_in_time_verified=False,
            missing_values={name:sum(r[name] is None for r in rows) for name in FEATURES}),
        limitations=['Latest corrected historical fixtures/results with assumed availability; not strict observed-time data.',
            'Frozen Elo parameters were previously selected on development seasons; earlier development comparisons are retrospective.',
            'No actual starting-goalie identity, current roster/injury information, season-final standings, live-game states or target-game stats enter features.',
            'Shot and goalie rates use available NHL game logs; xG and special-teams splits are deferred until historical coverage/timing is audited.',
            'Scheduled regular-season starts approximate rest/workload; no historical injury or preseason workload reconstruction.'])
    for path,sha in provenance['files'].items():verify(Path(path),sha)
    write_document(output/'manifest.json',manifest)
    return rows,labels,manifest


def read_dataset(path):
    manifest=document(path/'manifest.json')
    if manifest.get('status')!='complete' or manifest.get('data_mode')!=MODE or manifest.get('feature_columns')!=FEATURES:
        raise ValueError('Expected a complete reconstructed pregame dataset with the current feature schema')
    results=[]
    for name in ('pregame_features','game_labels'):
        verify(path/f'{name}.csv',manifest['artifacts'][name]['csv']['sha256'])
        verify(path/f'{name}.parquet',manifest['artifacts'][name]['parquet']['sha256'])
        rows=list(csv_rows(path/f'{name}.csv'))
        if len(rows)!=manifest['artifacts'][name]['csv']['rows']:
            raise ValueError('Dataset row count mismatch')
        for row in rows:
            for key in ('game_id','season_id','home_team_id','away_team_id','home_win'):
                if key in row:row[key]=int(row[key])
            for key in FEATURES+['frozen_elo_probability','historical_home_probability']:
                if key in row:row[key]=float(row[key]) if row[key] else None
        results.append(rows)
    for source,sha in manifest['original_files'].items():verify(Path(source),sha)
    return *results,manifest


def matrix(rows,columns):
    import numpy as np
    values=np.asarray([[np.nan if row.get(key) in (None,'') else float(row[key]) for key in columns] for row in rows],dtype='float64')
    if np.isinf(values).any():raise ValueError('Infinite model feature')
    return values


def fit_logistic(rows,labels,columns):
    import numpy as np
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits
    if len({labels[r['game_id']] for r in rows})<2:
        raise ValueError('Training requires both game outcomes')
    pipeline=make_pipeline(SimpleImputer(strategy='mean',add_indicator=True,keep_empty_features=True),StandardScaler(),LogisticRegression(**CONFIG))
    with threadpool_limits(limits=1),warnings.catch_warnings():
        warnings.simplefilter('error',ConvergenceWarning)
        pipeline.fit(matrix(rows,columns),np.asarray([labels[r['game_id']] for r in rows]))
    imputer,scaler,classifier=pipeline.steps[0][1],pipeline.steps[1][1],pipeline.steps[2][1]
    model=dict(version=VERSION,data_mode=MODE,parameters=CONFIG,feature_columns=columns,
        transformed_columns=list(imputer.get_feature_names_out(columns)),imputer_statistics=imputer.statistics_.tolist(),
        missing_indicator_indices=imputer.indicator_.features_.tolist(),scaler_mean=scaler.mean_.tolist(),scaler_scale=scaler.scale_.tolist(),
        coefficients=classifier.coef_[0].tolist(),intercept=float(classifier.intercept_[0]),iterations=classifier.n_iter_.tolist())
    return pipeline,model


def predict_saved(model,rows):
    """Portable numeric model artifact, without loading untrusted pickle objects."""
    import numpy as np
    from threadpoolctl import threadpool_limits
    if not rows:return []
    raw=matrix(rows,model['feature_columns']);missing=np.isnan(raw)
    filled=np.where(missing,np.asarray(model['imputer_statistics']),raw)
    indicators=missing[:,model['missing_indicator_indices']].astype(float)
    transformed=np.concatenate([filled,indicators],axis=1)
    scaled=(transformed-np.asarray(model['scaler_mean']))/np.asarray(model['scaler_scale'])
    with threadpool_limits(limits=1):
        z=scaled@np.asarray(model['coefficients'])+model['intercept']
    return (1/(1+np.exp(-np.clip(z,-709,709)))).tolist()


def chronological_folds(rows,labels,as_of):
    feature_ids={r['game_id'] for r in rows}
    if len(feature_ids)!=len(rows):raise ValueError('Duplicate feature game IDs')
    seen=set();truth={}
    for label in labels:
        gid=label['game_id']
        if gid in seen or gid not in feature_ids or label.get('data_mode')!=MODE or label['home_win'] not in (0,1):raise ValueError('Invalid/duplicate outcome label')
        seen.add(gid)
        if timestamp(label['label_available_at'])<as_of:truth[gid]=label
    for row in rows:
        stamp=row.get('max_assumed_feature_available_at')
        if row.get('data_mode')!=MODE or stamp and timestamp(stamp)>=timestamp(row['prediction_at']):
            raise ValueError('Feature availability reaches pregame cutoff')
        if row['game_id'] in truth and timestamp(truth[row['game_id']]['label_available_at'])<=timestamp(row['prediction_at']):
            raise ValueError('Target label was available before its own game')
    seasons=sorted({r['season_id'] for r in rows})
    for season in seasons[1:]:
        test=[r for r in rows if r['season_id']==season and r['game_id'] in truth]
        if not test:continue
        cutoff=min(timestamp(r['prediction_at']) for r in rows if r['season_id']==season)
        train=[r for r in rows if r['season_id']<season and r['season_id']!=HOLDOUT and r['game_id'] in truth
            and timestamp(truth[r['game_id']]['label_available_at'])<cutoff]
        if not train:continue
        yield season,cutoff,train,test,truth


def paired_week_bootstrap(rows,model,seed=42,replicates=1000):
    import numpy as np
    blocks=defaultdict(list)
    for row in rows:
        day=date.fromisoformat(row['game_date']);week=day-timedelta(days=day.weekday())
        y=row['home_win'];a=row[model];b=row['frozen_elo']
        blocks[week].append(((a-y)**2-(b-y)**2,
            -(y*math.log(max(a,1e-15))+(1-y)*math.log(max(1-a,1e-15)))
            +(y*math.log(max(b,1e-15))+(1-y)*math.log(max(1-b,1e-15)))))
    n=len(rows)
    result=dict(n=n,weeks=len(blocks),replicates=replicates,model=model,
        brier_delta=sum(v[0] for block in blocks.values() for v in block)/n,
        log_loss_delta=sum(v[1] for block in blocks.values() for v in block)/n,
        brier_lower=None,brier_upper=None,log_loss_lower=None,log_loss_upper=None)
    if len(blocks)<10:return result
    block=np.asarray([[len(values),sum(v[0] for v in values),sum(v[1] for v in values)] for values in blocks.values()])
    rng=np.random.default_rng(seed);values=[]
    for _ in range(replicates):
        sums=block[rng.integers(0,len(block),size=len(block))].sum(axis=0)
        values.append(sums[1:]/sums[0])
    bounds=np.quantile(values,[0.025,0.975],axis=0)
    result.update(brier_lower=float(bounds[0,0]),brier_upper=float(bounds[1,0]),log_loss_lower=float(bounds[0,1]),log_loss_upper=float(bounds[1,1]))
    return result


def evaluate(rows,labels,output,as_of,completed):
    output.mkdir(parents=True,exist_ok=True)
    truth_values={r['game_id']:r['home_win'] for r in labels}
    predictions=[];folds=[];coefficients=[];artifacts={};fitted={}
    for season,cutoff,train,test,truth in chronological_folds(rows,labels,as_of):
        role='reserved_previously_inspected_benchmark' if season==HOLDOUT else 'development_retrospective' if season in completed else 'partial_reconstructed_monitoring'
        train_ids=sorted(r['game_id'] for r in train)
        train_hash=hashlib.sha256(json.dumps(train_ids).encode()).hexdigest()
        latest=max(timestamp(truth[r['game_id']]['label_available_at']) for r in train)
        fold=dict(season_id=season,role=role,prediction_start=cutoff,training_games=len(train),test_games=len(test),
            training_seasons=sorted({r['season_id'] for r in train}),training_game_ids_sha256=train_hash,max_training_label_available_at=latest)
        folds.append(fold)
        probabilities={}
        for name,columns in MODELS.items():
            key=name,train_hash
            if key not in fitted:fitted[key]=fit_logistic(train,truth_values,columns)
            pipeline,model=fitted[key]
            probabilities[name]=predict_saved(model,test)
            from threadpoolctl import threadpool_limits
            import numpy as np
            with threadpool_limits(limits=1):
                reference=pipeline.predict_proba(matrix(test,columns))[:,1]
            if not np.allclose(reference,probabilities[name],rtol=0,atol=1e-12):
                raise ValueError('Portable model predictions differ from fitted pipeline')
            saved=dict(model,fold=fold,versions={p:importlib.metadata.version(p) for p in ('scikit-learn','numpy','scipy')})
            path=output/'models'/f'{season}_{name}.json';write_document(path,saved)
            artifacts[str(path.relative_to(output))]=dict(sha256=file_hash(path))
            coefficients.extend(dict(season_id=season,model=name,feature=feature,coefficient=coef,odds_ratio_per_standard_deviation=math.exp(coef))
                for feature,coef in zip(model['transformed_columns'],model['coefficients']))
        for index,row in enumerate(test):
            predictions.append(dict(game_id=row['game_id'],season_id=season,prediction_at=row['prediction_at'],data_mode=MODE,
                role=role,game_date=row['game_date'],home_team_id=row['home_team_id'],away_team_id=row['away_team_id'],
                home_win=truth[row['game_id']]['home_win'],label_available_at=truth[row['game_id']]['label_available_at'],
                max_assumed_feature_available_at=row['max_assumed_feature_available_at'],
                frozen_elo=row['frozen_elo_probability'],historical_home_rate=row['historical_home_probability'],
                **{name:probabilities[name][index] for name in MODELS}))
    metrics=[];calibration=[];paired=[];monthly=[]
    names=['frozen_elo','historical_home_rate',*MODELS]
    for season in sorted({p['season_id'] for p in predictions}):
        cohort=[r for r in predictions if r['season_id']==season]
        for name in names:
            metrics.append(dict(season_id=season,role=cohort[0]['role'],model=name,**probability_metrics((r[name],r['home_win']) for r in cohort)))
            for month in sorted({r['game_date'][:7] for r in cohort}):
                monthly.append(dict(season_id=season,month=month,model=name,**probability_metrics((r[name],r['home_win']) for r in cohort if r['game_date'].startswith(month))))
            for bucket in range(10):
                values=[r for r in cohort if min(9,int(r[name]*10))==bucket]
                calibration.append(dict(season_id=season,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                    mean_probability=sum(r[name] for r in values)/len(values) if values else None,
                    observed_home_win_rate=sum(r['home_win'] for r in values)/len(values) if values else None))
        for name in MODELS:
            paired.append(dict(season_id=season,**paired_week_bootstrap(cohort,name,seed=42+season)))
    if not predictions:raise ValueError('No eligible chronological evaluation folds')
    for name,values in [('predictions',predictions),('metrics',metrics),('monthly_metrics',monthly),('calibration',calibration),('paired_comparisons',paired),('coefficients',coefficients)]:
        artifacts[name+'.csv']=save_csv(output/(name+'.csv'),values)
    write_document(output/'folds.json',folds)
    write_document(output/'metrics.json',metrics)
    write_document(output/'paired_comparisons.json',paired)
    render_report(output,metrics,paired,folds)
    return dict(artifacts=artifacts,folds=folds,metrics=metrics,paired=paired)


def render_report(output,metrics,paired,folds):
    lines=['# Pregame logistic regression versus frozen Elo','',
        'Reconstructed historical evaluation, not verified historical point-in-time observations. No production model was replaced.',
        'All models are compared on identical games at one hour before puck drop. Logistic settings/features were fixed before scoring; no holdout tuning.',
        'Earlier development folds are retrospective: frozen Elo parameters were previously selected using those development seasons. The 2025–26 benchmark has already been inspected and is not pristine unseen data.',
        '', '| Season | Role | Model | Games | Brier ↓ | Log loss ↓ | Accuracy |','| --- | --- | --- | ---: | ---: | ---: | ---: |']
    for row in metrics:
        lines.append(f"| {row['season_id']} | {row['role']} | {row['model']} | {row['n']} | {row['brier']:.5f} | {row['log_loss']:.5f} | {row['accuracy']:.1%} |")
    lines.extend(['','## Paired comparison with frozen Elo','',
        'Negative deltas favour logistic regression. Intervals resample calendar-week blocks, conditional on the fitted models; they do not cover training uncertainty or all cross-week/team dependence.',
        '', '| Season | Model | Brier delta | Exploratory 95% interval | Weeks |','| --- | --- | ---: | --- | ---: |'])
    for row in paired:
        interval=f"[{row['brier_lower']:.5f}, {row['brier_upper']:.5f}]" if row['brier_lower'] is not None else 'Insufficient week blocks'
        lines.append(f"| {row['season_id']} | {row['model']} | {row['brier_delta']:+.5f} | {interval} | {row['weeks']} |")
    lines.extend(['','## Chronology and limitations','',
        '- Imputation, missing indicators, scaling and coefficients fit only earlier eligible training games; all their labels predate the test season’s first forecast.',
        '- 2025–26 labels are excluded from every fit, including partial 2026–27 monitoring. Earlier results can still update later feature state, as in sequential operation.',
        '- Missing shots/goaltending history is retained as missing. No actual target-game starter, lineup, injuries, live stats, final standings or target outcomes are features.',
        '- Rest uses earlier scheduled regular-season starts, even if last night’s result is not yet assumed published. Historical schedule revisions remain reconstructed.',
        '- Last-ten windows reset each season; prior-season rates and frozen Elo provide separate carryover. Arizona/Utah continuity uses the existing franchise map.',
        '- xG/special-teams features are deferred rather than backfilling season-final aggregates or scanning the full shot archive.',
        '- Training settings are fixed C=1 with L2 regularisation; no threshold tuning or post-hoc calibration. Better results are not guaranteed.',
        '- 2026–27 results are partial retrospective monitoring from the frozen source clock, not newly sealed prospective predictions.',
        '- Reports do not select/deploy a winner. Freeze the next candidate before scoring genuinely future games.',
        '', 'See `dataset/manifest.json`, `folds.json`, calibration/monthly tables, paired comparisons and portable coefficient artifacts for audit details.',''])
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')


def run_experiment(evaluation,output,dataset=None,features_only=False):
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty experiment directory')
    output.mkdir(parents=True,exist_ok=True)
    write_document(output/'experiment_plan.json',plan())
    if dataset:
        rows,labels,source=read_dataset(dataset)
        dataset_path=dataset.resolve()
    else:
        dataset_path=output/'dataset'
        rows,labels,source=create_dataset(evaluation,dataset_path)
    as_of=timestamp(source['as_of']) if isinstance(source['as_of'],str) else source['as_of']
    result={} if features_only else evaluate(rows,labels,output,as_of,source['completed_seasons'])
    for path,sha in source['original_files'].items():verify(Path(path),sha)
    manifest=dict(status='complete',version=VERSION,data_mode=MODE,as_of=as_of,generated_at=datetime.now(timezone.utc),
        dataset=str(dataset_path.resolve()),dataset_manifest_sha256=file_hash(dataset_path/'manifest.json'),
        experiment_plan_sha256=file_hash(output/'experiment_plan.json'),features_only=features_only,
        frozen_elo_parameters=source['frozen_elo_parameters'],production_changed=False,
        files={str(p.relative_to(output)):dict(sha256=file_hash(p)) for p in output.rglob('*') if p.is_file()},
        summary=dict(feature_rows=len(rows),scored_seasons=[r['season_id'] for r in result.get('folds',[])]))
    write_document(output/'manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation',type=Path,help='Frozen improved Elo evaluation; defaults to latest complete evaluation')
    parser.add_argument('--output',type=Path,default=Path('data/team_models/reconstructed'))
    parser.add_argument('--dataset',type=Path,help='Reuse a complete pregame dataset without rebuilding source aggregates')
    parser.add_argument('--features-only',action='store_true')
    args=parser.parse_args()
    os.environ.setdefault('OMP_NUM_THREADS','1');os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    try:
        manifest=run_experiment(args.evaluation or discover_run(),output,args.dataset,args.features_only)
    except Exception as error:
        if output.exists() and not (output/'manifest.json').exists():
            write_document(output/'manifest.json',dict(status='failed',error=type(error).__name__,message=str(error)))
        raise
    print(json.dumps(dict(output=str(output),**manifest['summary']),indent=2))


if __name__=='__main__':main()
