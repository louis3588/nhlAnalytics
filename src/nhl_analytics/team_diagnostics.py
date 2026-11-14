"""Development-only explanations of fixed ablations. No fitting, tuning or promotion."""
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

import pyarrow.parquet as pq

from nhl_analytics.baselines import probability_metrics
from nhl_analytics.database import timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prediction_store import csv_rows, document, verify
from nhl_analytics.pregame import read_sources
from nhl_analytics.reconstructed import file_hash
from nhl_analytics.team_ablation import ADDITIONS, DEVELOPMENT, load_inputs
from nhl_analytics.team_feature_audit import DEFAULT_RUN

EXPERIMENT=Path('data/team_ablations/development/20261112-xg-goalie-v2')
CANDIDATE=Path('data/team_feature_audits/20261112-final')


def correlation(pairs):
    values=[(float(a),float(b)) for a,b in pairs if a is not None and b is not None]
    if not values:return dict(n=0,pearson=None)
    a,b=zip(*values);sa,sb=pstdev(a),pstdev(b);ma,mb=mean(a),mean(b)
    return dict(n=len(values),pearson=mean((x-ma)*(y-mb) for x,y in values)/(sa*sb) if sa and sb else None)


def proxy_label(feature,actual):
    """Postgame diagnostic label only. Never inserted into prediction features."""
    values=[]
    for side in ('home','away'):
        value=feature.get(side+'_proxy_goalie_id')
        proxy=int(value) if value not in ('',None) else None
        starters=actual.get((feature['game_id'],side),set())
        values.append(None if proxy is None or len(starters)!=1 else proxy in starters)
    return 'unknown' if None in values else 'both_correct' if all(values) else 'at_least_one_wrong'


def load_predictions(path):
    rows=list(csv_rows(path))
    for row in rows:
        row.update(game_id=int(row['game_id']),season_id=int(row['season_id']),home_win=int(row['home_win']))
        if row['season_id'] not in DEVELOPMENT[1:]:raise ValueError('Nondevelopment prediction in diagnostics')
        for name in ('baseline_context','frozen_elo',*ADDITIONS):
            row[name]=float(row[name])
            if not 0<row[name]<1:raise ValueError('Invalid saved probability')
    return rows


def cohort_metrics(predictions,features,proxy):
    groups=defaultdict(list)
    for row in predictions:
        feature=features[row['game_id']]
        for candidate,extra in ADDITIONS.items():
            period='first_10_team_games' if min(feature['home_season_games'],feature['away_season_games'])<10 else 'established_season'
            complete='complete_inputs' if all(feature[k] is not None for k in extra) else 'missing_added_input'
            for dimension,group in [('season_stage',period),('input_coverage',complete),('goalie_proxy_match',proxy[row['game_id']])]:
                if dimension=='goalie_proxy_match' and candidate!='context_plus_goalie':continue
                groups[candidate,dimension,group].append(row)
    output=[]
    for (candidate,dimension,group),rows in sorted(groups.items()):
        baseline=probability_metrics((r['baseline_context'],r['home_win']) for r in rows)
        result=probability_metrics((r[candidate],r['home_win']) for r in rows)
        output.append(dict(candidate=candidate,dimension=dimension,group=group,n=len(rows),
            baseline_brier=baseline['brier'],candidate_brier=result['brier'],brier_delta=result['brier']-baseline['brier'],
            baseline_log_loss=baseline['log_loss'],candidate_log_loss=result['log_loss'],
            baseline_accuracy=baseline['accuracy'],candidate_accuracy=result['accuracy']))
    return output


def run(experiment,candidate_dir,model_run,output):
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new empty diagnostic directory')
    output.mkdir(parents=True,exist_ok=True)
    manifest=document(experiment/'manifest.json')
    if manifest.get('role')!='development_only_research' or manifest.get('holdout_scored') or manifest.get('production_changed'):
        raise ValueError('Expected development-only unchanged-production experiment')
    for name,info in manifest['files'].items():verify(experiment/name,info['sha256'])
    plan=document(experiment/'experiment_plan.json')
    for path,sha in plan['input_sha256'].items():verify(Path(path),sha)
    rows,_,source,_=load_inputs(model_run,candidate_dir)
    predictions=load_predictions(experiment/'predictions.csv')
    if len(predictions)!=manifest['evaluation_games']:raise ValueError('Prediction count mismatch')
    ids={r['game_id'] for r in predictions};features={r['game_id']:r for r in rows if r['game_id'] in ids}
    if len(ids)!=len(predictions) or set(features)!=ids:raise ValueError('Prediction/feature cohort mismatch')
    games,_,history,_=read_sources(Path(source['evaluation']))
    game_map={g['game_id']:g for g in games};actual=defaultdict(set)
    for batch in pq.ParquetFile(history/'goalies.parquet').iter_batches(batch_size=1000,
            columns=['game_id','player_id','games_started','home_road','team_abbrev'],use_threads=False):
        for row in batch.to_pylist():
            if row['game_id'] not in ids or row['games_started']!=1:continue
            side='home' if row['home_road']=='H' else 'away'
            if row['team_abbrev']!=game_map[row['game_id']][side+'_team_abbrev']:raise ValueError('Starter/game side mismatch')
            actual[row['game_id'],side].add(row['player_id'])
    proxy={gid:proxy_label(feature,actual) for gid,feature in features.items()}
    matches=[]
    for season in DEVELOPMENT[1:]:
        for side in ('home','away'):
            known=correct=unavailable=0
            for feature in features.values():
                if feature['season_id']!=season:continue
                value=feature.get(side+'_proxy_goalie_id');starters=actual.get((feature['game_id'],side),set())
                if value in (None,'') or len(starters)!=1:unavailable+=1;continue
                known+=1;correct+=int(int(value) in starters)
            matches.append(dict(season_id=season,side=side,known=known,correct=correct,unavailable=unavailable,
                match_rate=correct/known if known else None,diagnostic_only=True))
    relations=[('last10_xg_share_diff','last10_shot_share_diff'),
        ('last10_xg_diff_per_game_diff','last10_goal_diff_per_game_diff'),
        ('last10_xg_share_diff','last10_xg_diff_per_game_diff'),
        ('proxy_goalie_last10_save_pct_diff','last10_save_pct_diff'),
        ('proxy_goalie_last7_minutes_diff','games_last7_diff')]
    correlations=[]
    for season in [*DEVELOPMENT[1:],0]:
        cohort=[f for f in features.values() if not season or f['season_id']==season]
        for a,b in relations:correlations.append(dict(season_id=season,feature=a,comparison_feature=b,
            **correlation((f[a],f[b]) for f in cohort)))
    distributions=[];coefficients=[]
    folds=document(experiment/'folds.json')
    for fold in folds:
        season=fold['season_id']
        train=[r for r in rows if r['season_id']<season]
        test=[f for f in features.values() if f['season_id']==season]
        for candidate,extra in ADDITIONS.items():
            model=document(experiment/'research_models'/f'{season}_{candidate}.json')
            for key,coef in zip(model['transformed_columns'],model['coefficients']):
                if key in extra or key.startswith('missingindicator_') and key.removeprefix('missingindicator_') in extra:
                    coefficients.append(dict(season_id=season,candidate=candidate,feature=key,standardised_coefficient=coef))
            for name in extra:
                a=[f[name] for f in train if f[name] is not None];b=[f[name] for f in test if f[name] is not None]
                sd=pstdev(a) if a else None
                distributions.append(dict(season_id=season,candidate=candidate,feature=name,
                    train_n=len(a),test_n=len(b),train_missing_pct=100*(1-len(a)/len(train)),test_missing_pct=100*(1-len(b)/len(test)),
                    train_mean=mean(a) if a else None,test_mean=mean(b) if b else None,
                    mean_shift_train_sd=(mean(b)-mean(a))/sd if a and b and sd else None))
    cohorts=cohort_metrics(predictions,features,proxy)
    extremes=[]
    for candidate in ADDITIONS:
        enriched=[dict(game_id=r['game_id'],season_id=r['season_id'],game_date=r['game_date'],candidate=candidate,
            baseline_probability=r['baseline_context'],candidate_probability=r[candidate],home_win=r['home_win'],
            brier_delta=(r[candidate]-r['home_win'])**2-(r['baseline_context']-r['home_win'])**2,
            goalie_proxy_postgame_match=proxy[r['game_id']]) for r in predictions]
        for label,group in [('most_harmful',sorted(enriched,key=lambda r:r['brier_delta'],reverse=True)[:20]),
                            ('most_helpful',sorted(enriched,key=lambda r:r['brier_delta'])[:20])]:
            extremes.extend(dict(r,diagnostic_group=label) for r in group)
    for name,values in [('proxy_postgame_match',matches),('feature_correlations',correlations),('cohort_metrics',cohorts),
                        ('feature_distributions',distributions),('added_coefficients',coefficients),('extreme_changes',extremes)]:
        save_csv(output/(name+'.csv'),values)
    known=sum(r['known'] for r in matches);correct=sum(r['correct'] for r in matches)
    summary=dict(status='complete',generated_at=datetime.now(timezone.utc),evaluation_games=len(predictions),
        production_changed=False,models_fitted=False,holdout_used=False,goalie_proxy_known_team_games=known,
        goalie_proxy_correct_team_games=correct,goalie_proxy_match_rate=correct/known if known else None,
        both_goalie_proxies_correct_games=sum(v=='both_correct' for v in proxy.values()),
        experiment_manifest_sha256=file_hash(experiment/'manifest.json'),implementation_sha256=file_hash(Path(__file__)),
        postgame_starters_used_as_diagnostic_labels_only=True)
    write_document(output/'summary.json',summary)
    match_text=f'{correct/known:.1%}' if known else 'unavailable'
    lines=['# Development feature diagnostics','',
        f'Examined {len(predictions):,} saved development predictions. No models were refitted; no holdout or prospective outcomes were used.',
        f'The last-known-starter proxy matched the actual postgame-labelled starter in {correct:,}/{known:,} known team-games ({match_text}). Both sides matched in {summary["both_goalie_proxies_correct_games"]:,} games.',
        'Actual starters are diagnostic labels only. They were not inserted into feature datasets, forecasts or model fitting.','',
        '## Redundancy','', '| Added feature | Existing/other signal | Paired rows | Pearson r |','| --- | --- | ---: | ---: |']
    for r in correlations:
        if not r['season_id']:lines.append(f"| {r['feature']} | {r['comparison_feature']} | {r['n']} | {r['pearson']:.3f} |" if r['pearson'] is not None else f"| {r['feature']} | {r['comparison_feature']} | {r['n']} | unavailable |")
    lines.extend(['','## Where errors change','',
        'These are exploratory matched-game cohorts, not a new winner-selection rule. Positive Brier delta is worse than context.',
        '', '| Candidate | Diagnostic | Group | Games | Brier delta |','| --- | --- | --- | ---: | ---: |'])
    for r in cohorts:lines.append(f"| {r['candidate']} | {r['dimension']} | {r['group']} | {r['n']} | {r['brier_delta']:+.6f} |")
    lines.extend(['','## What these checks can and cannot explain','',
        '- Complete-input cohort results in the original experiment were still slightly worse, so missingness alone does not explain the result.',
        '- Strong correlation can indicate redundant signals; it does not prove redundancy caused the score change. Added coefficients are conditional weights, not causal feature importance.',
        '- Proxy/starter mismatches make goalie context about the wrong potential participant. Match-stratified scores diagnose the limitation without giving an oracle starter to a model.',
        '- Current MoneyPuck freshness and historical predictive signal are separate questions: development xG came from a pinned historical snapshot, so today\'s download lag did not create the development result.',
        '- Distribution and missingness tables compare earlier training seasons with each evaluation season. Any change is descriptive, not a tuned preprocessing choice.',
        '- Calendar-week intervals in the original study remain exploratory. Small gains in individual seasons do not justify promotion.','',
        '## Suggested next fixed experiments','',
        '1. Keep production unchanged and continue genuine prospective tracking.',
        '2. For xG, pre-register a longer-horizon, prior-season-shrunk team-strength feature alongside a recent deviation. Compare one fixed design against context; do not search window lengths on the benchmark.',
        '3. For goalies, first improve the pregame selection representation: a timestamped confirmed starter if available before cutoff, or a past-only weighted roster/rotation mixture. Add prior-history shrinkage for save rates rather than relying only on short team-season samples.',
        '4. Require observed source coverage and replay tests before a new candidate campaign. Evaluate only on development seasons until a candidate is frozen, then use future games.',
        'These are hypotheses for separately pre-registered work. No new parameters, models, selection thresholds or production outputs changed here.',''])
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path,default=EXPERIMENT)
    parser.add_argument('--candidate',type=Path,default=CANDIDATE)
    parser.add_argument('--model-run',type=Path,default=DEFAULT_RUN)
    parser.add_argument('--output',type=Path,default=Path('data/team_diagnostics/development')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();print(json.dumps(run(args.experiment,args.candidate,args.model_run,args.output),indent=2,default=str))


if __name__=='__main__':main()
