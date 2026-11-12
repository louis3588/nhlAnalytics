"""Matched prospective scoring only; historical reference results stay separate."""

import math
import random
from collections import Counter,defaultdict
from datetime import date,timedelta
from pathlib import Path

from nhl_analytics.baselines import probability_metrics
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.prospective_store import jsonable

NAMES=('frozen_elo','logistic_team_context')


def model_values(row,campaign):
    output=[]
    for name,column,version in [('frozen_elo','elo_home_probability',campaign['elo_version']),('logistic_team_context','logistic_home_probability',campaign['logistic_version'])]:
        p=row[column]
        if p is None:continue
        home=int(row['winner_team_id']==row['home_team_id']) if row['valid_for_scoring'] else None
        output.append(dict(model=name,version=version,home_win_probability=p,away_win_probability=1-p,
            favourite_team_id=row['home_team_id'] if p>=0.5 else row['away_team_id'],
            favourite_won=(p>=0.5)==bool(home) if home is not None else None,brier=(p-home)**2 if home is not None else None))
    return output


def game_item(row,campaign,now):
    status=row['status'] or ('awaiting_tracker' if now>=row['cutoff_at'] else 'pending_data' if row['last_attempt_reason'] else 'awaiting_cutoff')
    return dict(game_id=row['game_id'],season_id=row['season_id'],home_team_id=row['home_team_id'],away_team_id=row['away_team_id'],
        scheduled_start=row['scheduled_start'],cutoff_at=row['cutoff_at'],fixture_id=row['fixture_id'],game_state=row['game_state'],
        data_mode='prospective_observed',status=status,timing=row['timing'],reason=row['reason'] or row['last_attempt_reason'],
        generated_at=row['generated_at'],feature_as_of=row['feature_as_of'],max_source_available_at=row['max_source_available_at'],feature_sha256=row['feature_sha256'],
        outcome_status=row['outcome_status'],valid_for_scoring=row['valid_for_scoring'],
        outcome=dict(result_id=row['result_id'],checked_at=row['result_checked_at'],home_score=row['home_score'],away_score=row['away_score'],winner_team_id=row['winner_team_id']) if row['result_id'] else None,
        models=model_values(row,campaign))


def paired_interval(rows):
    groups=defaultdict(list)
    for row in rows:
        day=row['game_date'];week=day-timedelta(days=day.weekday())
        y=int(row['winner_team_id']==row['home_team_id']);elo=row['elo_home_probability'];log=row['logistic_home_probability']
        groups[week].append((log-y)**2-(elo-y)**2)
    n=len(rows)
    answer=dict(n=n,weeks=len(groups),brier_delta=sum(sum(v) for v in groups.values())/n if n else None,lower=None,upper=None,
        available=n>=100 and len(groups)>=10,method='Paired calendar-week block bootstrap, conditional exploratory interval')
    if not answer['available']:return answer
    blocks=[(len(v),sum(v)) for v in groups.values()];rng=random.Random(42);deltas=[]
    for _ in range(1000):
        chosen=[blocks[rng.randrange(len(blocks))] for _ in blocks]
        deltas.append(sum(v[1] for v in chosen)/sum(v[0] for v in chosen))
    deltas.sort();answer['lower']=deltas[24];answer['upper']=deltas[974]
    return answer


def performance(rows,campaign,now):
    paired=[r for r in rows if r['valid_for_scoring']]
    summary=[];calibration=[];curves={};comparisons=[];daily=[]
    for timing in ('all','standard','early'):
        cohort=[r for r in paired if timing=='all' or r['timing']==timing]
        comparisons.append(dict(timing=timing,**paired_interval(cohort)))
        groups=[('all','all',cohort)]
        groups += [('season',str(s),[r for r in cohort if r['season_id']==s]) for s in sorted({r['season_id'] for r in cohort})]
        groups += [('month',m,[r for r in cohort if r['game_date'].isoformat().startswith(m)]) for m in sorted({r['game_date'].isoformat()[:7] for r in cohort})]
        for dimension,key,values in groups:
            for name,column in zip(NAMES,('elo_home_probability','logistic_home_probability')):
                summary.append(dict(timing=timing,dimension=dimension,group=key,model=name,
                    **probability_metrics((r[column],int(r['winner_team_id']==r['home_team_id'])) for r in values)))
        for name,column in zip(NAMES,('elo_home_probability','logistic_home_probability')):
            for bucket in range(10):
                values=[r for r in cohort if min(9,int(r[column]*10))==bucket]
                calibration.append(dict(timing=timing,model=name,bin_lower=bucket/10,bin_upper=(bucket+1)/10,n=len(values),
                    mean_probability=sum(r[column] for r in values)/len(values) if values else None,
                    observed_home_win_rate=sum(r['winner_team_id']==r['home_team_id'] for r in values)/len(values) if values else None))
        cumulative=[];sums=[0.0,0.0]
        for count,row in enumerate(sorted(cohort,key=lambda r:(r['scheduled_start'],r['game_id'])),1):
            y=int(row['winner_team_id']==row['home_team_id'])
            for i,column in enumerate(('elo_home_probability','logistic_home_probability')):sums[i]+=(row[column]-y)**2
            cumulative.append(dict(n=count,game_id=row['game_id'],date=row['game_date'],frozen_elo=sums[0]/count,logistic_team_context=sums[1]/count))
        curves[timing]=cumulative
    for day in sorted({r['game_date'] for r in paired}):
        values=[r for r in paired if r['game_date']==day]
        for name,column in zip(NAMES,('elo_home_probability','logistic_home_probability')):
            daily.append(dict(game_date=day,model=name,**probability_metrics((r[column],int(r['winner_team_id']==r['home_team_id'])) for r in values)))
    due=[r for r in rows if r['cutoff_at']<=now]
    sealed=[r for r in rows if r['status']=='sealed']
    missing=[r for r in rows if r['status']=='missing']
    return dict(data_mode='prospective_observed',campaign=campaign['campaign'],model_set_id=campaign['model_set_id'],
        registered_at=campaign['registered_at'],tracking_date=campaign['tracking_date'],generated_at=now,
        coverage=dict(tracked_games=len(rows),sealed_pairs=len(sealed),standard_pairs=sum(r['timing']=='standard' for r in sealed),
            early_pairs=sum(r['timing']=='early' for r in sealed),due_games=len(due),due_sealed=sum(r['status']=='sealed' for r in due),
            missing=len(missing),pending=sum(r['status'] is None for r in rows),validated_paired=len(paired),
            exclusions=dict(Counter(r['reason'] or r['outcome_status'] for r in rows if not r['valid_for_scoring'] and (r['status']=='missing' or r['status']=='sealed')))),
        metrics=summary,paired_differences=comparisons,calibration=calibration,cumulative=curves,daily_by_game_date=daily,
        historical=dict(data_mode='reconstructed_latest_corrected',as_of=campaign['bundle']['reference_as_of'],rows=campaign['bundle']['references']),
        limitations=['Early bootstrap and standard-window forecasts are separately labelled and filterable.',
            'No superiority claim from a small sample. Intervals require 100 games and ten calendar weeks.',
            'Result corrections can change derived scores; sealed probabilities never change.',
            'Historical reconstructed benchmarks are reference data only and never enter prospective scoring.'])


def save_report(report,items,root):
    folder=Path(root)/report['generated_at'].strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True,exist_ok=False)
    write_document(folder/'cumulative.json',report)
    save_csv(folder/'metrics.csv',report['metrics'])
    save_csv(folder/'calibration.csv',report['calibration'])
    if report['daily_by_game_date']:save_csv(folder/'daily.csv',jsonable(report['daily_by_game_date']))
    forecasts=[]
    for r in items:
        models={m['model']:m for m in r['models']}
        record={key:r[key] for key in ('game_id','fixture_id','season_id','home_team_id','away_team_id','scheduled_start',
            'status','timing','reason','generated_at','cutoff_at','feature_as_of','max_source_available_at','feature_sha256',
            'outcome_status','valid_for_scoring')}
        record.update({key:(r['outcome'] or {}).get(key) for key in ('home_score','away_score','winner_team_id')})
        for name in NAMES:
            for key in ('version','home_win_probability','away_win_probability','favourite_won','brier'):
                record[f'{name}_{key}']=models.get(name,{}).get(key)
        forecasts.append(record)
    save_csv(folder/'games.csv',forecasts)
    lines=['# Prospective model tracking','',f"Campaign: {report['campaign']}",f"Generated: {report['generated_at'].isoformat()}",'',
        f"Sealed pairs: {report['coverage']['sealed_pairs']}; validated matched games: {report['coverage']['validated_paired']}",'',
        '| Timing | Model | Validated games | Brier | Log loss | Accuracy |','| --- | --- | ---: | ---: | ---: | ---: |']
    for row in report['metrics']:
        if row['dimension']!='all':continue
        fmt=lambda v:'Unavailable' if v is None else f'{v:.5f}'
        lines.append(f"| {row['timing']} | {row['model']} | {row['n']} | {fmt(row['brier'])} | {fmt(row['log_loss'])} | {fmt(row['accuracy'])} |")
    lines.extend(['','Only immutable prospective forecast pairs with a matching current schedule and validated final result are scored.',
        'Early forecasts are labelled separately. Small samples do not establish a superior model. Historical benchmarks are excluded.',''])
    (folder/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    write_document(Path(root)/'latest.json',dict(folder=str(folder.resolve()),generated_at=report['generated_at']))
    return folder
