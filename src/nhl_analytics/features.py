"""Reusable, strictly point-in-time NHL features with versioned CSV/Parquet exports."""

import argparse
import csv
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date,datetime,timedelta,timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nhl_analytics.asof import LEAGUE_TZ,past_final,read_context,read_games,read_player_games,read_shots,read_team_advanced
from nhl_analytics.database import connect,timestamp
from nhl_analytics.game_logs import toi_seconds
from nhl_analytics.ingest import season_argument
from nhl_analytics.team_context import nested

VERSION='1.0.0'
WINDOWS=('season','last5','last10')
PLAYER_METRICS={'goals':'goals','assists':'assists','points':'points','shots':'shots',
                'toi_seconds':'toi','power_play_points':'powerPlayPoints'}
GOALIE_METRICS={'shots_against':'shotsAgainst','goals_against':'goalsAgainst','starts':'gamesStarted','toi_seconds':'toi'}


def ratio(a,b,scale=1):
    return a*scale/b if a is not None and b is not None and b>0 else None


def total(values):
    values=list(values)
    return None if any(v is None for v in values) else sum(values)


def metric(record,key):
    value=record['payload'].get(key)
    if key=='toi':
        return toi_seconds(value)
    if value is not None and (type(value) is not int or value<0):
        raise ValueError(f'Invalid count for feature {key}')
    return value


def ordering(record):
    return (record['game_date'],record.get('start_at') or datetime.min.replace(tzinfo=timezone.utc),record['game_id'])


def attach_xg(record,actors,coverage):
    game=coverage.get(record['game_id'])
    if not game or game['available_at']>=record['cutoff']:
        return None,None
    kind='player' if record['kind']=='skater' else 'goalie'
    if game['unknown_player' if kind=='player' else 'unknown_goalie']:
        return None,None
    actor=actors.get((kind,record['player_id'],record['game_id']))
    expected_shots=record['payload'].get('shots' if kind=='player' else 'shotsAgainst')
    expected_goals=record['payload'].get('goals' if kind=='player' else 'goalsAgainst')
    sog,goals=(actor['sog'],actor['goals']) if actor else (0,0)
    if expected_shots is None or expected_goals is None or sog!=expected_shots or goals!=expected_goals:
        return None,None
    if actor and (not actor['valid_xg'] or actor['available_at']>=record['cutoff']):
        return None,None
    return (float(actor['xg']) if actor else 0.0),game['available_at']


def actor_features(records,kind,cutoff):
    records=sorted(records,key=ordering)
    last=records[-1]
    result=dict(player_id=last['player_id'],season_id=last['season_id'],prediction_at=cutoff,
        last_played_team_id=last['team_id'],last_game_id=last['game_id'],last_game_date=last['game_date'],
        days_since_last_appearance=(cutoff.astimezone(LEAGUE_TZ).date()-last['game_date']).days,
        max_source_available_at=max(max(r['available_at'],r.get('xg_available_at') or r['available_at']) for r in records),
        max_event_start_at=max((r['start_at'] for r in records if r.get('start_at')),default=None))
    metrics=PLAYER_METRICS if kind=='skater' else GOALIE_METRICS
    for window in WINDOWS:
        subset=records if window=='season' else records[-int(window[4:]):]
        n=len(subset)
        prefix=window+'_'
        result[prefix+'games']=n
        sums={name:total(metric(r,key) for r in subset) for name,key in metrics.items()}
        result.update({prefix+k:v for k,v in sums.items()})
        result[prefix+'toi_seconds_per_game']=ratio(sums['toi_seconds'],n)
        if kind=='skater':
            for key in ('goals','assists','points','shots'):
                result[prefix+key+'_per_game']=ratio(sums[key],n)
            for key in ('goals','points','shots'):
                result[prefix+key+'_per60']=ratio(sums[key],sums['toi_seconds'],3600)
            result[prefix+'shooting_pct']=ratio(sums['goals'],sums['shots'])
        else:
            saves=(sums['shots_against']-sums['goals_against']) if sums['shots_against'] is not None and sums['goals_against'] is not None else None
            result[prefix+'saves']=saves
            result[prefix+'save_pct']=ratio(saves,sums['shots_against'])
            result[prefix+'goals_against_per60']=ratio(sums['goals_against'],sums['toi_seconds'],3600)
        covered=[r['xg'] for r in subset if r.get('xg') is not None]
        result[prefix+'xg_covered_games']=len(covered)
        result[prefix+'unblocked_xg_covered_sum']=sum(covered) if covered else None
        result[prefix+'unblocked_xg_per_covered_game']=ratio(sum(covered),len(covered))
        result[prefix+'unblocked_xg']=sum(covered) if len(covered)==n else None
    if kind=='skater':
        values=[metric(r,'toi') for r in records[-10:]]
        result['toi_change_last5_vs_previous5_seconds']=(sum(values[-5:])/5-sum(values[:5])/5) if len(values)==10 and None not in values else None
    else:
        seven=[r for r in records if r.get('start_at') and r['start_at']>=cutoff-timedelta(days=7)]
        result['appearances_last7_days']=len(seven)
        result['starts_last7_days']=total(metric(r,'gamesStarted') for r in seven)
        result['toi_last7_days_seconds']=total(metric(r,'toi') for r in seven)
    return result


def team_features(team,season,records,known_games,standings,edge,cutoff,phase,advanced):
    records=sorted(records,key=ordering)
    availability=[r['available_at'] for r in records]+[g['available_at'] for g in known_games.values()
        if g['season_id']==season and team in (g['home_team_id'],g['away_team_id']) and g['available_at']<cutoff]
    result=dict(team_id=team,season_id=season,prediction_at=cutoff,last_game_id=records[-1]['game_id'] if records else None,
        last_game_date=records[-1]['game_date'] if records else None,
        days_since_last_game=(cutoff.astimezone(LEAGUE_TZ).date()-records[-1]['game_date']).days if records else None,
        max_source_available_at=max(availability,default=None),
        max_event_start_at=max((r['start_at'] for r in records if r.get('start_at')),default=None))
    for window in WINDOWS:
        rows=records if window=='season' else records[-int(window[4:]):]
        n=len(rows)
        prefix=window+'_'
        wins=sum(r['win'] for r in rows)
        gf=sum(r['gf'] for r in rows)
        ga=sum(r['ga'] for r in rows)
        points=sum(r['points'] for r in rows) if phase==2 else None
        result.update({prefix+'games':n,prefix+'wins':wins,prefix+'goals_for':gf,prefix+'goals_against':ga,
            prefix+'goal_differential':gf-ga,prefix+'points':points,prefix+'points_pct':ratio(points,2*n),
            prefix+'win_rate':ratio(wins,n),prefix+'goals_for_per_game':ratio(gf,n),prefix+'goals_against_per_game':ratio(ga,n)})
        covered=[]
        for row in rows:
            source=advanced.get((row['game_id'],row['home']))
            if not source or source['available_at']>=cutoff or source['game_date']!=row['game_date']:
                continue
            game=known_games[row['game_id']]
            shootout=game['result_type']=='SO'
            expected_gf=row['gf']-int(shootout and row['win'])
            expected_ga=row['ga']-int(shootout and not row['win'])
            if source['gf'] is None or source['ga'] is None or float(source['gf'])!=expected_gf or float(source['ga'])!=expected_ga:
                continue
            if source['xgf'] is None or source['xga'] is None or source['xgf']<0 or source['xga']<0:
                continue
            covered.append(source)
        result[prefix+'xg_covered_games']=len(covered)
        xgf=sum(r['xgf'] for r in covered) if covered else None
        xga=sum(r['xga'] for r in covered) if covered else None
        result.update({prefix+'xg_for_covered_sum':xgf,prefix+'xg_against_covered_sum':xga,
            prefix+'xg_for_per_covered_game':ratio(xgf,len(covered)),prefix+'xg_against_per_covered_game':ratio(xga,len(covered)),
            prefix+'xg_for':xgf if len(covered)==n and n else None,prefix+'xg_against':xga if len(covered)==n and n else None})
        if covered:
            result['max_source_available_at']=max(result['max_source_available_at'],max(r['available_at'] for r in covered))
    for side in ('home','away'):
        rows=[r for r in records if r['home']==(side=='home')]
        result[side+'_games']=len(rows)
        result[side+'_win_rate']=ratio(sum(r['win'] for r in rows),len(rows))
    future=[]
    for game in known_games.values():
        if game['season_id']!=season or game['available_at']>=cutoff or team not in (game['home_team_id'],game['away_team_id']):
            continue
        if game['is_final'] or game['game_state'] not in ('FUT','PRE') or game.get('schedule_state') not in ('OK','TBD'):
            continue
        start=timestamp(game['start_time_utc']) if game.get('start_time_utc') else None
        if (start and start>=cutoff) or (start is None and date.fromisoformat(game['game_date'])>=cutoff.astimezone(LEAGUE_TZ).date()):
            future.append(game)
    future.sort(key=lambda g:(g['game_date'],g.get('start_time_utc') or '9999'))
    next_game=future[0] if future else None
    result.update(known_upcoming_games=len(future),next_game_id=None,next_game_date=None,next_game_start_at=None,
                  next_opponent_team_id=None,next_game_is_home=None,rest_days_before_next_game=None,next_game_back_to_back=None)
    if next_game:
        result.update(next_game_id=next_game['game_id'],next_game_date=date.fromisoformat(next_game['game_date']),
            next_game_start_at=timestamp(next_game['start_time_utc']) if next_game.get('start_time_utc') else None,
            next_game_is_home=next_game['home_team_id']==team,
            next_opponent_team_id=next_game['away_team_id'] if next_game['home_team_id']==team else next_game['home_team_id'])
        result['max_source_available_at']=max(result['max_source_available_at'] or next_game['available_at'],next_game['available_at'])
        if records:
            gap=(date.fromisoformat(next_game['game_date'])-records[-1]['game_date']).days
            result['rest_days_before_next_game']=max(0,gap-1)
            result['next_game_back_to_back']=gap==1
    standing=standings.get((team,season))
    result.update(standings_date=None,standings_points=None,standings_points_pct=None,standings_league_rank=None,standings_age_days=None)
    if standing and standing['observed_at']<cutoff:
        p=standing['payload']
        result.update(standings_date=standing['standings_date'],standings_points=p['points'],
            standings_points_pct=p.get('pointPctg'),standings_league_rank=p.get('leagueSequence'),
            standings_age_days=(cutoff.astimezone(LEAGUE_TZ).date()-standing['standings_date']).days)
        result['max_source_available_at']=max(result['max_source_available_at'] or standing['observed_at'],standing['observed_at'])
    tracking=edge.get((team,season))
    result.update(edge_available=False,edge_games_played=None,edge_offensive_zone_pct=None,edge_offensive_zone_even_strength_pct=None,
                  edge_distance_per_game_km=None,edge_bursts_over20mph_per_game=None,edge_max_skating_speed_kmh=None)
    if tracking and tracking['observed_at']<cutoff:
        p=tracking['payload']
        gp=p['team']['gamesPlayed']
        result.update(edge_available=True,edge_games_played=gp,
            edge_offensive_zone_pct=nested(p,'zoneTimeDetails','offensiveZonePctg'),
            edge_offensive_zone_even_strength_pct=nested(p,'zoneTimeDetails','offensiveZoneEvPctg'),
            edge_distance_per_game_km=ratio(nested(p,'distanceSkated','total','metric'),gp),
            edge_bursts_over20mph_per_game=ratio(nested(p,'skatingSpeed','burstsOver20','value'),gp),
            edge_max_skating_speed_kmh=nested(p,'skatingSpeed','speedMax','metric'))
        result['max_source_available_at']=max(result['max_source_available_at'] or tracking['observed_at'],tracking['observed_at'])
    return result


def build_features(games,records,standings,edge,shot_actors,shot_coverage,cutoff,phase,team_advanced=None):
    games={key:game for key,game in games.items() if game['available_at']<cutoff}
    eligible={key:g for key,g in games.items() if past_final(g,cutoff)}
    actors=defaultdict(list)
    groups=defaultdict(list)
    for source in records:
        if source['available_at']>=cutoff or source['game_id'] not in eligible:
            continue
        row={**source,'cutoff':cutoff}
        row['xg'],row['xg_available_at']=attach_xg(row,shot_actors,shot_coverage)
        actors[(row['kind'],row['player_id'],row['season_id'])].append(row)
    for game in eligible.values():
        start=timestamp(game['start_time_utc']) if game.get('start_time_utc') else None
        for home in (True,False):
            team=game['home_team_id'] if home else game['away_team_id']
            gf,ga=(game['home_score'],game['away_score']) if home else (game['away_score'],game['home_score'])
            if gf is None or ga is None or gf==ga:
                raise ValueError('Eligible final game has no valid result')
            win=gf>ga
            groups[(team,game['season_id'])].append(dict(game_id=game['game_id'],game_date=date.fromisoformat(game['game_date']),
                start_at=start,available_at=game['available_at'],home=home,gf=gf,ga=ga,win=win,
                points=2 if win else (1 if game['result_type'] in ('OT','SO') else 0)))
    # Cold-start teams can be known through a published future schedule, but no
    # player membership is inferred from today's mutable roster.
    for game in games.values():
        for team in (game['home_team_id'],game['away_team_id']):
            groups.setdefault((team,game['season_id']),[])
    outputs={'players':[],'goalies':[],'teams':[]}
    for (kind,player,season),rows in sorted(actors.items()):
        outputs['players' if kind=='skater' else 'goalies'].append(actor_features(rows,kind,cutoff))
    for (team,season),rows in sorted(groups.items()):
        outputs['teams'].append(team_features(team,season,rows,games,standings,edge,cutoff,phase,team_advanced or {}))
    by_team={(r['team_id'],r['season_id']):r for r in outputs['teams']}
    for row in outputs['teams']:
        opponent=by_team.get((row['next_opponent_team_id'],row['season_id']))
        row['next_opponent_points_pct']=opponent['season_points_pct'] if opponent else None
        if opponent and opponent['max_source_available_at']:
            row['max_source_available_at']=max(row['max_source_available_at'] or opponent['max_source_available_at'],opponent['max_source_available_at'])
    for rows in outputs.values():
        for row in rows:
            row['game_type_id']=phase
            row['feature_version']=VERSION
            if row['max_source_available_at'] is not None and row['max_source_available_at']>=cutoff:
                raise ValueError('Future source leaked into features')
            if row['max_event_start_at'] is not None and row['max_event_start_at']>=cutoff:
                raise ValueError('Future result leaked into rolling features')
    coverage=[]
    for kind in ('skater','goalie'):
        for season in sorted({key[2] for key in actors if key[0]==kind}):
            rows=[r for key,values in actors.items() if key[0]==kind and key[2]==season for r in values]
            coverage.append(dict(kind=kind,season_id=season,player_games=len(rows),xg_covered_games=sum(r['xg'] is not None for r in rows)))
    return outputs,dict(known_games=len(games),eligible_final_games=len(eligible),coverage_by_kind_season=coverage,
        player_games=sum(len(v) for v in actors.values()),xg_covered_player_games=sum(r.get('xg') is not None for v in actors.values() for r in v))


def schemas():
    common={'season_id':pa.int64(),'game_type_id':pa.int64(),'prediction_at':pa.timestamp('us',tz='UTC'),
            'feature_version':pa.string(),'last_game_id':pa.int64(),'last_game_date':pa.date32(),
            'max_source_available_at':pa.timestamp('us',tz='UTC'),'max_event_start_at':pa.timestamp('us',tz='UTC')}
    result={}
    for dataset,kind,metrics in (('players','skater',PLAYER_METRICS),('goalies','goalie',GOALIE_METRICS)):
        fields={**common,'player_id':pa.int64(),'last_played_team_id':pa.int64(),'days_since_last_appearance':pa.int64()}
        for window in WINDOWS:
            prefix=window+'_'
            for key in ('games',*metrics,'xg_covered_games',*(['saves'] if kind=='goalie' else [])):
                fields[prefix+key]=pa.int64()
            rates=['toi_seconds_per_game','unblocked_xg','unblocked_xg_covered_sum','unblocked_xg_per_covered_game']
            rates+=['goals_per_game','assists_per_game','points_per_game','shots_per_game','goals_per60','points_per60','shots_per60','shooting_pct'] if kind=='skater' else ['save_pct','goals_against_per60']
            fields.update({prefix+k:pa.float64() for k in rates})
        if kind=='skater':
            fields['toi_change_last5_vs_previous5_seconds']=pa.float64()
        else:
            fields.update({k:pa.int64() for k in ('appearances_last7_days','starts_last7_days','toi_last7_days_seconds')})
        result[dataset]=pa.schema(fields)
    fields={**common,'team_id':pa.int64(),'days_since_last_game':pa.int64()}
    for window in WINDOWS:
        fields.update({window+'_'+k:pa.int64() for k in ('games','wins','goals_for','goals_against','goal_differential','points','xg_covered_games')})
        fields.update({window+'_'+k:pa.float64() for k in ('points_pct','win_rate','goals_for_per_game','goals_against_per_game')})
        fields.update({window+'_'+k:pa.float64() for k in ('xg_for','xg_against','xg_for_covered_sum','xg_against_covered_sum',
                                                          'xg_for_per_covered_game','xg_against_per_covered_game')})
    for k in ('home_games','away_games','known_upcoming_games','next_game_id','next_opponent_team_id','rest_days_before_next_game',
              'standings_points','standings_league_rank','standings_age_days','edge_games_played'):
        fields[k]=pa.int64()
    for k in ('home_win_rate','away_win_rate','standings_points_pct','edge_offensive_zone_pct','edge_offensive_zone_even_strength_pct',
              'edge_distance_per_game_km','edge_bursts_over20mph_per_game','edge_max_skating_speed_kmh','next_opponent_points_pct'):
        fields[k]=pa.float64()
    fields.update(next_game_date=pa.date32(),standings_date=pa.date32(),next_game_start_at=pa.timestamp('us',tz='UTC'),
                  next_game_is_home=pa.bool_(),next_game_back_to_back=pa.bool_(),edge_available=pa.bool_())
    result['teams']=pa.schema(fields)
    return result


def json_default(value):
    if isinstance(value,(datetime,date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def atomic_replace(temporary,path):
    # Windows readers/virus scanners can briefly deny replacement. Never delete
    # the old pointer or fall back to an in-place write of partially visible JSON.
    for attempt in range(9):
        try:
            temporary.replace(path)
            return
        except PermissionError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in (5,32,33) or attempt==8:raise
            time.sleep(min(0.025*2**attempt,0.25))


def write_document(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=json_default)+'\n',encoding='utf-8')
    atomic_replace(temporary,path)


def export_features(connection,cutoff,seasons,phase,output,publish=True):
    output.mkdir(parents=True,exist_ok=True)
    (output/'manifest.json').unlink(missing_ok=True)
    games=read_games(connection,cutoff,seasons,phase)
    records,player_provenance=read_player_games(connection,cutoff,seasons,phase,games)
    standings,edge=read_context(connection,cutoff,seasons,phase)
    actors,coverage,shot_provenance=read_shots(connection,cutoff,seasons,phase,{g for g,v in games.items() if past_final(v,cutoff)})
    advanced,team_files=read_team_advanced(connection,cutoff,seasons,phase,{g for g,v in games.items() if past_final(v,cutoff)})
    outputs,diagnostics=build_features(games,records,standings,edge,actors,coverage,cutoff,phase,advanced)
    artifacts={}
    for dataset,schema in schemas().items():
        rows=outputs[dataset]
        if any(set(row)!=set(schema.names) for row in rows):
            raise ValueError(f'Feature schema drift in {dataset}')
        schema=schema.with_metadata({b'feature_version':VERSION.encode(),b'prediction_at':cutoff.isoformat().encode(),
                                    b'availability_policy':b'strict source availability before prediction timestamp'})
        table=pa.Table.from_pylist(rows,schema=schema)
        output.mkdir(parents=True,exist_ok=True)
        temporary=output/f'{dataset}.parquet.tmp'
        pq.write_table(table,temporary,compression='zstd')
        temporary.replace(output/f'{dataset}.parquet')
        csv_temp=output/f'{dataset}.csv.tmp'
        with csv_temp.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=schema.names)
            writer.writeheader()
            writer.writerows({k:json_default(v) if isinstance(v,(datetime,date)) else v for k,v in row.items()} for row in rows)
        csv_temp.replace(output/f'{dataset}.csv')
        artifacts[dataset]=dict(rows=len(rows),schema=str(schema),sha256=hashlib.sha256((output/f'{dataset}.parquet').read_bytes()).hexdigest(),
                               null_counts={name:table[name].null_count for name in schema.names})
    manifest=dict(feature_version=VERSION,status='complete' if any(outputs.values()) else 'no_eligible_data',
        prediction_at=cutoff,generated_at=datetime.now(timezone.utc),seasons=seasons,game_type_id=phase,
        availability_policy='source observation strictly before prediction_at; final game known before cutoff; no canonical mutable stats',
        diagnostics=diagnostics,player_sources=player_provenance,moneypuck=shot_provenance,moneypuck_team_files=team_files,artifacts=artifacts,
        limitations=['Historical downloads are not backdated availability.','Names, current rosters and current biographies are excluded.',
                     'Derived xG sums source unblocked attempts (SHOT/MISS/GOAL); it is not a reproduced published goalie GSAx metric.',
                     'Zero xG requires known game-level shot coverage and matching NHL shot/goal counts. Missing coverage stays null.'])
    if publish:
        write_document(output/'manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prediction-at',type=timestamp,default=None,help='Timezone-aware ISO timestamp; defaults to now UTC')
    parser.add_argument('--seasons',type=season_argument,nargs='+',default=[20252026,20262027])
    parser.add_argument('--game-type',type=int,choices=[2,3],default=2)
    parser.add_argument('--output',type=Path,default=Path('data/features'))
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args=parser.parse_args()
    cutoff=args.prediction_at or datetime.now(timezone.utc)
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    with connect(args.env_file) as connection,connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        manifest=export_features(connection,cutoff,list(dict.fromkeys(args.seasons)),args.game_type,output,publish=False)
        from nhl_analytics.health import build_report,write_report
        report=build_report(connection,list(dict.fromkeys(args.seasons)),cutoff)
        report['feature_export']=dict(diagnostics=manifest['diagnostics'],artifacts=manifest['artifacts'])
        write_report(report,output)
        manifest['health_status']=report['status']
        manifest['health_report']='health.json'
        if report['status']=='error':
            manifest['status']='blocked_health_errors'
        write_document(output/'manifest.json',manifest)
    print(json.dumps(dict(status=manifest['status'],prediction_at=cutoff.isoformat(),output=str(output),
                         rows={k:v['rows'] for k,v in manifest['artifacts'].items()}),indent=2))
    if manifest['status']=='blocked_health_errors':
        raise SystemExit(1)


if __name__=='__main__':
    main()
