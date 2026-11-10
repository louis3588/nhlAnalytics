"""Read immutable observations as known strictly before a prediction instant."""

from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from nhl_analytics.database import timestamp

LEAGUE_TZ=ZoneInfo('America/New_York')


def past_final(game,cutoff):
    if not game.get('is_final') or game['available_at']>=cutoff:
        return False
    start=game.get('start_time_utc')
    return timestamp(start)<cutoff if start else date.fromisoformat(game['game_date'])<cutoff.astimezone(LEAGUE_TZ).date()


def read_games(connection,cutoff,seasons,phase):
    query='''SELECT DISTINCT ON (game_id) game_id,observed_at,payload
        FROM analytics.game_schedule_observations
        WHERE observed_at<%s AND (payload->'normalized'->>'season_id')::int=ANY(%s)
        AND (payload->'normalized'->>'game_type_id')::int=%s
        ORDER BY game_id,observed_at DESC'''
    games={}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query,(cutoff,seasons,phase))
        for row in cursor:
            game={**row['payload']['normalized'],'available_at':row['observed_at']}
            games[row['game_id']]=game
    return games


def read_player_games(connection,cutoff,seasons,phase,games):
    # Select the latest whole response, not the latest row per game: the latter
    # would resurrect a game retracted by a newer complete response.
    query='''WITH sources AS (
        SELECT DISTINCT ON (player_id,season_id,game_type_id,kind) *
        FROM analytics.game_log_sources WHERE observed_at<%s AND season_id=ANY(%s) AND game_type_id=%s
        ORDER BY player_id,season_id,game_type_id,kind,observed_at DESC,source_id DESC
    ) SELECT s.player_id,s.season_id,s.game_type_id,s.kind,s.source_id,s.observed_at AS available_at,
        o.game_id,o.observed_at AS row_available_at,o.payload
        FROM sources s JOIN analytics.player_game_observations o USING(source_id)
        WHERE o.observed_at<%s AND o.player_id=s.player_id AND o.kind=s.kind'''
    records=[]
    excluded=0
    source_ids=set()
    with connection.cursor(name='asof_player_games',row_factory=dict_row) as cursor:
        cursor.itersize=2000
        cursor.execute(query,(cutoff,seasons,phase,cutoff))
        for row in cursor:
            game=games.get(row['game_id'])
            if not game or not past_final(game,cutoff):
                excluded+=1
                continue
            payload=row['payload']
            # Join context by the source home/road flag, never by current roster.
            home=payload['homeRoadFlag']=='H'
            expected_team=game['home_team_abbrev'] if home else game['away_team_abbrev']
            if payload['teamAbbrev']!=expected_team:
                raise ValueError(f"Player/game team mismatch for {row['player_id']}/{row['game_id']}")
            records.append(dict(player_id=row['player_id'],season_id=row['season_id'],kind=row['kind'],
                game_id=row['game_id'],game_date=date.fromisoformat(game['game_date']),
                start_at=timestamp(game['start_time_utc']) if game.get('start_time_utc') else None,
                team_id=game['home_team_id'] if home else game['away_team_id'],
                available_at=max(row['available_at'],row['row_available_at'],game['available_at']),payload=payload))
            source_ids.add(str(row['source_id']))
    return records,dict(excluded_without_known_final_game=excluded,source_ids=sorted(source_ids))


def read_context(connection,cutoff,seasons,phase):
    standings={}
    edge={}
    with connection.cursor(row_factory=dict_row) as cursor:
        if phase==2:
            cursor.execute('''SELECT DISTINCT ON (team_id,season_id) team_id,season_id,standings_date,observed_at,payload
                FROM analytics.team_standings_observations
                WHERE observed_at<%s AND season_id=ANY(%s) AND standings_date<=%s
                ORDER BY team_id,season_id,standings_date DESC,observed_at DESC''',
                (cutoff,seasons,cutoff.astimezone(LEAGUE_TZ).date()))
            for row in cursor:
                standings[(row['team_id'],row['season_id'])]=dict(row)
        cursor.execute('''SELECT DISTINCT ON (team_id,season_id,game_type_id) team_id,season_id,observed_at,payload
            FROM analytics.team_edge_observations WHERE observed_at<%s AND season_id=ANY(%s) AND game_type_id=%s
            ORDER BY team_id,season_id,game_type_id,observed_at DESC''',(cutoff,seasons,phase))
        for row in cursor:
            edge[(row['team_id'],row['season_id'])]=dict(row)
    return standings,edge


def read_shots(connection,cutoff,seasons,phase,game_ids):
    if not game_ids:
        return {},{},dict(files=[],canonical_events=0)
    # Complete-file availability is later than file registration. Do not make a
    # half-imported archive appear available at its earlier observed_at timestamp.
    query='''WITH events AS MATERIALIZED (
        SELECT DISTINCT ON (r.record_key) r.game_id,r.player_id,r.goalie_id,r.xgoals_for,r.goals,
            r.payload->>'event' AS event,
            (r.goalie_id IS NULL AND coalesce(r.payload->>'shotOnEmptyNet','0') NOT IN ('1','1.0')) AS unknown_goalie,
            f.completed_at AS available_at,r.file_id
        FROM analytics.moneypuck_records r JOIN analytics.moneypuck_files f USING(file_id)
        WHERE r.kind='shot' AND r.season_id=ANY(%s) AND r.game_type_id=%s AND r.game_id=ANY(%s)
            AND f.status='complete' AND f.completed_at<%s AND r.observed_at<%s
        ORDER BY r.record_key,r.observed_at DESC,r.file_id,r.row_number ASC
    ), aggregates AS (
        SELECT 'player' AS kind,game_id,player_id AS entity_id,sum(xgoals_for::numeric) AS xg,
            count(*) FILTER(WHERE event IN ('SHOT','GOAL')) AS sog,
            count(*) FILTER(WHERE goals=1) AS goals,
            bool_and(xgoals_for IS NOT NULL AND xgoals_for BETWEEN 0 AND 1) AS valid_xg,
            max(available_at) AS available_at,count(*) AS events,
            false AS unknown_player,false AS unknown_goalie
        FROM events WHERE player_id IS NOT NULL GROUP BY game_id,player_id
        UNION ALL
        SELECT 'goalie',game_id,goalie_id,sum(xgoals_for::numeric),
            count(*) FILTER(WHERE event IN ('SHOT','GOAL')),count(*) FILTER(WHERE goals=1),
            bool_and(xgoals_for IS NOT NULL AND xgoals_for BETWEEN 0 AND 1),max(available_at),count(*),false,false
        FROM events WHERE goalie_id IS NOT NULL GROUP BY game_id,goalie_id
        UNION ALL
        SELECT 'game',game_id,NULL,NULL,0,0,true,max(available_at),count(*),
            bool_or(player_id IS NULL),bool_or(unknown_goalie)
        FROM events GROUP BY game_id
    ) SELECT * FROM aggregates'''
    actors={}
    coverage={}
    canonical_events=0
    with connection.cursor(name='asof_shot_aggregates',row_factory=dict_row) as cursor:
        cursor.itersize=2000
        cursor.execute(query,(seasons,phase,list(game_ids),cutoff,cutoff))
        for row in cursor:
            if row['kind']=='game':
                coverage[row['game_id']]=dict(row)
                canonical_events+=row['events']
            else:
                actors[(row['kind'],row['entity_id'],row['game_id'])]=dict(row)
    files=connection.execute('''SELECT file_id,sha256,observed_at,completed_at FROM analytics.moneypuck_files
        WHERE kind='shot' AND status='complete' AND completed_at<%s''',(cutoff,)).fetchall()
    return actors,coverage,dict(canonical_events=canonical_events,files=[dict(file_id=str(f[0]),sha256=f[1],
        registered_at=f[2].isoformat(),available_at=f[3].isoformat()) for f in files])


def read_team_advanced(connection,cutoff,seasons,phase,game_ids):
    if not game_ids:
        return {},[]
    query='''SELECT DISTINCT ON (r.game_id,r.payload->>'home_or_away')
        r.game_id,r.game_date,r.payload->>'home_or_away' AS side,r.goals AS gf,
        r.payload->>'goalsAgainst' AS ga,r.xgoals_for AS xgf,r.xgoals_against AS xga,
        f.completed_at AS available_at,r.file_id,f.sha256
        FROM analytics.moneypuck_records r JOIN analytics.moneypuck_files f USING(file_id)
        WHERE r.kind='team_game' AND r.situation='all' AND r.season_id=ANY(%s)
        AND r.game_type_id=%s AND r.game_id=ANY(%s) AND f.status='complete'
        AND f.completed_at<%s AND r.observed_at<%s
        ORDER BY r.game_id,r.payload->>'home_or_away',r.observed_at DESC,r.file_id,r.row_number ASC'''
    records={}
    files={}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query,(seasons,phase,list(game_ids),cutoff,cutoff))
        for row in cursor:
            if row['side'] not in ('HOME','AWAY'):
                continue
            records[(row['game_id'],row['side']=='HOME')]=dict(row)
            files[str(row['file_id'])]=dict(file_id=str(row['file_id']),sha256=row['sha256'],available_at=row['available_at'].isoformat())
    return records,[files[k] for k in sorted(files)]
