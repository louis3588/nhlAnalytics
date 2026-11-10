"""Import NHL regular-season/playoff fixtures and results; retain JSON snapshots."""

import argparse
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import requests

from nhl_analytics.database import connect, migrate, timestamp
from nhl_analytics.ingest import Client, WEB, season_argument, write_json

LOG = logging.getLogger(__name__)
FIELDS = ('game_id','season_id','game_type_id','game_date','home_team_abbrev','away_team_abbrev',
          'home_team_id','away_team_id','start_time_utc','venue','venue_timezone','neutral_site',
          'game_state','schedule_state','home_score','away_score','is_final','result_type','winner_team_id')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def normalize_game(game,season,types):
    game_id,phase = game['id'],game['gameType']
    if type(game_id) is not int or game['season'] != season or phase not in types:
        raise ValueError('Unexpected schedule game season/type')
    if game_id//1_000_000 != season//10000 or game_id//10000%100 != phase:
        raise ValueError('Schedule game ID disagrees with season/type')
    date.fromisoformat(game['gameDate'])
    home,away = game['homeTeam'],game['awayTeam']
    if any(type(t['id']) is not int or t['id']<=0 or not t['abbrev'] for t in (home,away)) or home['id']==away['id']:
        raise ValueError('Invalid home/away teams')
    for team in (home,away):
        score = team.get('score')
        if score is not None and (type(score) is not int or score<0):
            raise ValueError('Invalid game score')
    state = game['gameState']
    if not isinstance(state,str) or not state:
        raise ValueError('Missing game state')
    final = state in ('OFF','FINAL')
    result,winner = None,None
    if final:
        if home.get('score') is None or away.get('score') is None or home['score']==away['score']:
            raise ValueError('Final game requires unequal nonnegative scores')
        result = game.get('gameOutcome',{}).get('lastPeriodType') or game.get('periodDescriptor',{}).get('periodType')
        if result not in ('REG','OT','SO'):
            raise ValueError('Final game missing a recognized result type')
        winner = home['id'] if home['score']>away['score'] else away['id']
    start = game.get('startTimeUTC')
    if start:
        timestamp(start)
    neutral = game.get('neutralSite')
    if neutral is not None and type(neutral) is not bool:
        raise ValueError('Invalid neutral-site flag')
    return dict(game_id=game_id,season_id=season,game_type_id=phase,game_date=game['gameDate'],
                home_team_abbrev=home['abbrev'],away_team_abbrev=away['abbrev'],
                home_team_id=home['id'],away_team_id=away['id'],start_time_utc=start or None,
                venue=game.get('venue',{}).get('default'),venue_timezone=game.get('venueTimezone'),neutral_site=neutral,
                game_state=state,schedule_state=game.get('gameScheduleState'),home_score=home.get('score'),
                away_score=away.get('score'),is_final=final,result_type=result,winner_team_id=winner)


def fetch_season(client,season,types):
    # Discover that season's franchises instead of hardcoding today's team list.
    calendar = client.get(f'{WEB}/schedule-calendar/{season//10000}-07-01')
    teams = calendar['teams']
    ids = [team['id'] for team in teams]
    if not teams or len(set(ids))!=len(ids) or any(t['seasonId']!=season for t in teams):
        raise ValueError('Invalid season-specific schedule team discovery')
    selected,seen = {},{}
    for index,team in enumerate(teams,1):
        payload = client.get(f"{WEB}/club-schedule-season/{team['abbrev']}/{season}")
        rows = payload['games']
        if not isinstance(rows,list):
            raise ValueError('Missing club schedule game list')
        count,club_ids = 0,set()
        for game in rows:
            if game['gameType'] not in types:
                continue
            core = normalize_game(game,season,types)
            key = core['game_id']
            if key in club_ids:
                raise ValueError('Duplicate game in one club schedule')
            club_ids.add(key)
            if team['id'] not in (core['home_team_id'],core['away_team_id']):
                raise ValueError('Club schedule contains an unrelated team')
            if key in selected:
                previous = normalize_game(selected[key],season,types)
                for field in ('season_id','game_type_id','home_team_id','away_team_id'):
                    if previous[field]!=core[field]:
                        raise ValueError('Club schedules disagree on game identity')
            # Prefer the most recently retrieved representation if live scores or
            # schedule times changed between the home and away club requests.
            selected[key] = game
            seen.setdefault(key,set()).add(team['id'])
            count += 1
        if 2 in types and not count:
            raise ValueError(f"No selected season games for {team['abbrev']}")
        LOG.info('%s: fetched %s/%s clubs (%s)',season,index,len(teams),team['abbrev'])
    for key,game in selected.items():
        if seen[key] != {game['homeTeam']['id'],game['awayTeam']['id']}:
            raise ValueError(f'Game {key} is missing from one participating club schedule')
    if not selected:
        raise ValueError('No schedule games available for this season/type selection')
    return dict(schemaVersion=1,source='NHL',seasonId=season,gameTypes=sorted(types),
                retrievedAt=datetime.now(timezone.utc).isoformat(),teams=teams,
                games=[selected[key] for key in sorted(selected)])


def load_season(connection,envelope,path):
    if envelope.get('schemaVersion')!=1 or envelope.get('source')!='NHL':
        raise ValueError('Expected a version-1 NHL schedule snapshot')
    season,types = envelope['seasonId'],envelope['gameTypes']
    if type(season) is not int or season%10000!=season//10000+1 or not types or any(t not in (2,3) for t in types):
        raise ValueError('Invalid season/game types')
    observed = timestamp(envelope['retrievedAt'])
    cores = [normalize_game(game,season,types) for game in envelope['games']]
    ids = [core['game_id'] for core in cores]
    if not ids or len(set(ids))!=len(ids):
        raise ValueError('Empty schedule or duplicate game IDs')
    teams = {team['id']:team for team in envelope['teams']}
    if any(c['home_team_id'] not in teams or c['away_team_id'] not in teams for c in cores):
        raise ValueError('Schedule game references an undiscovered team')
    checksum = digest(sorted(cores,key=lambda c:c['game_id']))
    source_id = uuid5(NAMESPACE_URL,f'NHL-schedule:{season}:{sorted(types)}:{observed.isoformat()}')
    counts = dict(changed=0,unchanged=0,older_skipped=0)
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        prior = connection.execute('SELECT content_sha256 FROM analytics.schedule_imports WHERE source_id=%s',(source_id,)).fetchone()
        if prior and prior[0]!=checksum:
            raise ValueError('Previously imported schedule snapshot was modified')
        connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING',(season,))
        connection.execute('''INSERT INTO analytics.schedule_imports
            (source_id,season_id,game_types,observed_at,content_sha256,raw_path,game_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (source_id,season,sorted(set(types)),observed,checksum,str(Path(path).resolve()),len(cores)))
        with connection.cursor() as cursor:
            cursor.executemany('''INSERT INTO analytics.teams(team_id,abbreviation,name,updated_at)
                VALUES (%s,%s,%s,%s) ON CONFLICT (team_id) DO UPDATE SET
                abbreviation=EXCLUDED.abbreviation,name=EXCLUDED.name,updated_at=EXCLUDED.updated_at
                WHERE EXCLUDED.updated_at >= teams.updated_at''',
                [(t['id'],t['abbrev'],t['name']['default'],observed) for t in teams.values()])
        columns = [*FIELDS,'updated_at','schedule_checked_at','schedule_content_sha256','schedule_payload']
        updates = [sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c)) for c in columns[1:] if c!='updated_at']
        updates.append(sql.SQL('updated_at=GREATEST(games.updated_at,EXCLUDED.updated_at)'))
        statement = sql.SQL('INSERT INTO analytics.games ({}) VALUES ({}) ON CONFLICT (game_id) DO UPDATE SET {}').format(
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder()*len(columns)),sql.SQL(',').join(updates))
        existing = {row[0]:row[1:] for row in connection.execute('''SELECT game_id,schedule_checked_at,schedule_content_sha256
            FROM analytics.games WHERE season_id=%s AND game_type_id=ANY(%s)''',(season,types)).fetchall()}
        changes,observations = [],[]
        for core,raw in zip(cores,envelope['games']):
            checked,previous_hash = existing.get(core['game_id'],(None,None))
            if checked and observed<checked:
                counts['older_skipped'] += 1
                continue
            content_hash = digest(core)
            counts['unchanged' if previous_hash==content_hash else 'changed'] += 1
            changes.append((*(core[field] for field in FIELDS),observed,observed,content_hash,Jsonb(raw)))
            if previous_hash != content_hash:
                observations.append((core['game_id'],observed,source_id,Jsonb(dict(normalized=core,sourceGame=raw))))
        with connection.cursor() as cursor:
            cursor.executemany(statement,changes)
            cursor.executemany('''INSERT INTO analytics.game_schedule_observations
                (game_id,observed_at,source_id,payload) VALUES (%s,%s,%s,%s)''',observations)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seasons',nargs='+',type=season_argument,default=[20252026,20262027])
    parser.add_argument('--game-types',nargs='+',type=int,choices=[2,3],default=[2,3])
    parser.add_argument('--output',type=Path,default=Path('data/nhl_schedules'))
    parser.add_argument('--load-json',type=Path,help='Replay one saved season snapshot without HTTP requests')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    run = args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    client = Client(run/'http')
    try:
        with connect(args.env_file) as connection:
            migrate(connection)
            if args.load_json:
                result = load_season(connection,json.loads(args.load_json.read_text(encoding='utf-8')),args.load_json)
                LOG.info('Loaded cached schedule: %s',result)
            else:
                results = []
                for season in dict.fromkeys(args.seasons):
                    envelope = fetch_season(client,season,set(args.game_types))
                    path = run/f'{season}.json'
                    write_json(path,envelope)
                    result = load_season(connection,envelope,path)
                    results.append(dict(seasonId=season,gameCount=len(envelope['games']),**result))
                    write_json(run/'progress.json',{'seasons':results})
                    LOG.info('%s loaded: %s',season,result)
                write_json(run/'manifest.json',{'status':'complete','seasons':results})
                LOG.info('Schedules/results import complete: %s',run)
    except psycopg.Error:
        parser.exit(1,f'Database operation failed. Raw JSON remains in {run}; credentials are not logged.\n')
    except (requests.RequestException,ValueError,KeyError,TypeError,OSError) as error:
        parser.exit(1,f'Schedule import failed: {error}. Raw responses, if any: {run}\n')
    finally:
        client.session.close()


if __name__ == '__main__':
    main()
