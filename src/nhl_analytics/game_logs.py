"""Fetch season-specific NHL skater/goalie game logs with resumable raw caching."""

import argparse
import hashlib
import json
import logging
import re
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
import requests
from psycopg import sql
from psycopg.types.json import Jsonb

from nhl_analytics.database import connect, migrate, timestamp
from nhl_analytics.ingest import Client, WEB, STATS, fetch_stats, season_argument, write_json

LOG = logging.getLogger(__name__)
SKATER_FIELDS = {
    'goals':'goals','assists':'assists','points':'points','plus_minus':'plusMinus',
    'penalty_minutes':'pim','power_play_goals':'powerPlayGoals','power_play_points':'powerPlayPoints',
    'shorthanded_goals':'shorthandedGoals','shorthanded_points':'shorthandedPoints',
    'game_winning_goals':'gameWinningGoals','overtime_goals':'otGoals','shots':'shots','shifts':'shifts',
}
GOALIE_FIELDS = {
    'games_started':'gamesStarted','decision':'decision','shots_against':'shotsAgainst',
    'goals_against':'goalsAgainst','save_pct':'savePctg','shutouts':'shutouts',
    'goals':'goals','assists':'assists','penalty_minutes':'pim',
}


def toi_seconds(value):
    if value is None:
        return None
    if not isinstance(value,str) or not re.fullmatch(r'\d+:[0-5]\d',value):
        raise ValueError(f'Invalid ice time: {value!r}')
    minutes,seconds = value.split(':')
    return int(minutes)*60+int(seconds)


def validate_log(envelope):
    season,phase,kind = envelope['seasonId'],envelope['gameTypeId'],envelope['kind']
    if kind not in ('skater','goalie') or phase not in (2,3):
        raise ValueError('Invalid kind or game type')
    if type(season) is not int or season % 10000 != season // 10000 + 1 or type(envelope['playerId']) is not int or envelope['playerId'] <= 0:
        raise ValueError('Invalid player/season')
    timestamp(envelope['retrievedAt'])
    payload = envelope['payload']
    if not isinstance(payload,dict):
        raise ValueError('Expected a game-log response object')
    if payload.get('seasonId') != season or payload.get('gameTypeId') != phase:
        raise ValueError('Game log season/game type does not match request')
    rows = payload.get('gameLog')
    expected = envelope['expectedGames']
    if type(expected) is not int or expected < 1 or not isinstance(rows,list) or len(rows) < expected:
        raise ValueError(f'Incomplete game log: expected at least {expected} games')
    seen = set()
    for row in rows:
        game = row['gameId']
        if type(game) is not int or game // 1_000_000 != season // 10000 or game // 10000 % 100 != phase:
            raise ValueError(f'Game ID disagrees with season/game type: {game}')
        if game in seen:
            raise ValueError(f'Duplicate player/game: {game}')
        seen.add(game)
        date.fromisoformat(row['gameDate'])
        if row['homeRoadFlag'] not in ('H','R') or not row['teamAbbrev'] or not row['opponentAbbrev']:
            raise ValueError('Missing/invalid game team context')
        if row['teamAbbrev'] == row['opponentAbbrev']:
            raise ValueError('A team cannot play itself')
        toi_seconds(row.get('toi'))
        required = ('goals','assists','points') if kind == 'skater' else ('shotsAgainst','goalsAgainst')
        for field in required:
            if type(row.get(field)) is not int or row[field] < 0:
                raise ValueError(f'Missing/invalid {kind} statistic {field}')
        if kind == 'goalie' and row['goalsAgainst'] > row['shotsAgainst']:
            raise ValueError('Goals against exceeds shots against')
    # Response ordering and dropdown metadata do not create false data revisions.
    content = json.dumps(sorted(rows,key=lambda r:r['gameId']),sort_keys=True,separators=(',',':'))
    return rows, hashlib.sha256(content.encode()).hexdigest()


def load_game_log(connection,envelope,raw_path):
    rows,checksum = validate_log(envelope)
    player,season,phase,kind = (envelope[k] for k in ('playerId','seasonId','gameTypeId','kind'))
    observed = timestamp(envelope['retrievedAt'])
    key = (player,season,phase,kind)
    source_id = uuid5(NAMESPACE_URL,f'NHL-game-log:{key}:{observed.isoformat()}:{checksum}')
    with connection.transaction():
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        state = connection.execute('''SELECT content_sha256,last_checked_at FROM analytics.game_log_state
            WHERE player_id=%s AND season_id=%s AND game_type_id=%s AND kind=%s''',key).fetchone()
        if state and observed < state[1]:
            return 'older-cache-skipped'
        if state and checksum == state[0]:
            connection.execute('''UPDATE analytics.game_log_state SET last_checked_at=%s
                WHERE player_id=%s AND season_id=%s AND game_type_id=%s AND kind=%s''',(observed,*key))
            return 'unchanged'
        connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING',(season,))
        connection.execute('''INSERT INTO analytics.players(player_id,full_name,updated_at) VALUES (%s,%s,%s)
            ON CONFLICT (player_id) DO NOTHING''',(player,envelope.get('fullName'),observed))
        connection.execute('''INSERT INTO analytics.game_log_sources
            (source_id,player_id,season_id,game_type_id,kind,observed_at,content_sha256,source_url,raw_path,game_count,expected_games)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (source_id,*key,observed,checksum,envelope['url'],str(Path(raw_path).resolve()),len(rows),envelope['expectedGames']))
        with connection.cursor() as cursor:
            cursor.executemany('''INSERT INTO analytics.games
                (game_id,season_id,game_type_id,game_date,home_team_abbrev,away_team_abbrev,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (game_id) DO UPDATE SET
                game_date=EXCLUDED.game_date,home_team_abbrev=EXCLUDED.home_team_abbrev,
                away_team_abbrev=EXCLUDED.away_team_abbrev,updated_at=EXCLUDED.updated_at
                WHERE EXCLUDED.updated_at >= games.updated_at''',
                [(r['gameId'],season,phase,r['gameDate'],
                  r['teamAbbrev'] if r['homeRoadFlag']=='H' else r['opponentAbbrev'],
                  r['opponentAbbrev'] if r['homeRoadFlag']=='H' else r['teamAbbrev'],observed) for r in rows])
            fields = SKATER_FIELDS if kind=='skater' else GOALIE_FIELDS
            table = sql.Identifier('analytics',f'{kind}_game_stats')
            columns = ['player_id','game_id','season_id','game_type_id','team_abbrev','opponent_abbrev','home_road',
                       *fields,'toi_seconds',*(['saves'] if kind=='goalie' else []),'source_id','payload']
            records = []
            for row in rows:
                record = [player,row['gameId'],season,phase,row['teamAbbrev'],row['opponentAbbrev'],row['homeRoadFlag'],
                          *(row.get(field) for field in fields.values()),toi_seconds(row.get('toi'))]
                if kind == 'goalie':
                    record.append(row['shotsAgainst']-row['goalsAgainst'])
                records.append((*record,source_id,Jsonb(row)))
            statement = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (player_id,game_id) DO UPDATE SET {}').format(
                table,sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder()*len(columns)),
                sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c)) for c in columns[2:]))
            cursor.executemany(statement,records)
            # A validated complete replacement can retract erroneous source records;
            # previous versions remain in observation history and raw files.
            cursor.execute(sql.SQL('DELETE FROM {} WHERE player_id=%s AND season_id=%s AND game_type_id=%s AND NOT (game_id=ANY(%s))').format(table),
                           (player,season,phase,[r['gameId'] for r in rows]))
            cursor.executemany('''INSERT INTO analytics.player_game_observations
                (source_id,player_id,game_id,kind,observed_at,payload) VALUES (%s,%s,%s,%s,%s,%s)''',
                [(source_id,player,r['gameId'],kind,observed,Jsonb(r)) for r in rows])
        connection.execute('''INSERT INTO analytics.game_log_state
            (player_id,season_id,game_type_id,kind,content_sha256,source_id,last_checked_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (player_id,season_id,game_type_id,kind)
            DO UPDATE SET content_sha256=EXCLUDED.content_sha256,source_id=EXCLUDED.source_id,last_checked_at=EXCLUDED.last_checked_at''',
            (*key,checksum,source_id,observed))
    return 'loaded'


def discover(client,seasons,phase):
    participants = []
    for season in seasons:
        for kind in ('skater','goalie'):
            for row in fetch_stats(client,kind,season,phase):
                if row['gamesPlayed'] > 0:
                    participants.append(dict(playerId=row['playerId'],seasonId=season,gameTypeId=phase,kind=kind,
                                             expectedGames=row['gamesPlayed'],fullName=row.get(f'{kind}FullName')))
    if not participants:
        raise ValueError('No season participants found')
    return participants


def fetch_report(client,participant,report):
    url = f"{STATS}/{participant['kind']}/{report}"
    rows,total,source_url = [],None,None
    while total is None or len(rows) < total:
        params = {'isAggregate':'false','isGame':'true','start':len(rows),'limit':100,
                  'sort':json.dumps([{'property':'gameId','direction':'ASC'}]),
                  'cayenneExp':f"seasonId={participant['seasonId']} and gameTypeId={participant['gameTypeId']} and playerId={participant['playerId']}"}
        if source_url is None:
            source_url = requests.Request('GET',url,params=params).prepare().url
        payload = client.get(url,params)
        page,total = payload['data'],payload['total']
        if not isinstance(page,list) or type(total) is not int or total < 0:
            raise ValueError('Invalid NHL per-game report pagination')
        if not page and len(rows) < total:
            raise ValueError('Incomplete NHL per-game report pagination')
        rows.extend(page)
        if len(rows) > total:
            raise ValueError('NHL per-game report exceeded its declared total')
        if not page:
            break
    if any(row['playerId'] != participant['playerId'] for row in rows):
        raise ValueError('NHL report returned another player')
    return rows,source_url


def fetch_report_fallback(client,participant,reason,primary_url):
    summary,url = fetch_report(client,participant,'summary')
    kind = participant['kind']
    ice_rows = fetch_report(client,participant,'timeonice')[0] if kind == 'skater' else []
    ice_by_game = {row['gameId']:row for row in ice_rows}
    aliases = {'pim':'penaltyMinutes','powerPlayGoals':'ppGoals','powerPlayPoints':'ppPoints',
               'shorthandedGoals':'shGoals','shorthandedPoints':'shPoints','savePctg':'savePct'}
    normalized = []
    for row in summary:
        record = dict(gameId=row['gameId'],gameDate=row['gameDate'],teamAbbrev=row['teamAbbrev'],
                      opponentAbbrev=row['opponentTeamAbbrev'],homeRoadFlag=row['homeRoad'])
        for field in (SKATER_FIELDS if kind == 'skater' else GOALIE_FIELDS).values():
            source_field = aliases.get(field,field)
            if source_field in row:
                record[field] = row[source_field]
        seconds = row.get('timeOnIcePerGame' if kind == 'skater' else 'timeOnIce')
        if seconds is not None:
            if type(seconds) not in (int,float) or seconds < 0 or not float(seconds).is_integer():
                raise ValueError('Expected whole-second NHL per-game ice time')
            record['toi'] = f'{int(seconds)//60}:{int(seconds)%60:02d}'
        if kind == 'skater':
            if row['gameId'] not in ice_by_game:
                raise ValueError('Missing per-game time-on-ice supplement')
            record['shifts'] = ice_by_game[row['gameId']]['shifts']
        normalized.append(record)
    envelope = {**participant,'url':url,'primaryUrl':primary_url,'fallbackReason':reason,
                'retrievedAt':datetime.now(timezone.utc).isoformat(),
                'payload':dict(seasonId=participant['seasonId'],gameTypeId=participant['gameTypeId'],
                               gameLog=normalized,summaryGameRows=summary,timeOnIceRows=ice_rows)}
    validate_log(envelope)
    return envelope


def run_import(connection,run,seasons,phase,resume=False,workers=2):
    plan_path = run/'plan.json'
    attempt = run/'http'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    client = Client(attempt)
    local = threading.local()
    worker_clients = []
    failures,counts = [],{'loaded':0,'unchanged':0,'older-cache-skipped':0}
    def fetch_participant(participant):
        player,season,kind = (participant[k] for k in ('playerId','seasonId','kind'))
        path = run/'players'/str(season)/kind/f'{player}.json'
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8')),path
        if not hasattr(local,'client'):
            local.client = Client(attempt/threading.current_thread().name)
            worker_clients.append(local.client)
        url = f"{WEB}/player/{player}/game-log/{season}/{participant['gameTypeId']}"
        payload = local.client.get(url)
        envelope = {**participant,'url':url,'retrievedAt':datetime.now(timezone.utc).isoformat(),'payload':payload}
        try:
            validate_log(envelope)
        except ValueError as error:
            LOG.warning('%s %s %s: game log incomplete/invalid; checking NHL per-game reports',season,kind,player)
            envelope = fetch_report_fallback(local.client,participant,str(error),url)
        write_json(path,envelope)
        return envelope,path
    try:
        if resume:
            participants = json.loads(plan_path.read_text(encoding='utf-8'))['participants']
        else:
            participants = discover(client,seasons,phase)
            write_json(plan_path,{'schemaVersion':1,'participants':participants})
        LOG.info('Import plan: %s player-seasons; run directory %s',len(participants),run)
        # At most two responses are in flight; sessions and raw archive paths are
        # thread-local. All writes to the database remain on this main thread.
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='nhl-fetch') as pool:
            pending = deque()
            iterator = iter(enumerate(participants,1))
            def schedule():
                item = next(iterator,None)
                if item is not None:
                    index,participant = item
                    pending.append((index,participant,pool.submit(fetch_participant,participant)))
            for _ in range(workers):
                schedule()
            while pending:
                index,participant,future = pending.popleft()
                try:
                    envelope,path = future.result()
                    status = load_game_log(connection,envelope,path)
                    counts[status] += 1
                except (requests.RequestException,ValueError,KeyError,TypeError,OSError,psycopg.Error) as error:
                    # DB exceptions can contain connection details: record their class only.
                    message = type(error).__name__ if isinstance(error,psycopg.Error) else str(error)
                    failures.append({**participant,'error':message})
                    LOG.error('%s %s %s: %s',participant['seasonId'],participant['kind'],participant['playerId'],message)
                if index % 25 == 0 or index == len(participants):
                    write_json(run/'progress.json',{'processed':index,'total':len(participants),**counts,'failures':failures})
                    LOG.info('Processed %s/%s player-seasons; %s failures',index,len(participants),len(failures))
                schedule()
        write_json(run/'manifest.json',{'status':'failed' if failures else 'complete','total':len(participants),
                                      **counts,'failures':failures,'finishedAt':datetime.now(timezone.utc).isoformat()})
        return failures
    finally:
        client.session.close()
        for worker_client in worker_clients:
            worker_client.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seasons',nargs='+',type=season_argument,default=[20252026,20262027])
    parser.add_argument('--game-type',type=int,choices=[2,3],default=2)
    parser.add_argument('--output',type=Path,default=Path('data/nhl_game_logs'))
    parser.add_argument('--resume',type=Path,help='Resume an existing run directory without refetching cached responses')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--workers',type=int,choices=[1,2],default=2,help='Bounded concurrent HTTP requests; DB writes remain sequential')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    run = args.resume or args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    try:
        with connect(args.env_file) as connection:
            migrate(connection)
            failures = run_import(connection,run,list(dict.fromkeys(args.seasons)),args.game_type,bool(args.resume),args.workers)
        if failures:
            parser.exit(1,f'{len(failures)} player-seasons failed. Resume with --resume {run}\n')
        LOG.info('Game-log import complete: %s',run)
    except psycopg.Error:
        parser.exit(1,'Database connection/migration failed. Credentials are not logged.\n')
    except (requests.RequestException,ValueError,KeyError,OSError) as error:
        parser.exit(1,f'Import failed: {error}. Run directory: {run}\n')


if __name__ == '__main__':
    main()
