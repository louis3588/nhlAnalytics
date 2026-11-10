"""Stream local MoneyPuck CSVs to TimescaleDB in resumable COPY batches."""

import argparse
import csv
import hashlib
import logging
import math
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.types.json import Jsonb

from nhl_analytics.database import connect, migrate

LOG = logging.getLogger(__name__)
COLUMNS = ('file_id','row_number','observed_at','kind','record_key','season_id','game_type_id',
           'entity_id','player_id','goalie_id','line_player_ids','team_code','situation','game_id',
           'game_date','xgoals_for','xgoals_against','goals','ice_time','payload')


def integer(value, optional=False):
    if optional and str(value).strip().lower() in ('','none','nan','na','null','0','0.0','-1','-1.0'):
        return None
    try:
        number = Decimal(str(value))
        if optional and number in (0,-1):
            return None
        if not number.is_finite() or number != number.to_integral_value() or number < 0:
            raise ValueError(f'Invalid integer ID/value: {value!r}')
        return int(number)
    except InvalidOperation as error:
        raise ValueError(f'Invalid integer ID/value: {value!r}') from error


def number(value):
    if value is None or str(value).lower() in ('','nan','na','null'):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite measurement')
    return result


def detect_kind(headers):
    fields = set(headers)
    if 'gameId' in fields and 'playerId' in fields:
        raise ValueError('Individual player-game CSVs need their own game-level mapping; not supported yet')
    if {'shooterPlayerId','game_id','id','season'} <= fields:
        return 'shot'
    if {'gameId','gameDate','team','season','situation'} <= fields and 'playerId' not in fields:
        return 'team_game'
    if {'lineId','season','situation','team'} <= fields:
        return 'line_season'
    if {'playerId','season','situation','team'} <= fields:
        if 'I_F_xGoals' in fields:
            return 'skater_season'
        if 'xGoals' in fields:
            return 'goalie_season'
    if {'team','season','situation','xGoalsFor'} <= fields:
        return 'team_season'
    raise ValueError('Unsupported MoneyPuck CSV header')


def csv_rows(reader, headers):
    # DictReader would silently discard duplicated header columns.
    for row in reader:
        if not row:
            continue
        if len(row) != len(headers):
            raise ValueError(f'CSV line {reader.line_num}: expected {len(headers)} fields, got {len(row)}')
        result = {}
        for name, value in zip(headers, row):
            if name in result and result[name] != value:
                raise ValueError(f'CSV line {reader.line_num}: conflicting duplicate column {name}')
            result[name] = value
        yield result


def transform(row, kind, file_id, row_number, observed, default_game_type):
    year = integer(row['season'])
    if not 1900 <= year <= 2100:
        raise ValueError(f'Unexpected MoneyPuck season {year}')
    season = year * 10000 + year + 1
    playoff = row.get('isPlayoffGame', row.get('playoffGame'))
    if playoff is not None and integer(playoff) not in (0,1):
        raise ValueError('Invalid playoff flag')
    game_type = (3 if integer(playoff) else 2) if playoff is not None else default_game_type
    player = goalie = game_id = game_date = None
    player_name = goalie_name = None
    members = []
    situation = row.get('situation','all')
    team = row.get('team')
    if not situation or not team:
        raise ValueError('Missing team or situation')
    if kind == 'shot':
        short_game = integer(row['game_id'])
        game_id = short_game if short_game >= 1_000_000_000 else year * 1_000_000 + short_game
        if game_id // 1_000_000 != year or (game_id // 10000) % 100 != game_type:
            raise ValueError('Shot game ID disagrees with season/playoff flag')
        event_id = integer(row['id'])
        entity = str(event_id)
        player, goalie = integer(row['shooterPlayerId'], True), integer(row.get('goalieIdForShot',''), True)
        player_name, goalie_name = row.get('shooterName'), row.get('goalieNameForShot')
        if team not in ('HOME','AWAY'):
            raise ValueError('Unexpected shot team side')
        team = row['homeTeamCode'] if team == 'HOME' else row['awayTeamCode']
        key = f'{season}|{game_id}|{event_id}'
        xfor, xagainst, goals, ice = number(row.get('xGoal')), None, number(row.get('goal')), None
    else:
        if kind in ('skater_season','goalie_season'):
            player = integer(row['playerId'])
            if player <= 0:
                raise ValueError('Missing player ID')
            player_name = row.get('name')
            entity = str(player)
        elif kind == 'line_season':
            entity = row['lineId']
            if not entity.isdigit() or len(entity) not in (14,21):
                raise ValueError('Cannot decode line ID into 7-digit NHL player IDs')
            members = [int(entity[i:i+7]) for i in range(0,len(entity),7)]
            if len(set(members)) != len(members) or any(p <= 0 for p in members):
                raise ValueError('Invalid line member IDs')
        else:
            entity = team
        if kind == 'team_game':
            game_id = integer(row['gameId'])
            game_date = datetime.strptime(row['gameDate'], '%Y%m%d').date()
            if game_id // 1_000_000 != year or (game_id // 10000) % 100 != game_type:
                raise ValueError('Team game ID disagrees with season/playoff flag')
        key = f'{kind}|{season}|{game_type}|{entity}|{team}|{situation}|{game_id or ""}'
        xfor = number(row.get('I_F_xGoals',row.get('xGoalsFor')))
        xagainst = number(row.get('xGoalsAgainst',row.get('xGoals') if kind == 'goalie_season' else None))
        goals = number(row.get('I_F_goals',row.get('goalsFor',row.get('goals'))))
        ice = number(row.get('icetime',row.get('iceTime')))
    return (file_id,row_number,observed,kind,key,season,game_type,entity,player,goalie,members,
            team,situation,game_id,game_date,xfor,xagainst,goals,ice,Jsonb(row),player_name,goalie_name)


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_batch(connection, batch, file_id, processed):
    with connection.transaction():
        connection.execute('TRUNCATE mp_batch')
        # Only stage identity metadata. Large JSON payloads are sent once, directly
        # to the final hypertable, avoiding duplicate TOAST compression and writes.
        with connection.cursor().copy('COPY mp_batch (season_id,player_id,goalie_id,line_player_ids,player_name,goalie_name,observed_at) FROM STDIN') as copy:
            for record in batch:
                copy.write_row((record[5],record[8],record[9],record[10],record[-2],record[-1],record[2]))
        connection.execute('''INSERT INTO analytics.seasons SELECT DISTINCT season_id FROM mp_batch ON CONFLICT DO NOTHING''')
        connection.execute('''INSERT INTO analytics.players (player_id,full_name,updated_at)
            SELECT DISTINCT ON (player_id) player_id,name,observed_at FROM (
                SELECT player_id,player_name AS name,observed_at FROM mp_batch
                UNION ALL SELECT goalie_id,goalie_name,observed_at FROM mp_batch
                UNION ALL SELECT unnest(line_player_ids),NULL,observed_at FROM mp_batch
            ) p WHERE player_id IS NOT NULL ORDER BY player_id,name NULLS LAST
            ON CONFLICT (player_id) DO UPDATE SET full_name=EXCLUDED.full_name
            WHERE players.full_name IS NULL AND EXCLUDED.full_name IS NOT NULL''')
        with connection.cursor().copy('COPY analytics.moneypuck_records (' + ','.join(COLUMNS) + ') FROM STDIN') as copy:
            for record in batch:
                copy.write_row(record[:-2])
        connection.execute('UPDATE analytics.moneypuck_files SET rows_processed=%s WHERE file_id=%s', (processed,file_id))


def import_file(connection, path, chunk_size=3000, game_type=2):
    path = Path(path)
    if chunk_size <= 0 or game_type not in (2,3):
        raise ValueError('Chunk size must be positive and game type must be 2 or 3')
    before = path.stat()
    checksum = fingerprint(path)
    file_id = uuid5(NAMESPACE_URL, 'MoneyPuck:' + checksum)
    locked = connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (str(file_id),)).fetchone()[0]
    if not locked:
        raise ValueError(f'Another importer is processing {path.name}')
    registered = False
    try:
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader = csv.reader(stream)
            headers = next(reader)
            kind = detect_kind(headers)
            existing = connection.execute('SELECT status,rows_processed,observed_at,game_type_default,paths FROM analytics.moneypuck_files WHERE file_id=%s', (file_id,)).fetchone()
            if existing and existing[3] != game_type:
                raise ValueError('Same file was registered with a different default game type')
            paths = existing[4] if existing else []
            if str(path.resolve()) not in paths:
                paths.append(str(path.resolve()))
            if existing:
                connection.execute('UPDATE analytics.moneypuck_files SET paths=%s WHERE file_id=%s',(Jsonb(paths),file_id))
                if existing[0] == 'complete':
                    LOG.info('%s: identical content already loaded (%s rows)',path.name,existing[1])
                    return existing[1]
            with connection.transaction():
                connection.execute('''INSERT INTO analytics.moneypuck_files
                    (file_id,sha256,paths,kind,game_type_default,status) VALUES (%s,%s,%s,%s,%s,'loading')
                    ON CONFLICT (file_id) DO UPDATE SET status='loading',error=NULL''', (file_id,checksum,Jsonb(paths),kind,game_type))
                processed, observed = connection.execute('SELECT rows_processed,observed_at FROM analytics.moneypuck_files WHERE file_id=%s',(file_id,)).fetchone()
            registered = True
            connection.execute('DROP TABLE IF EXISTS pg_temp.mp_batch')
            connection.execute('''CREATE TEMP TABLE mp_batch (season_id integer,player_id bigint,
                goalie_id bigint,line_player_ids bigint[],player_name text,goalie_name text,observed_at timestamptz)''')
            rows = csv_rows(reader,headers)
            # Resumption streams past committed rows without retaining them in memory.
            for _ in islice(rows,processed):
                pass
            match = re.search(r'(20\d{2})_(20\d{2})',path.stem)
            warned = False
            last_report = time.monotonic()
            last_report_rows = processed
            LOG.info('%s: starting/resuming at %s committed rows',path.name,processed)
            while True:
                batch = []
                for offset,row in enumerate(islice(rows,chunk_size),1):
                    if match and row['season'] != match[1] and not warned:
                        LOG.warning('%s: filename season disagrees with CSV; using CSV season %s',path.name,row['season'])
                        warned = True
                    try:
                        batch.append(transform(row,kind,file_id,processed+offset,observed,game_type))
                    except (ValueError,KeyError) as error:
                        raise ValueError(f'{path.name} data row {processed+offset}: {error}') from error
                if not batch:
                    break
                write_batch(connection,batch,file_id,processed+len(batch))
                processed += len(batch)
                if processed-last_report_rows >= chunk_size*10 or time.monotonic()-last_report >= 15:
                    LOG.info('%s: committed %s rows',path.name,processed)
                    last_report,last_report_rows = time.monotonic(),processed
            after = path.stat()
            if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                raise ValueError('File changed during import; completion withheld')
            if processed == 0:
                raise ValueError('CSV contains no data rows')
            connection.execute("UPDATE analytics.moneypuck_files SET status='complete',completed_at=now(),error=NULL WHERE file_id=%s",(file_id,))
            LOG.info('%s: complete, %s rows (%s)',path.name,processed,kind)
            return processed
    except Exception as error:
        if registered:
            connection.execute("UPDATE analytics.moneypuck_files SET status='failed',error=%s WHERE file_id=%s",
                               (type(error).__name__,file_id))
        raise
    finally:
        connection.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(str(file_id),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths',nargs='+',type=Path,help='CSV files or directories (CSV files directly inside)')
    parser.add_argument('--chunk-size',type=int,default=3000)
    parser.add_argument('--game-type',type=int,choices=[2,3],default=2,help='Default when CSV has no playoff flag')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error('--chunk-size must be positive')
    paths = sorted({p.resolve() for item in args.paths for p in (item.glob('*.csv') if item.is_dir() else [item])})
    if not paths:
        parser.error('No CSV files found')
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    failures = 0
    try:
        with connect(args.env_file) as connection:
            migrate(connection)
            for path in paths:
                try:
                    import_file(connection,path,args.chunk_size,args.game_type)
                except psycopg.Error as error:
                    failures += 1
                    LOG.error('%s: database load failed (%s); completed chunks retained, rerun to resume',path.name,type(error).__name__)
                except (ValueError,KeyError,OSError,StopIteration,csv.Error) as error:
                    failures += 1
                    LOG.error('%s: %s',path.name,error)
    except psycopg.Error:
        parser.exit(1,'Database connection/migration failed. Credentials are not logged.\n')
    except (ValueError,OSError) as error:
        parser.exit(1,f'Import failed: {error}\n')
    if failures:
        parser.exit(1,f'{failures} file(s) failed. Successful files remain loaded.\n')


if __name__ == '__main__':
    main()
