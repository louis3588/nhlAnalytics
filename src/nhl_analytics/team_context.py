"""Import dated NHL standings and optional team EDGE tracking snapshots."""

import argparse
import json
import logging
import math
from datetime import date,datetime,timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import requests

from nhl_analytics.database import connect,migrate,timestamp
from nhl_analytics.ingest import Client,WEB,season_argument,write_json
from nhl_analytics.schedules import digest

LOG=logging.getLogger(__name__)
STANDING_FIELDS={
    'games_played':'gamesPlayed','wins':'wins','losses':'losses','ot_losses':'otLosses','ties':'ties',
    'points':'points','points_pct':'pointPctg','goals_for':'goalFor','goals_against':'goalAgainst',
    'goal_differential':'goalDifferential','regulation_wins':'regulationWins','regulation_ot_wins':'regulationPlusOtWins',
    'league_rank':'leagueSequence','conference_rank':'conferenceSequence','division_rank':'divisionSequence',
    'wildcard_rank':'wildcardSequence','conference':'conferenceName','division':'divisionName',
    'streak_type':'streakCode','streak_count':'streakCount',
}


def metadata(envelope,kind):
    if envelope.get('schemaVersion')!=1 or envelope.get('source')!='NHL' or envelope.get('kind')!=kind:
        raise ValueError(f'Expected version-1 NHL {kind} snapshot')
    season=envelope['seasonId']
    if type(season) is not int or season%10000!=season//10000+1:
        raise ValueError('Invalid season')
    return season,timestamp(envelope['retrievedAt'])


def team_map(teams,season):
    ids=[t['id'] for t in teams]
    abbreviations=[t['abbrev'] for t in teams]
    if not teams or len(set(ids))!=len(ids) or len(set(abbreviations))!=len(abbreviations):
        raise ValueError('Missing/duplicate team identifiers')
    if any(t['seasonId']!=season or type(t['id']) is not int or t['id']<=0 for t in teams):
        raise ValueError('Team discovery season mismatch')
    return {t['abbrev']:t for t in teams}


def ensure_teams(connection,teams,observed):
    with connection.cursor() as cursor:
        cursor.executemany('''INSERT INTO analytics.teams(team_id,abbreviation,name,conference,division,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (team_id) DO UPDATE SET
            abbreviation=EXCLUDED.abbreviation,name=EXCLUDED.name,
            conference=COALESCE(EXCLUDED.conference,teams.conference),division=COALESCE(EXCLUDED.division,teams.division),
            updated_at=EXCLUDED.updated_at WHERE EXCLUDED.updated_at>=teams.updated_at''',
            [(t['id'],t['abbrev'],t['name']['default'],t.get('conference'),t.get('division'),observed) for t in teams])


def save_row(connection,table,history,keys,values,payload,envelope,path):
    observed=timestamp(envelope['retrievedAt'])
    key_values=tuple(values[k] for k in keys)
    condition=sql.SQL(' AND ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in keys)
    target=sql.Identifier('analytics',table)
    previous=connection.execute(sql.SQL('SELECT observed_at,content_sha256 FROM {} WHERE {}').format(target,condition),key_values).fetchone()
    checksum=digest(payload)
    if previous and observed<previous[0]:
        return 'older_skipped'
    changed=previous is None or checksum!=previous[1]
    if previous and changed and observed==previous[0]:
        raise ValueError('Cached snapshot content changed at an already imported timestamp')
    record={**values,'observed_at':observed,'content_sha256':checksum,'source_url':envelope['url'],
            'raw_path':str(Path(path).resolve()),'payload':Jsonb(payload)}
    columns=list(record)
    statement=sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}').format(
        target,sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder()*len(columns)),
        sql.SQL(',').join(map(sql.Identifier,keys)),
        sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c)) for c in columns if c not in keys))
    connection.execute(statement,tuple(record.values()))
    if changed:
        history_columns=[*keys,'observed_at','source_url','raw_path','payload']
        connection.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
            sql.Identifier('analytics',history),sql.SQL(',').join(map(sql.Identifier,history_columns)),
            sql.SQL(',').join(sql.Placeholder()*len(history_columns))),
            (*key_values,observed,envelope['url'],str(Path(path).resolve()),Jsonb(payload)))
    return 'changed' if changed else 'unchanged'


def validate_standings(envelope):
    season,_=metadata(envelope,'standings')
    as_of=envelope['standingsDate']
    date.fromisoformat(as_of)
    teams=team_map(envelope['teams'],season)
    rows=envelope['payload']['standings']
    seen=set()
    result=[]
    for row in rows:
        abbrev=row['teamAbbrev']['default']
        if abbrev in seen or abbrev not in teams:
            raise ValueError('Duplicate or unknown standings team')
        seen.add(abbrev)
        if row['seasonId']!=season or row['date']!=as_of or row.get('gameTypeId',2)!=2:
            raise ValueError('Standings season/date/game type mismatch')
        core={field:row.get(source) for field,source in STANDING_FIELDS.items()}
        core['ties']=row.get('ties',0)
        for field in ('games_played','wins','losses','ot_losses','ties','points','goals_for','goals_against'):
            if type(core[field]) is not int or core[field]<0:
                raise ValueError(f'Invalid standings metric: {field}')
        if core['games_played']!=core['wins']+core['losses']+core['ot_losses']+core['ties']:
            raise ValueError('Standings record does not equal games played')
        if core['points']!=2*core['wins']+core['ot_losses']+core['ties']:
            raise ValueError('Standings points disagree with record')
        if core['goal_differential']!=core['goals_for']-core['goals_against']:
            raise ValueError('Standings goal differential mismatch')
        pct=core['points_pct']
        if pct is not None and (not 0<=pct<=1 or (core['games_played'] and abs(pct-core['points']/(2*core['games_played']))>0.0001)):
            raise ValueError('Invalid standings points percentage')
        result.append(({'team_id':teams[abbrev]['id'],'season_id':season,'standings_date':as_of,**core},row))
    if seen!=set(teams):
        raise ValueError('Incomplete standings: not every season team is represented')
    return result


def load_standings(connection,envelope,path):
    rows=validate_standings(envelope)
    counts={'changed':0,'unchanged':0,'older_skipped':0}
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING',(envelope['seasonId'],))
        standings_by_abbrev={r['teamAbbrev']['default']:r for _,r in rows}
        teams=[{**t,'conference':standings_by_abbrev[t['abbrev']]['conferenceName'],
                'division':standings_by_abbrev[t['abbrev']]['divisionName']} for t in envelope['teams']]
        ensure_teams(connection,teams,timestamp(envelope['retrievedAt']))
        for values,raw in rows:
            status=save_row(connection,'team_standings','team_standings_observations',
                            ('team_id','season_id','standings_date'),values,raw,envelope,path)
            counts[status]+=1
    return counts


def nested(payload,*keys):
    value=payload
    for key in keys:
        if not isinstance(value,dict):
            return None
        value=value.get(key)
    return value


def normalize_edge(envelope):
    season,_=metadata(envelope,'team_edge')
    payload=envelope['payload']
    team=payload['team']
    if team['id']!=envelope['teamId'] or envelope['gameTypeId']!=2:
        raise ValueError('EDGE team/game type mismatch')
    if not any(s['id']==season and 2 in s['gameTypes'] for s in payload['seasonsWithEdgeStats']):
        raise ValueError('EDGE does not advertise requested season/game type')
    if type(team['gamesPlayed']) is not int or team['gamesPlayed']<0:
        raise ValueError('Invalid EDGE games played')
    locations={r['locationCode']:r for r in payload.get('sogSummary',[])}
    core=dict(team_id=team['id'],season_id=season,game_type_id=2,games_played=team['gamesPlayed'],
        skating_speed_max_kmh=nested(payload,'skatingSpeed','speedMax','metric'),
        speed_bursts_over20mph=nested(payload,'skatingSpeed','burstsOver20','value'),
        speed_bursts_over22mph=nested(payload,'skatingSpeed','burstsOver22','value'),
        total_distance_km=nested(payload,'distanceSkated','total','metric'),
        top_shot_speed_kmh=nested(payload,'shotSpeed','topShotSpeed','metric'),
        shot_attempts_over90mph=nested(payload,'shotSpeed','shotAttemptsOver90','value'),
        offensive_zone_pct=nested(payload,'zoneTimeDetails','offensiveZonePctg'),
        offensive_zone_even_strength_pct=nested(payload,'zoneTimeDetails','offensiveZoneEvPctg'),
        neutral_zone_pct=nested(payload,'zoneTimeDetails','neutralZonePctg'),
        defensive_zone_pct=nested(payload,'zoneTimeDetails','defensiveZonePctg'),
        shots_on_goal=locations.get('all',{}).get('shots'),
        high_danger_shots=locations.get('high',{}).get('shots'),high_danger_goals=locations.get('high',{}).get('goals'))
    for key,value in core.items():
        if value is None or key in ('team_id','season_id','game_type_id'):
            continue
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError(f'Invalid EDGE metric: {key}')
        if key.endswith('_pct') and value>1:
            raise ValueError('EDGE zone fractions must be between zero and one')
    zones=[core[k] for k in ('offensive_zone_pct','neutral_zone_pct','defensive_zone_pct')]
    if all(v is not None for v in zones) and abs(sum(zones)-1)>0.001:
        raise ValueError('EDGE zone fractions do not sum to one')
    if all(core[k] is None for k in ('offensive_zone_pct','total_distance_km','skating_speed_max_kmh')):
        raise ValueError('EDGE response has no usable tracking measurements')
    for key in ('speed_bursts_over20mph','speed_bursts_over22mph','shot_attempts_over90mph','shots_on_goal','high_danger_shots','high_danger_goals'):
        if core[key] is not None and type(core[key]) is not int:
            raise ValueError(f'EDGE count is not an integer: {key}')
    return core


def load_edge(connection,envelope,path):
    core=normalize_edge(envelope)
    teams=team_map(envelope['teams'],envelope['seasonId'])
    if not any(t['id']==core['team_id'] for t in teams.values()):
        raise ValueError('Unknown EDGE team ID')
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING',(envelope['seasonId'],))
        ensure_teams(connection,[t for t in teams.values() if t['id']==core['team_id']],timestamp(envelope['retrievedAt']))
        # Dropdown availability changes are not new tracking measurements.
        raw={k:v for k,v in envelope['payload'].items() if k!='seasonsWithEdgeStats'}
        return save_row(connection,'team_season_edge','team_edge_observations',
                        ('team_id','season_id','game_type_id'),core,raw,envelope,path)


def collect_standings(client,season,seasons,as_of=None):
    specification=next((s for s in seasons['seasons'] if s['id']==season),None)
    if specification is None:
        raise ValueError('Season not available in NHL standings metadata')
    cutoff=min(as_of or seasons['currentDate'],specification['standingsEnd'])
    if cutoff<specification['standingsStart']:
        raise ValueError('Requested date is before this season has standings')
    teams=client.get(f'{WEB}/schedule-calendar/{season//10000}-07-01')['teams']
    url=f'{WEB}/standings/{cutoff}'
    payload=client.get(url)
    envelope=dict(schemaVersion=1,source='NHL',kind='standings',seasonId=season,standingsDate=cutoff,
                  retrievedAt=datetime.now(timezone.utc).isoformat(),url=url,teams=teams,payload=payload)
    validate_standings(envelope)
    return envelope


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seasons',nargs='+',type=season_argument,default=[20252026,20262027])
    parser.add_argument('--as-of',type=lambda value:date.fromisoformat(value).isoformat())
    parser.add_argument('--edge',action='store_true',help='Also collect useful team EDGE tracking features')
    parser.add_argument('--load-json',type=Path,help='Replay one standings or EDGE snapshot')
    parser.add_argument('--output',type=Path,default=Path('data/nhl_team_context'))
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    run=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    client=Client(run/'http')
    failures=[]
    results=[]
    try:
        with connect(args.env_file) as connection:
            migrate(connection)
            if args.load_json:
                envelope=json.loads(args.load_json.read_text(encoding='utf-8'))
                loader=load_standings if envelope['kind']=='standings' else load_edge
                LOG.info('Replayed snapshot: %s',loader(connection,envelope,args.load_json))
                return
            seasons=client.get(f'{WEB}/standings-season')
            for season in dict.fromkeys(args.seasons):
                envelope=collect_standings(client,season,seasons,args.as_of)
                path=run/f'standings-{season}.json'
                write_json(path,envelope)
                result=load_standings(connection,envelope,path)
                results.append(dict(kind='standings',seasonId=season,date=envelope['standingsDate'],**result))
                LOG.info('%s standings at %s: %s',season,envelope['standingsDate'],result)
                if args.edge:
                    if args.as_of and envelope['standingsDate']<next(s['standingsEnd'] for s in seasons['seasons'] if s['id']==season):
                        raise ValueError('EDGE cannot be backdated to a historical standings cutoff')
                    for index,team in enumerate(envelope['teams'],1):
                        try:
                            url=f"{WEB}/edge/team-detail/{team['id']}/{season}/2"
                            payload=client.get(url)
                            edge=dict(schemaVersion=1,source='NHL',kind='team_edge',seasonId=season,gameTypeId=2,
                                      teamId=team['id'],teams=envelope['teams'],retrievedAt=datetime.now(timezone.utc).isoformat(),url=url,payload=payload)
                            path=run/'edge'/str(season)/f"{team['id']}.json"
                            write_json(path,edge)
                            status=load_edge(connection,edge,path)
                            results.append(dict(kind='team_edge',seasonId=season,teamId=team['id'],status=status))
                        except (requests.RequestException,ValueError,KeyError,TypeError,psycopg.Error) as error:
                            message=type(error).__name__ if isinstance(error,psycopg.Error) else str(error)
                            failures.append(dict(seasonId=season,teamId=team['id'],error=message))
                            LOG.error('EDGE %s %s: %s',season,team['abbrev'],message)
                        if index%8==0:
                            LOG.info('%s EDGE processed %s/%s teams',season,index,len(envelope['teams']))
                write_json(run/'progress.json',{'results':results,'failures':failures})
            write_json(run/'manifest.json',{'status':'failed' if failures else 'complete','results':results,'failures':failures})
            if failures:
                parser.exit(1,f'{len(failures)} EDGE snapshots failed. Successful data retained in {run}\n')
            LOG.info('Team-context import complete: %s',run)
    except psycopg.Error:
        parser.exit(1,'Database operation failed. Credentials are not logged.\n')
    except (requests.RequestException,ValueError,KeyError,TypeError,OSError) as error:
        parser.exit(1,f'Import failed: {error}. Raw directory: {run}\n')
    finally:
        client.session.close()


if __name__=='__main__':
    main()
