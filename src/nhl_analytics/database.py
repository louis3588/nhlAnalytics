"""Versioned migrations and atomic loading of completed NHL snapshots."""

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

LOG = logging.getLogger(__name__)


def connect(env_file=Path('.env')):
    load_dotenv(env_file, override=False)
    password = os.environ.get('POSTGRES_PASSWORD')
    if not password:
        raise ValueError('Set POSTGRES_PASSWORD in .env before connecting')
    # Keyword parameters avoid URL escaping issues with password punctuation.
    return psycopg.connect(
        host=os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        port=int(os.environ.get('POSTGRES_PORT', '6543')),
        dbname=os.environ.get('POSTGRES_DB', 'nhl_analytics'),
        user=os.environ.get('POSTGRES_USER', 'postgres'), password=password,
        connect_timeout=10, autocommit=True,
    )


def migrate(connection):
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(768432101)")
        connection.execute('CREATE SCHEMA IF NOT EXISTS analytics')
        connection.execute('''CREATE TABLE IF NOT EXISTS analytics.schema_migrations (
            version text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())''')
        for path in sorted(files('nhl_analytics').joinpath('migrations').iterdir(), key=lambda p: p.name):
            if not path.name.endswith('.sql'):
                continue
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            existing = connection.execute(
                'SELECT checksum FROM analytics.schema_migrations WHERE version = %s', (path.name,)
            ).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise ValueError(f'Applied migration changed: {path.name}; add a new migration instead')
                continue
            connection.execute(sql, prepare=False)
            connection.execute('INSERT INTO analytics.schema_migrations(version, checksum) VALUES (%s,%s)',
                               (path.name, checksum))
            LOG.info('Applied migration %s', path.name)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Snapshot timestamps must include a timezone')
    return parsed.astimezone(timezone.utc)


def read_snapshot(path):
    documents = {}
    digest = hashlib.sha256()
    for name in ('manifest.json', 'players.json', 'nhl-rosters.json', 'player_season_stats.json'):
        data = (path / name).read_bytes()
        digest.update(name.encode() + b'\0' + data + b'\0')
        documents[name] = json.loads(data)
    manifest = documents['manifest.json']
    if manifest.get('status') != 'complete' or manifest.get('schemaVersion') != 1 or manifest.get('source') != 'NHL':
        raise ValueError('Expected a completed version-1 NHL snapshot')
    for document in documents.values():
        for key in ('seasonId', 'gameTypeId', 'collectedAt', 'source', 'schemaVersion', 'rosterObservedAt'):
            if document.get(key) != manifest.get(key):
                raise ValueError(f'Snapshot metadata mismatch: {key}')
    timestamp(manifest['collectedAt'])
    timestamp(manifest['rosterObservedAt'])
    season = manifest['seasonId']
    if type(season) is not int or season % 10000 != season // 10000 + 1 or manifest['gameTypeId'] not in (2, 3):
        raise ValueError('Invalid season or game type')
    players = documents['players.json']['players']
    teams = documents['nhl-rosters.json']['teams']
    stats = documents['player_season_stats.json']
    for key, rows in [('playerCount', players), ('teamCount', teams),
                      ('skaterCount', stats['skaters']), ('goalieCount', stats['goalies'])]:
        if manifest[key] != len(rows):
            raise ValueError(f'Snapshot count mismatch: {key}')
    def unique_ids(rows, field):
        ids = [row[field] for row in rows]
        if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
            raise ValueError(f'Invalid or duplicate {field}')
        return set(ids)
    player_ids = unique_ids(players, 'id')
    unique_ids(teams, 'id')
    for team in teams:
        if not unique_ids(team['players'], 'id') <= player_ids:
            raise ValueError('Roster player missing from players.json')
    for kind in ('skaters', 'goalies'):
        if not unique_ids(stats[kind], 'playerId') <= player_ids:
            raise ValueError('Statistics player missing from players.json')
        if any(row['seasonId'] != season or row['gameTypeId'] != manifest['gameTypeId'] for row in stats[kind]):
            raise ValueError('Statistics season/game type mismatch')
    return manifest, players, teams, stats, digest.hexdigest()


def load_snapshot(connection, path):
    path = Path(path)
    manifest, players, teams, stats, checksum = read_snapshot(path)
    season, game_type = manifest['seasonId'], manifest['gameTypeId']
    collected, observed = timestamp(manifest['collectedAt']), timestamp(manifest['rosterObservedAt'])
    run_id = uuid5(NAMESPACE_URL, f"NHL:{season}:{game_type}:{collected.isoformat()}")
    with connection.transaction():
        # Serializes import writes; prevents duplicate races and preserves atomic batches.
        connection.execute('SELECT pg_advisory_xact_lock(768432101)')
        existing = connection.execute('SELECT content_sha256 FROM analytics.import_runs WHERE run_id=%s',
                                      (run_id,)).fetchone()
        if existing:
            if existing[0] != checksum:
                raise ValueError('Snapshot contents changed for an already loaded observation')
            return False
        connection.execute('INSERT INTO analytics.seasons VALUES (%s) ON CONFLICT DO NOTHING', (season,))
        connection.execute('''INSERT INTO analytics.import_runs
            (run_id,source,season_id,game_type_id,collected_at,roster_observed_at,content_sha256,snapshot_path,manifest)
            VALUES (%s,'NHL',%s,%s,%s,%s,%s,%s,%s)''',
            (run_id, season, game_type, collected, observed, checksum, str(path.resolve()), Jsonb(manifest)))
        with connection.cursor() as cursor:
            cursor.executemany('''INSERT INTO analytics.players
                (player_id,first_name,last_name,full_name,birth_date,birth_country,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (player_id) DO UPDATE SET
                first_name=COALESCE(EXCLUDED.first_name,players.first_name),
                last_name=COALESCE(EXCLUDED.last_name,players.last_name),
                full_name=COALESCE(EXCLUDED.full_name,players.full_name),
                birth_date=COALESCE(EXCLUDED.birth_date,players.birth_date),
                birth_country=COALESCE(EXCLUDED.birth_country,players.birth_country),
                updated_at=EXCLUDED.updated_at WHERE EXCLUDED.updated_at >= players.updated_at''',
                [(p['id'], p.get('firstName'), p.get('lastName'),
                  p.get('fullName') or ' '.join(filter(None, [p.get('firstName'), p.get('lastName')])) or None,
                  p.get('birthDate'), p.get('birthCountry'), observed) for p in players])
            cursor.executemany('''INSERT INTO analytics.teams
                (team_id,abbreviation,name,conference,division,updated_at) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (team_id) DO UPDATE SET abbreviation=EXCLUDED.abbreviation,name=EXCLUDED.name,
                conference=EXCLUDED.conference,division=EXCLUDED.division,updated_at=EXCLUDED.updated_at
                WHERE EXCLUDED.updated_at >= teams.updated_at''',
                [(t['id'], t['abbreviation'], t['name'], t.get('conference'), t.get('division'), observed) for t in teams])
            cursor.executemany('''INSERT INTO analytics.roster_observations
                (run_id,observed_at,season_id,player_id,team_id,jersey_number,position,position_group,
                shoots_catches,height_cm,weight_kg,headshot,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                [(run_id, observed, season, p['id'], t['id'], p.get('number'), p.get('position'),
                  p.get('positionGroup'), p.get('shootsCatches'), p.get('heightCm'), p.get('weightKg'),
                  p.get('headshot'), Jsonb(p)) for t in teams for p in t['players']])
            cursor.executemany('''INSERT INTO analytics.skater_season_stats
                (run_id,collected_at,player_id,season_id,game_type_id,team_abbrevs,games_played,goals,
                 assists,points,shots,shooting_pct,time_on_ice_per_game,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                [(run_id, collected, s['playerId'], season, game_type, s.get('teamAbbrevs'),
                  s.get('gamesPlayed'), s.get('goals'), s.get('assists'), s.get('points'), s.get('shots'),
                  s.get('shootingPct'), s.get('timeOnIcePerGame'), Jsonb(s)) for s in stats['skaters']])
            cursor.executemany('''INSERT INTO analytics.goalie_season_stats
                (run_id,collected_at,player_id,season_id,game_type_id,team_abbrevs,games_played,games_started,
                 wins,losses,saves,shots_against,goals_against,save_pct,goals_against_average,shutouts,time_on_ice,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                [(run_id, collected, s['playerId'], season, game_type, s.get('teamAbbrevs'),
                  s.get('gamesPlayed'), s.get('gamesStarted'), s.get('wins'), s.get('losses'), s.get('saves'),
                  s.get('shotsAgainst'), s.get('goalsAgainst'), s.get('savePct'), s.get('goalsAgainstAverage'),
                  s.get('shutouts'), s.get('timeOnIce'), Jsonb(s)) for s in stats['goalies']])
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('migrate', help='Apply pending versioned migrations')
    loader = commands.add_parser('load', help='Load a completed snapshot without fetching again')
    loader.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        with connect(args.env_file) as connection:
            migrate(connection)
            if args.command == 'load':
                loaded = load_snapshot(connection, args.snapshot)
                LOG.info('Snapshot %s', 'loaded successfully' if loaded else 'already loaded; skipped')
    except psycopg.Error:
        parser.exit(1, 'Database operation failed; check connection settings, migrations and snapshot values. Credentials are not logged.\n')
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'Database import failed: {error}\n')


if __name__ == '__main__':
    main()
