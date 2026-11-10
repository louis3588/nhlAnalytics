"""Explicitly reconstructed event history. Never used by the strict exporter."""

import argparse
import hashlib
import json
from datetime import datetime,time,timedelta,timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.rows import dict_row

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.database import connect
from nhl_analytics.features import write_document
from nhl_analytics.ingest import season_argument

MODE='reconstructed_latest_corrected'
POLICY='Assume game statistics available at 12:00 America/New_York on the day after game_date; not verified publication times.'
DEFAULT_SEASONS=[20212022,20222023,20232024,20242025,20252026,20262027]


def assumed_available(day):
    return datetime.combine(day+timedelta(days=1),time(12),LEAGUE_TZ).astimezone(timezone.utc)


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def export_history(connection,seasons,output):
    output.mkdir(parents=True,exist_ok=True)
    if (output/'manifest.json').exists():
        raise ValueError('Use a new reconstruction output directory')
    artifacts={}
    # Named cursors bound database reads. Parquet batches avoid materializing
    # the full player history during the export.
    queries={
        'games':'''SELECT game_id,season_id,game_date,start_time_utc,home_team_id,away_team_id,
            home_team_abbrev,away_team_abbrev,home_score,away_score,is_final,result_type,
            winner_team_id,schedule_checked_at AS retrieved_at,schedule_content_sha256 AS source_sha256
            FROM analytics.games WHERE season_id=ANY(%s) AND game_type_id=2 ORDER BY season_id,game_date,game_id''',
        'skaters':'''SELECT p.player_id,p.game_id,p.season_id,g.game_date,p.home_road,p.team_abbrev,
            p.goals,p.assists,p.points,p.shots,p.toi_seconds,p.source_id::text,
            s.observed_at AS retrieved_at,s.content_sha256 AS source_sha256
            FROM analytics.skater_game_stats p JOIN analytics.games g USING(game_id)
            JOIN analytics.game_log_sources s USING(source_id)
            WHERE p.season_id=ANY(%s) AND p.game_type_id=2 ORDER BY p.season_id,g.game_date,p.game_id,p.player_id''',
        'goalies':'''SELECT p.player_id,p.game_id,p.season_id,g.game_date,p.home_road,p.team_abbrev,
            p.games_started,p.shots_against,p.goals_against,p.saves,p.toi_seconds,p.source_id::text,
            s.observed_at AS retrieved_at,s.content_sha256 AS source_sha256
            FROM analytics.goalie_game_stats p JOIN analytics.games g USING(game_id)
            JOIN analytics.game_log_sources s USING(source_id)
            WHERE p.season_id=ANY(%s) AND p.game_type_id=2 ORDER BY p.season_id,g.game_date,p.game_id,p.player_id''',
    }
    for name,query in queries.items():
        path=output/f'{name}.parquet'
        temporary=output/f'{name}.parquet.tmp'
        count=0
        writer=None
        with connection.cursor(name=f'reconstruct_{name}',row_factory=dict_row) as cursor:
            cursor.execute(query,(seasons,))
            try:
                while rows:=cursor.fetchmany(4000):
                    for row in rows:
                        row['assumed_available_at']=assumed_available(row['game_date'])
                    # Explicit strings for identifiers/nullable provenance prevent
                    # an all-null first batch from fixing the wrong Arrow type.
                    table=pa.Table.from_pylist(rows)
                    if writer is None:
                        schema=table.schema.with_metadata({b'data_mode':MODE.encode(),b'availability_policy':POLICY.encode()})
                        writer=pq.ParquetWriter(temporary,schema,compression='zstd')
                    writer.write_table(table.cast(writer.schema))
                    count+=len(rows)
            finally:
                if writer:
                    writer.close()
        if not count:
            raise ValueError(f'No {name} rows for requested seasons')
        temporary.replace(path)
        artifacts[name]=dict(rows=count,sha256=file_hash(path))
    manifest=dict(status='complete',version=1,data_mode=MODE,generated_at=datetime.now(timezone.utc),
        seasons=seasons,game_type_id=2,availability_policy=POLICY,artifacts=artifacts,
        limitations=['Latest corrected records and latest rescheduled fixtures are used retrospectively.',
            'This is not a strict point-in-time backtest. Original publication times and revisions are unknown.',
            'No historical injuries, announced rosters, EDGE or season-final aggregates enter features.',
            'Reconstruction contains outcomes; baseline builders separate model inputs from future labels.'])
    write_document(output/'manifest.json',manifest)
    return manifest


def load_history(path,columns=None):
    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('status')!='complete' or manifest.get('data_mode')!=MODE:
        raise ValueError('Expected a complete reconstructed history, not strict feature exports')
    outputs={}
    for name,artifact in manifest['artifacts'].items():
        file=path/f'{name}.parquet'
        if file_hash(file)!=artifact['sha256']:
            raise ValueError(f'Changed reconstruction file: {name}')
        parquet=pq.ParquetFile(file)
        names=columns.get(name) if columns else None
        names=names or parquet.schema_arrow.names
        outputs[name]=[]
        for batch in parquet.iter_batches(batch_size=4000,columns=names):
            values=batch.to_pydict()
            # Share column-name objects across rows/batches, and let callers
            # omit provenance columns they do not use for model arithmetic.
            outputs[name].extend(dict(zip(names,row)) for row in zip(*(values[key] for key in names)))
        if len(outputs[name])!=artifact['rows']:
            raise ValueError(f'Reconstruction row count mismatch: {name}')
    return outputs,manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seasons',type=season_argument,nargs='+',default=DEFAULT_SEASONS)
    parser.add_argument('--output',type=Path,default=Path('data/reconstructed'))
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    args=parser.parse_args()
    output=args.output/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    with connect(args.env_file) as connection,connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        manifest=export_history(connection,sorted(set(args.seasons)),output)
    print(json.dumps(dict(output=str(output),rows={k:v['rows'] for k,v in manifest['artifacts'].items()}),indent=2))


if __name__=='__main__':
    main()
