"""Read-only team feature audit and isolated xG/goalie research exports. Never fits models."""
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pyarrow.parquet as pq
from psycopg.rows import dict_row

from nhl_analytics.database import connect, timestamp
from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.pregame import FEATURES, franchise, read_sources, difference, ratio
from nhl_analytics.reconstructed import file_hash
from nhl_analytics.team_models import read_dataset

DEFAULT_RUN=Path('data/team_models/reconstructed/20261111T150925233807Z')
CANDIDATE=['last10_xg_share_diff','last10_xg_diff_per_game_diff',
           'proxy_goalie_last10_save_pct_diff','proxy_goalie_last7_minutes_diff',
           'proxy_goalie_days_since_appearance_diff']


def audit_rows(rows):
    """Missing is explicit, not zero. Latest source age is not game-history age."""
    groups=defaultdict(list)
    violations=[]
    for row in rows:
        groups[row['season_id'],row['prediction_at'][:7]].append(row)
        cutoff=timestamp(row['prediction_at'])
        for key in ('max_assumed_feature_available_at','home_last_stats_available_at','away_last_stats_available_at'):
            if row.get(key) and timestamp(row[key])>=cutoff:
                violations.append(dict(game_id=row['game_id'],field=key))
        for side in ('home','away'):
            if str(row.get(side+'_last_stats_game_id'))==str(row['game_id']):
                violations.append(dict(game_id=row['game_id'],field=side+'_target_game_in_history'))
    missing=[];freshness=[]
    for (season,month),items in sorted(groups.items()):
        for feature in FEATURES:
            absent=[r for r in items if r.get(feature) is None or not math.isfinite(float(r[feature]))]
            window='prior' if feature.startswith('prior_') else 'season' if feature.startswith(('season_','last10_')) else None
            structural=sum(not r['home_'+window+'_games'] or not r['away_'+window+'_games'] for r in absent) if window else 0
            n=len(absent)
            missing.append(dict(season_id=season,month=month,feature=feature,n=len(items),missing=n,
                structural_missing=structural,missing_with_history=n-structural,missing_pct=100*n/len(items)))
        for side in ('home','away'):
            ages=sorted((timestamp(r['prediction_at'])-timestamp(r[side+'_last_stats_available_at'])).total_seconds()/3600
                for r in items if r.get(side+'_last_stats_available_at'))
            freshness.append(dict(season_id=season,month=month,side=side,n=len(items),
                no_current_history=sum(not r[side+'_season_games'] for r in items),
                no_prior_history=sum(not r[side+'_prior_games'] for r in items),
                median_latest_stats_age_hours=ages[len(ages)//2] if ages else None,
                max_latest_stats_age_hours=max(ages,default=None)))
    return missing,freshness,violations


def context(records, goalie_records, target, cutoff):
    """One side, one season; exclude target and every not-yet-available observation."""
    known=lambda r:r['game_id']!=target and r['available_at']<cutoff and r['start']<cutoff
    eligible=sorted((r for r in records if known(r)),key=lambda r:(r['start'],r['game_id']))
    window=eligible[-10:]
    complete=[r for r in window if r.get('xgf') is not None and r.get('xga') is not None]
    # Require all selected games and at least five games. Counts/lag remain available.
    sufficient=len(window)>=5 and len(complete)==len(window)
    gf=sum(r['xgf'] for r in complete);ga=sum(r['xga'] for r in complete)
    goalie=sorted((r for r in goalie_records if known(r)),key=lambda r:(r['start'],r['game_id'],r['player_id']))
    starts=[r for r in goalie if r['started']==1]
    proxy=starts[-1]['player_id'] if starts else None
    appearances=[r for r in goalie if r['player_id']==proxy and r['toi_seconds'] and r['toi_seconds']>0]
    recent=appearances[-10:]
    valid=len(recent)>=5 and all(r['saves'] is not None and r['shots_against'] is not None and 0<=r['saves']<=r['shots_against'] for r in recent)
    stamps=[r['available_at'] for r in window+appearances]
    return dict(last10_xg_share=ratio(gf,gf+ga) if sufficient else None,
        last10_xg_diff_per_game=(gf-ga)/len(window) if sufficient else None,
        xg_games=len(window),xg_complete_games=len(complete),
        xg_latest_game_age_days=(cutoff-max(r['start'] for r in eligible if r.get('xgf') is not None and r.get('xga') is not None)).total_seconds()/86400 if any(r.get('xgf') is not None and r.get('xga') is not None for r in eligible) else None,
        proxy_goalie_id=proxy,proxy_goalie_appearances=len(recent),
        proxy_goalie_last10_save_pct=ratio(sum(r['saves'] for r in recent),sum(r['shots_against'] for r in recent)) if valid else None,
        proxy_goalie_last7_minutes=sum(r['toi_seconds'] for r in appearances if r['start']>=cutoff-timedelta(days=7))/60 if appearances else None,
        proxy_goalie_days_since_appearance=(cutoff-appearances[-1]['start']).total_seconds()/86400 if appearances else None,
        max_feature_available_at=max(stamps).isoformat() if stamps else None,
        source_game_ids=sorted({r['game_id'] for r in window+appearances}))


def source_context(connection,games,history):
    """Only bounded team-game and NHL goalie sources, never the shot archive."""
    by_id={g['game_id']:g for g in games};xg=defaultdict(list);goalies=defaultdict(list)
    inventory=[];rejected=0
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT DISTINCT ON (r.record_key) r.game_id,r.team_code,r.game_date,
            r.season_id,r.xgoals_for,r.xgoals_against,r.observed_at,r.file_id,f.sha256
            FROM analytics.moneypuck_records r JOIN analytics.moneypuck_files f USING(file_id)
            WHERE r.kind='team_game' AND r.situation='5on5' AND r.game_type_id=2
            AND r.season_id=ANY(%s) AND f.status='complete'
            ORDER BY r.record_key,r.observed_at DESC,r.file_id''', (sorted({g['season_id'] for g in games}),))
        seen=set()
        for row in cursor:
            game=by_id.get(row['game_id'])
            if not game or not game['is_final'] or row['game_date']!=game['game_date']:
                rejected+=1;continue
            sides=[s for s in ('home','away') if game[s+'_team_abbrev']==row['team_code']]
            if len(sides)!=1:raise ValueError('MoneyPuck team/game identity mismatch')
            team=franchise(game[sides[0]+'_team_id']);key=game['game_id'],team
            if key in seen:raise ValueError('Duplicate MoneyPuck team-game at 5v5')
            seen.add(key)
            if any(v is not None and (not math.isfinite(v) or v<0) for v in (row['xgoals_for'],row['xgoals_against'])):
                raise ValueError('Invalid expected goals')
            xg[game['season_id'],team].append(dict(game_id=game['game_id'],start=game['start_time_utc'],
                available_at=game['assumed_available_at'],observed_at=row['observed_at'],xgf=row['xgoals_for'],xga=row['xgoals_against']))
            inventory.append(dict(game_id=game['game_id'],team_id=team,season_id=game['season_id'],
                game_date=str(game['game_date']),file_id=str(row['file_id']),file_sha256=row['sha256'],
                actual_observed_at=row['observed_at'].isoformat(),assumed_available_at=game['assumed_available_at'].isoformat()))
    # Keep missing team-games in each ten-game window instead of silently reaching
    # further back to find ten nonmissing MoneyPuck rows.
    for game in games:
        if not game['is_final']:continue
        for side in ('home','away'):
            team=franchise(game[side+'_team_id'])
            if (game['game_id'],team) not in seen:
                xg[game['season_id'],team].append(dict(game_id=game['game_id'],start=game['start_time_utc'],
                    available_at=game['assumed_available_at'],xgf=None,xga=None))
    for batch in pq.ParquetFile(history/'goalies.parquet').iter_batches(batch_size=1000,use_threads=False):
        for row in batch.to_pylist():
            game=by_id[row['game_id']];side='home' if row['home_road']=='H' else 'away'
            if row['team_abbrev']!=game[side+'_team_abbrev']:raise ValueError('Goalie/game team mismatch')
            goalies[game['season_id'],franchise(game[side+'_team_id'])].append(dict(game_id=row['game_id'],
                player_id=row['player_id'],start=game['start_time_utc'],started=row['games_started'],
                available_at=max(row['assumed_available_at'],game['assumed_available_at']),
                shots_against=row['shots_against'],saves=row['saves'],toi_seconds=row['toi_seconds']))
    return xg,goalies,inventory,rejected


def enrich(rows,xg,goalies):
    output=[]
    for row in rows:
        cutoff=timestamp(row['prediction_at']);sides={}
        for side in ('home','away'):
            key=row['season_id'],franchise(row[side+'_team_id'])
            sides[side]=context(xg[key],goalies[key],row['game_id'],cutoff)
        result=dict(row)
        for name in CANDIDATE:
            metric=name[:-5];result[name]=difference(sides['home'][metric],sides['away'][metric])
        for side,item in sides.items():
            for key,value in item.items():
                result[side+'_'+key]=json.dumps(value) if isinstance(value,list) else value
        output.append(result)
    return output


def prospective_audit(connection,now):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT f.game_id,b.status,b.reason,b.recorded_at,b.feature_as_of,b.max_source_available_at,
            b.features,b.provenance->'coverage' AS coverage,f.cutoff_at FROM analytics.prospective_batches b
            JOIN analytics.prospective_fixtures f USING(fixture_id) WHERE b.campaign='elo_vs_logistic_v1' ORDER BY b.recorded_at''')
        rows=[]
        for r in cursor:
            features=r.pop('features') or {}
            r['missing_features']=[k for k in FEATURES if features.get(k) is None] if r['status']=='sealed' else []
            r['latest_source_age_minutes']=(r['feature_as_of']-r['max_source_available_at']).total_seconds()/60 if r['max_source_available_at'] else None
            r['timing_valid']=r['status']!='sealed' or r['max_source_available_at']<r['feature_as_of']<=r['recorded_at']<r['cutoff_at']
            rows.append(r)
        cursor.execute('''SELECT s.team_abbrev,count(DISTINCT s.game_id) AS log_games,max(g.game_date) AS latest_log_game,
            max(src.observed_at) AS latest_source_observed FROM analytics.skater_game_stats s
            JOIN analytics.games g USING(game_id) JOIN analytics.game_log_sources src USING(source_id)
            WHERE s.season_id=20262027 AND s.game_type_id=2 GROUP BY s.team_abbrev ORDER BY s.team_abbrev''')
        teams=list(cursor)
        cursor.execute('''SELECT home_team_abbrev AS team,max(game_date) AS date FROM analytics.games
            WHERE season_id=20262027 AND game_type_id=2 AND game_state IN ('FINAL','OFF') GROUP BY home_team_abbrev
            UNION ALL SELECT away_team_abbrev,max(game_date) FROM analytics.games
            WHERE season_id=20262027 AND game_type_id=2 AND game_state IN ('FINAL','OFF') GROUP BY away_team_abbrev''')
        latest={}
        for row in cursor:latest[row['team']]=max(latest.get(row['team'],row['date']),row['date'])
        for row in teams:
            row['latest_known_final']=latest.get(row['team_abbrev'])
            row['latest_final_present']=row['latest_known_final']==row['latest_log_game']
            row['source_age_hours']=(now-row['latest_source_observed']).total_seconds()/3600
        cursor.execute('''SELECT s.team_abbrev,count(DISTINCT s.game_id) AS games,max(g.game_date) AS latest
            FROM analytics.goalie_game_stats s JOIN analytics.games g USING(game_id)
            WHERE s.season_id=20262027 AND s.game_type_id=2 GROUP BY s.team_abbrev''')
        goalie_coverage={r['team_abbrev']:r for r in cursor}
        for row in teams:
            goalie=goalie_coverage.get(row['team_abbrev'],{})
            row['goalie_log_games']=goalie.get('games',0)
            row['latest_goalie_game']=goalie.get('latest')
            row['latest_goalie_final_present']=row['latest_known_final']==goalie.get('latest')
    return rows,teams


def run(model_run,output):
    if output.exists() and any(output.iterdir()):raise ValueError('Use an empty new audit directory')
    output.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((model_run/'manifest.json').read_text())
    dataset=Path(frozen['dataset']);rows,labels,manifest=read_dataset(dataset)
    missing,freshness,violations=audit_rows(rows)
    games,_,history,provenance=read_sources(Path(manifest['evaluation']))
    now=datetime.now(timezone.utc)
    with connect() as c,c.transaction():
        c.execute('SET TRANSACTION READ ONLY')
        xg,goalies,inventory,rejected=source_context(c,games,history)
        prospective,teams=prospective_audit(c,now)
    candidate=enrich(rows,xg,goalies)
    coverage=[]
    for season in sorted({r['season_id'] for r in candidate}):
        cohort=[r for r in candidate if r['season_id']==season]
        for name in CANDIDATE:
            coverage.append(dict(season_id=season,feature=name,n=len(cohort),missing=sum(r[name] is None for r in cohort)))
    for name,values in [('missing_by_month',missing),('freshness_by_month',freshness),('candidate_coverage',coverage),
                        ('team_current_freshness',teams),('xg_source_inventory',inventory),('candidate_features_reconstructed',candidate)]:
        save_csv(output/(name+'.csv'),values)
    write_document(output/'prospective_audit.json',prospective)
    summary=dict(status='complete',generated_at=now,reconstructed_rows=len(rows),timing_violations=violations,
        missing_feature_cells_with_history=sum(r['missing_with_history'] for r in missing),
        prospective_sealed=sum(r['status']=='sealed' for r in prospective),
        prospective_timing_violations=sum(not r['timing_valid'] for r in prospective),
        teams_with_latest_final=sum(r['latest_final_present'] for r in teams),teams_checked=len(teams),
        goalie_teams_with_latest_final=sum(r['latest_goalie_final_present'] for r in teams),
        xg_matched_team_games=len(inventory),xg_unmatched_or_not_in_frozen_history=rejected,
        xg_latest_game_date=max((r['game_date'] for r in inventory),default=None),
        production_changed=False,models_fitted=False,candidate_columns=CANDIDATE,
        candidate_data_mode='reconstructed_latest_corrected',
        strict_xg_historical_availability_verified=False,
        source_model_manifest_sha256=file_hash(model_run/'manifest.json'),source_dataset_manifest_sha256=file_hash(dataset/'manifest.json'),
        limitations=['xG uses latest corrected 5v5 team-game files and NHL next-day assumed availability only in this research export.',
            'Baseline artifacts retain aggregate latest-source bounds, not a full per-feature timestamp lineage. Passing these checks is not independent proof of every source publication time.',
            'Actual MoneyPuck import timestamps are preserved in xg_source_inventory; never substitute assumed times into observed forecasts.',
            'The goalie proxy is the last known starter for the team, not a confirmed target-game starter. No target-game player statistics are used.',
            'At least five valid games/appearances are required for xG and proxy save rates. Missing remains missing.',
            'xG history can be stale; per-side age and game counts must accompany it. No seasonal aggregate or shot archive was read.',
            'No model fit, comparison, tuning, promotion or retrospective prospective forecast generation was performed.'])
    write_document(output/'summary.json',summary)
    lines=['# Team feature freshness and candidate audit','',f'Audited {len(rows):,} frozen reconstructed feature rows. Production models were not modified.',
        f'Timing violations: {len(violations)} reconstructed, {summary["prospective_timing_violations"]} sealed prospective.',
        f'Current NHL skater logs include the latest known final for {summary["teams_with_latest_final"]}/{len(teams)} teams.',
        f'Latest matched MoneyPuck xG game: {summary["xg_latest_game_date"]}.','',
        'Missingness is reported by feature, season and month. Opening games without current-season history and the earliest season without prior history are structural missingness, not zero-valued rates.',
        'Latest source observation age and age of the game being summarised are distinct: refreshing an old game log does not create newer playing history.','',
        '## Research candidate only','',
        'The separate CSV adds recent 5v5 xG share/differential and a last-known-starter proxy with individual save rate, rest and seven-day workload. Counts, ages, source-game IDs and availability bounds remain alongside features.',
        'This is reconstructed research, not a strict observed export. Actual historical MoneyPuck publication times are unknown. Never join season-final totals to earlier predictions.',
        'No candidate was trained or scored. Pre-register chronological development folds and an ablation plan before fitting; exclude the previously inspected 2025–26 benchmark from selection and preserve genuine prospective records.',
        'For production readiness, first collect daily observed MoneyPuck team-game snapshots, validate source completeness and lag, and establish a prospective goalie-selection policy. Confirmed starters need a separate timestamped source; they are not available from this proxy.','',
        'See summary.json, missing_by_month.csv, freshness_by_month.csv, team_current_freshness.csv, prospective_audit.json and candidate_coverage.csv.','']
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-run',type=Path,default=DEFAULT_RUN)
    parser.add_argument('--output',type=Path,default=Path('data/team_feature_audits')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args();result=run(args.model_run,args.output)
    print(json.dumps({k:result[k] for k in ('reconstructed_rows','timing_violations','teams_with_latest_final','teams_checked','xg_latest_game_date','production_changed')}))


if __name__=='__main__':main()
