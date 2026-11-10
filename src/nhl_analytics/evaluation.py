"""Additive reconstructed diagnostics, development-only Elo selection and uncertainty."""

import argparse
import heapq
import importlib.metadata
import json
import sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow as pa

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.baselines import FRANCHISE_MAP,PARAMETERS,elo_predictions
from nhl_analytics.database import timestamp
from nhl_analytics.elo_development import candidate_grid,contextualize,select_elo
from nhl_analytics.evaluation_support import Metrics,horizon,player_values,read_artifacts,save_csv,slices
from nhl_analytics.features import write_document
from nhl_analytics.game_logs import validate_log
from nhl_analytics.reconstructed import MODE,file_hash
from nhl_analytics.uncertainty import Coverage,MODELS,NOMINALS,TARGETS,fit_pools,interval

VERSION='1.0.0'
HOLDOUT=20252026


def player_reports(artifacts,label_available):
    metrics=defaultdict(Metrics)
    extremes=defaultdict(list)
    serial=0
    for key,label in artifacts['player_labels'].items():
        player,season,_=key
        row=artifacts['player_inputs'][key]
        prediction=artifacts['player_predictions'][key]
        prior='yes' if int(row['prior_games']) else 'no'
        dimensions=slices(row)+[('month_prior',row['prediction_at'][:7]+';prior='+prior),
                                ('remaining_prior',horizon(row['team_games_remaining'])+';prior='+prior)]
        for model in MODELS:
            if abs(float(prediction[f'{model}_goals'])+float(prediction[f'{model}_assists'])-float(prediction[f'{model}_points']))>1e-8:
                raise ValueError('Goals plus assists do not equal projected points')
            for target in TARGETS:
                observed,p=player_values(row,prediction,target,model)
                actual=float(label[f'final_{target}'])
                if actual<observed:
                    raise ValueError('Final total is below already recorded statistics')
                for dimension,group in dimensions:
                    metrics[season,dimension,group,model,target].regression(p,actual,player)
                serial+=1
                error=dict(player_id=player,season_id=season,prediction_at=row['prediction_at'],model=model,target=target,
                    observed=observed,prediction=p,actual=actual,error=p-actual,absolute_error=abs(p-actual),
                    games_played=row['games_played'],team_games_remaining=row['team_games_remaining'],
                    prior_games=row['prior_games'],last_played_team_id=row['last_played_team_id'],
                    max_assumed_available_at=row['max_assumed_available_at'],label_available_at=label_available[season],
                    source_join='player_id + season_id + prediction_at')
                pool=extremes[season,model,target]
                heapq.heappush(pool,(abs(p-actual),serial,error))
                if len(pool)>10:
                    heapq.heappop(pool)
    rows=[dict(season_id=s,dimension=d,group=g,model=m,target=t,**v.regression_result())
          for (s,d,g,m,t),v in sorted(metrics.items())]
    worst=[entry[2] for group in sorted(extremes) for entry in sorted(extremes[group],reverse=True)]
    return rows,worst


def uncertainty_reports(artifacts,games,holdout,output):
    label_available={s:max(g['assumed_available_at'] for g in games if g['season_id']==s)
        for s in {key[1] for key in artifacts['player_labels']}}
    by_season=defaultdict(list)
    for key,row in artifacts['player_inputs'].items():
        by_season[key[1]].append((key,row))
    coverage=defaultdict(Coverage)
    unavailable=Counter()
    fit_records=[]
    def records():
        for season,entries in sorted(by_season.items()):
            start=min(k[2] for k,row in entries)
            pools=fit_pools(artifacts,season,start,label_available,holdout)
            for group,pool in pools.items():
                model,target,bucket,prior=group
                fit_records.append(dict(evaluation_season=season,model=model,target=target,remaining_bucket=bucket,prior_records=prior,
                    unique_calibration_players=pool['n_players'],eligible=pool['eligible'],radius80=pool['radii'][0.8],radius95=pool['radii'][0.95],
                    calibration_seasons=pool['seasons'],max_calibration_label_available_at=pool['latest']))
            for key,row in entries:
                prediction=artifacts['player_predictions'][key]
                label=artifacts['player_labels'].get(key)
                for model in MODELS:
                    for target in TARGETS:
                        observed,point=player_values(row,prediction,target,model)
                        for nominal in NOMINALS:
                            band=interval(row,prediction,target,model,nominal,pools)
                            if band['available'] and band['max_calibration_label_available_at']>=key[2]:
                                raise ValueError('Calibration label reaches prediction instant')
                            if not band['available']:
                                unavailable[season,model,target,nominal]+=1
                            actual=float(label[f'final_{target}']) if label else None
                            if band['available'] and actual is not None:
                                for dimension,group in slices(row):
                                    coverage[season,model,target,nominal,dimension,group].add(key[0],actual,band['lower'],band['upper'])
                            yield dict(player_id=key[0],season_id=season,prediction_at=row['prediction_at'],data_mode=MODE,
                                model=model,target=target,nominal=nominal,observed=observed,prediction=point,**band)
    artifact=save_csv(output/'player_intervals.csv',records())
    summaries=[dict(season_id=s,model=m,target=t,nominal=n,dimension=d,group=g,**value.result())
        for (s,m,t,n,d,g),value in sorted(coverage.items())]
    missing=[dict(season_id=s,model=m,target=t,nominal=n,unavailable=count) for (s,m,t,n),count in sorted(unavailable.items())]
    return artifact,summaries,fit_records,missing,label_available


def elo_reports(original,selected,games,labels):
    metrics=defaultdict(Metrics)
    bins=defaultdict(lambda:[0,0.0,0])
    worst=defaultdict(list)
    serial=0
    original={r['game_id']:r for r in contextualize(original,games)}
    selected={r['game_id']:r for r in contextualize(selected,games)}
    for name,forecasts,column in [('original',original,'home_win_probability'),('development_selected',selected,'home_win_probability'),
                                 ('historical_home_rate',original,'historical_home_win_probability')]:
        for gid,row in forecasts.items():
            if gid not in labels:
                continue
            y=int(labels[gid]['home_win'])
            p=row[column]
            b=min(9,int(p*10))
            dimensions=[('all','all'),('month',row['month']),('home_team',str(row['home_team_id'])),
                ('away_team',str(row['away_team_id'])),('rating_difference',row['rating_bucket']),('stage',row['stage']),
                ('probability_range',f'{b/10:.1f}-{(b+1)/10:.1f}')]
            for dimension,group in dimensions:
                metrics[row['season_id'],name,dimension,group].probability(p,y)
            values=bins[row['season_id'],name,b]
            values[0]+=1; values[1]+=p; values[2]+=y
            if name!='historical_home_rate':
                serial+=1
                error=dict(season_id=row['season_id'],model=name,game_id=gid,prediction_at=row['prediction_at'],
                    home_team_id=row['home_team_id'],away_team_id=row['away_team_id'],home_win_probability=p,home_win=y,
                    brier=(p-y)**2,home_rating=row['home_rating'],away_rating=row['away_rating'],rating_difference=row['rating_difference'],
                    stage=row['stage'],max_assumed_available_at=row['max_assumed_available_at'])
                heapq.heappush(worst[row['season_id'],name],((p-y)**2,serial,error))
                if len(worst[row['season_id'],name])>20:
                    heapq.heappop(worst[row['season_id'],name])
    rows=[dict(season_id=s,model=m,dimension=d,group=g,**v.probability_result()) for (s,m,d,g),v in sorted(metrics.items())]
    calibration=[]
    for s,m in sorted({(s,m) for s,m,b in bins}):
        for b in range(10):
            n,total,wins=bins[s,m,b]
            calibration.append(dict(season_id=s,model=m,bin_lower=b/10,bin_upper=(b+1)/10,n=n,
                mean_probability=total/n if n else None,observed_home_win_rate=wins/n if n else None,
                calibration_gap=(total-wins)/n if n else None))
    decomposition=[]
    for r in rows:
        if r['dimension']!='all':continue
        subset=[v for v in calibration if v['season_id']==r['season_id'] and v['model']==r['model'] and v['n']]
        reliability=sum(v['n']/r['n']*v['calibration_gap']**2 for v in subset)
        resolution=sum(v['n']/r['n']*(v['observed_home_win_rate']-r['observed_home_win_rate'])**2 for v in subset)
        uncertainty=r['observed_home_win_rate']*(1-r['observed_home_win_rate'])
        decomposition.append(dict(season_id=r['season_id'],model=r['model'],reliability=reliability,resolution=resolution,
            uncertainty=uncertainty,binning_residual=r['brier']-(uncertainty-resolution+reliability)))
    errors=[e[2] for group in sorted(worst) for e in sorted(worst[group],reverse=True)]
    return rows,calibration,decomposition,errors


def audit_sources(history,games,original,saved):
    max_error=0
    if {r['game_id'] for r in original}!=set(saved):
        raise ValueError('Recomputed original fixtures differ from saved predictions')
    for r in original:
        reference=saved[r['game_id']]
        for column in ('home_rating','away_rating','home_win_probability','historical_home_win_probability'):
            max_error=max(max_error,abs(r[column]-float(reference[column])))
        if r['max_assumed_available_at'] is not None and r['max_assumed_available_at']>=r['prediction_at']:
            raise ValueError('Original Elo update violates cutoff')
        if timestamp(reference['prediction_at'])!=r['prediction_at']:
            raise ValueError('Original Elo clock mismatch')
    if max_error>1e-10:
        raise ValueError('Original implementation no longer reproduces saved predictions')
    reverse,_=elo_predictions(list(reversed(games)),max(r['prediction_at'] for r in original))
    # The latest future fixtures were clipped at the saved as_of instant.
    if [(r['game_id'],r['home_win_probability']) for r in reverse]!=[(r['game_id'],r['home_win_probability']) for r in original]:
        raise ValueError('Elo depends on input file ordering')
    scoring=Counter()
    for batch in pq.ParquetFile(history/'skaters.parquet').iter_batches(batch_size=4000,columns=['game_id','home_road','goals']):
        v=batch.to_pydict()
        for gid,side,goals in zip(v['game_id'],v['home_road'],v['goals']):scoring[gid,side]+=goals
    mismatch=[]
    for game in games:
        if not game['is_final']:continue
        so=game['result_type']=='SO'
        home=game['home_score']-int(so and game['home_score']>game['away_score'])
        away=game['away_score']-int(so and game['away_score']>game['home_score'])
        if (home,away)!=(scoring[game['game_id'],'H'],scoring[game['game_id'],'R']):
            mismatch.append(dict(game_id=game['game_id'],season_id=game['season_id'],home_goals_without_shootout=home,
                away_goals_without_shootout=away,skater_home_goals=scoring[game['game_id'],'H'],skater_away_goals=scoring[game['game_id'],'R']))
    # Resolve skater-only discrepancies against matching frozen raw goalie
    # responses where the archived files are present. Never make HTTP requests.
    bad={r['game_id']:r for r in mismatch}
    goalie_goals=Counter();missing=Counter();raw_sources={}
    goalie_path=history/'goalies.parquet'
    if bad and goalie_path.exists():
        for batch in pq.ParquetFile(goalie_path).iter_batches(batch_size=4000,
                columns=['player_id','season_id','game_id','home_road','source_sha256']):
            for row in batch.to_pylist():
                if row['game_id'] not in bad:continue
                found=None
                pattern=f"*/players/{row['season_id']}/goalie/{row['player_id']}.json"
                for path in (history.parents[1]/'nhl_game_logs').glob(pattern):
                    try:
                        envelope=json.loads(path.read_text(encoding='utf-8'))
                        records,checksum=validate_log(envelope)
                    except (OSError,ValueError,KeyError,TypeError):
                        continue
                    if checksum==row['source_sha256']:
                        found=next((r for r in records if r['gameId']==row['game_id']),None)
                        if found is not None:
                            raw_sources[str(path)]=file_hash(path)
                            break
                if found is None or found.get('goals') is None:
                    missing[row['game_id']]+=1
                else:
                    goalie_goals[row['game_id'],row['home_road']]+=found['goals']
        for gid,r in bad.items():
            r['matching_raw_goalie_records_missing']=missing[gid]
            r['goalie_home_goals']=goalie_goals[gid,'H'];r['goalie_away_goals']=goalie_goals[gid,'R']
            r['reconciled_including_goalie_goals']=not missing[gid] and (
                r['skater_home_goals']+goalie_goals[gid,'H'],r['skater_away_goals']+goalie_goals[gid,'R'])==(
                r['home_goals_without_shootout'],r['away_goals_without_shootout'])
    return dict(original_reproduction_max_absolute_difference=max_error,reversed_input_order_identical=True,
        strictly_pre_cutoff_updates=True,skater_only_scoring_mismatches=mismatch,matching_raw_goalie_source_hashes=raw_sources,
        limitations=['Skater-only totals omit goalie goals; discrepancies require that context, not automatic source correction.',
            'Original publication/revision times are unknown; timing is audited under the recorded reconstruction assumption.',
            'Neutral-site flags are absent from this reconstruction; original Elo applies home advantage to every fixture.'])


def render_reports(output,player,elo,coverage,calibration,selection,audit,holdout,ablations,plots=True):
    lines=['# Player forecast diagnostics','',
        'Signed bias is prediction minus final total. Samples repeat players monthly; counts are not independent trials.','',
        '| Season | Month | Model | Snapshots | Players | Points MAE | Bias |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: |']
    for r in player:
        if r['dimension']=='month' and r['target']=='points':
            lines.append(f"| {r['season_id']} | {r['group']} | {r['model']} | {r['n']} | {r['unique_players']} | {r['mae']:.2f} | {r['bias']:.2f} |")
    lines+=['','## Aggregate scoring and games-played errors','',
        '| Season | Model | Target | Snapshots | MAE | RMSE | Bias |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: |']
    for r in player:
        if r['dimension']=='all':
            lines.append(f"| {r['season_id']} | {r['model']} | {r['target']} | {r['n']} | {r['mae']:.2f} | {r['rmse']:.2f} | {r['bias']:.2f} |")
    lines+=['','Games-played forecasts are identical between models; blending changes scoring rates only.',
            'Month/prior and remaining-games/prior cohort comparisons, counts and extremes are available in the companion CSV/JSON files.']
    (output/'player_monthly.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    lines=['# Elo diagnostics','',
        'Original results are preserved. Candidate parameters were selected only on earlier development seasons; the previously inspected holdout is not a pristine unseen test.','',
        f"Frozen selection: {selection['parameters']}",'',
        '| Season | Model | Games | Brier | Log loss | Accuracy | Mean home probability | Home win rate |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in elo:
        if r['dimension']=='all':
            lines.append(f"| {r['season_id']} | {r['model']} | {r['n']} | {r['brier']:.4f} | {r['log_loss']:.4f} | {r['accuracy']:.3f} | {r['mean_probability']:.3f} | {r['observed_home_win_rate']:.3f} |")
    lines+=['','## One-factor development experiments (no holdout scoring)','',
        '| Change | K | Home advantage | Carryover | Mean development Brier |',
        '| --- | ---: | ---: | ---: | ---: |']
    for r in ablations:
        lines.append(f"| {r['change']} | {r['elo_k']} | {r['elo_home_advantage']} | {r['elo_season_retention']} | {r['mean_development_brier']:.5f} |")
    lines+=['','![Reliability diagrams](elo_reliability.svg)' if plots else 'Plot omitted (--no-plots); use elo_calibration.csv.','',
        'Calibration tables include empty bins and sample counts. Team/month extremes are descriptive, often small samples; rare upsets alone do not prove model defects.',
        'The binned Brier decomposition includes a residual because bins approximate continuous probabilities. It does not identify a causal explanation for a season change.',
        '',f"Original reproduction maximum difference: {audit['original_reproduction_max_absolute_difference']:.3g}.",
        f"Skater-only score discrepancies: {len(audit['skater_only_scoring_mismatches'])}; goalie goals are omitted from that cross-check.",
        'Neutral-site effects cannot be identified from this frozen schema. No source timestamps were changed.']
    (output/'elo_diagnostics.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    lines=['# Player prediction intervals','',
        'These are marginal prediction intervals for final totals, not confidence intervals for a mean.',
        'Historical absolute errors are collapsed to the maximum per player within each calibration pool, across all earlier snapshots and seasons. Each player contributes one calibration score.',
        'Quantiles use ceil((n+1)*nominal); at least 40 player clusters are required. Sparse groups fall back to broader pools; insufficient history produces no interval.',
        f'Holdout {holdout} labels are excluded from every calibration fit, including current-season fits.',
        'Time drift and dependent player/team outcomes prevent an unconditional calibration guarantee. Player-max bands may be conservative. No joint coverage claim is made for goals, assists and points; marginal endpoints are not additive.',
        '', '| Season | Model | Target | Nominal | Snapshots | Coverage | Player-weighted coverage | Mean width |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for r in coverage:
        if r['dimension']=='all':
            lines.append(f"| {r['season_id']} | {r['model']} | {r['target']} | {r['nominal']:.0%} | {r['n']} | {r['coverage']:.3f} | {r['player_weighted_coverage']:.3f} | {r['average_width']:.1f} |")
    (output/'uncertainty.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def run_evaluation(root,output,holdout=HOLDOUT,plots=True):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty, separate evaluation directory; original artifacts are immutable')
    artifacts,source=read_artifacts(root)
    history=Path(source['history'])
    if file_hash(history/'manifest.json')!=source['history_manifest_sha256']:
        raise ValueError('History manifest no longer matches baseline')
    history_manifest=json.loads((history/'manifest.json').read_text(encoding='utf-8'))
    if history_manifest.get('status')!='complete' or history_manifest.get('data_mode')!=MODE:
        raise ValueError('History must be a complete reconstruction, not a strict export')
    for name in ('games','skaters',*(['goalies'] if 'goalies' in history_manifest['artifacts'] else [])):
        if file_hash(history/f'{name}.parquet')!=history_manifest['artifacts'][name]['sha256']:
            raise ValueError(f'Changed history: {name}')
    games=pq.read_table(history/'games.parquet').to_pylist()
    game_by_id={g['game_id']:g for g in games}
    now=timestamp(source['as_of'])
    if holdout not in source['completed_seasons']:
        raise ValueError('Reserved holdout must be a completed source season')
    for gid,label in artifacts['elo_labels'].items():
        game=game_by_id.get(gid)
        if not game or not game['is_final'] or int(label['home_win'])!=int(game['winner_team_id']==game['home_team_id']):
            raise ValueError('Saved Elo label differs from frozen game result')
    output.mkdir(parents=True,exist_ok=True)
    plan=dict(version=VERSION,data_mode=MODE,reserved_holdout=holdout,candidates=candidate_grid(),
        selection_policy='Only complete development seasons before holdout; earliest season is warmup. Freeze selection before computing candidate holdout metrics.',
        interval_policy='Fixed 40-player minimum; maximum absolute residual per player; earlier complete non-holdout seasons only.',
        original_manifest_sha256=file_hash(root/'manifest.json'))
    write_document(output/'experiment_plan.json',plan)
    # Freeze the choice before any candidate holdout metrics are computed.
    selection,development,folds=select_elo(games,now,holdout)
    write_document(output/'elo_selection.json',selection)
    save_csv(output/'elo_development_grid.csv',development)
    save_csv(output/'elo_forward_development.csv',folds)
    ablations=[]
    for parameters in candidate_grid():
        changed=[k for k in ('elo_k','elo_home_advantage','elo_season_retention') if parameters[k]!=PARAMETERS[k]]
        if len(changed)>1:continue
        rows=[r for r in development if all(r[k]==parameters[k] for k in ('elo_k','elo_home_advantage','elo_season_retention'))]
        ablations.append(dict(change=changed[0] if changed else 'original',**{k:v for k,v in parameters.items() if k.startswith('elo_')},
            mean_development_brier=sum(r['brier'] for r in rows)/len(rows),
            mean_development_log_loss=sum(r['log_loss'] for r in rows)/len(rows)))
    save_csv(output/'elo_development_ablations.csv',ablations)
    original,_=elo_predictions(games,now)
    audit=audit_sources(history,games,original,artifacts['elo_inputs_predictions'])
    selected,ratings=elo_predictions(games,now,selection['parameters'])
    write_document(output/'selected_elo_model.json',dict(parameters=selection['parameters'],franchise_ratings=ratings,as_of=now,data_mode=MODE,
        franchise_mapping=FRANCHISE_MAP,
        selection_sha256=file_hash(output/'elo_selection.json')))
    elo,calibration,decomposition,errors=elo_reports(original,selected,games,artifacts['elo_labels'])
    for name,rows in [('elo_metrics',elo),('elo_calibration',calibration),('elo_brier_decomposition',decomposition),('elo_extreme_errors',errors),
                      ('selected_elo_predictions',selected)]:
        save_csv(output/f'{name}.csv',rows)
    write_document(output/'elo_metrics.json',elo)
    write_document(output/'elo_calibration.json',calibration)
    write_document(output/'source_timing_audit.json',audit)
    interval_artifact,coverage,fits,missing,label_available=uncertainty_reports(artifacts,games,holdout,output)
    player,worst=player_reports(artifacts,label_available)
    paired=defaultdict(dict)
    for r in player:
        paired[r['season_id'],r['dimension'],r['group'],r['target']][r['model']]=r
    comparison=[dict(season_id=s,dimension=d,group=g,target=t,n=v['pace']['n'],unique_players=v['pace']['unique_players'],
        pace_mae=v['pace']['mae'],prior_shrinkage_mae=v['prior_shrinkage']['mae'],
        mae_reduction=v['pace']['mae']-v['prior_shrinkage']['mae']) for (s,d,g,t),v in sorted(paired.items())]
    for name,rows in [('player_metrics',player),('player_extreme_errors',worst),('interval_coverage',coverage)]:
        save_csv(output/f'{name}.csv',rows)
        write_document(output/f'{name}.json',rows)
    save_csv(output/'player_model_comparison.csv',comparison)
    write_document(output/'player_model_comparison.json',comparison)
    write_document(output/'interval_calibration.json',dict(fits=fits,unavailable=missing,holdout_excluded=holdout,
        minimum_player_clusters=40,method='Finite-sample rank of player-cluster maximum absolute residuals',joint_coverage_claim=False))
    render_reports(output,player,elo,coverage,calibration,selection,audit,holdout,ablations,plots)
    if plots:
        from nhl_analytics.evaluation_plots import reliability_plot
        reliability_plot(calibration,output/'elo_reliability.svg')
    files={p.name:dict(sha256=file_hash(p),bytes=p.stat().st_size) for p in sorted(output.iterdir()) if p.is_file()}
    write_document(output/'manifest.json',dict(status='complete',version=VERSION,data_mode=MODE,holdout=holdout,
        original_baseline=str(root.resolve()),original_manifest_sha256=file_hash(root/'manifest.json'),
        history_manifest_sha256=source['history_manifest_sha256'],as_of=now,files=files,
        implementation_sha256={name:file_hash(Path(__file__).with_name(name+'.py'))
            for name in ('evaluation','evaluation_support','elo_development','uncertainty','evaluation_plots','baselines')},
        runtime=dict(python=sys.version.split()[0],pyarrow=pa.__version__,
                     matplotlib=importlib.metadata.version('matplotlib') if plots else None),
        reconstructed_policy=history_manifest['availability_policy'],
        holdout_policy='Original benchmark retained. Holdout labels are diagnostics only, excluded from Elo selection and all uncertainty fits.',
        limitations=['Already inspected holdout is not pristine unseen data.','Intervals are marginal, not jointly additive or guaranteed calibrated.',
                    'No full MoneyPuck archive scan or database modification is performed.']))
    return dict(output=str(output),status='complete',selected_parameters=selection['parameters'],intervals=interval_artifact['rows'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('data/evaluations/reconstructed'))
    parser.add_argument('--no-plots',action='store_true',help='For minimal environments; calibration CSV/JSON still produced')
    args=parser.parse_args()
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    print(json.dumps(run_evaluation(args.baseline,output,plots=not args.no_plots),indent=2))


if __name__=='__main__':
    main()
