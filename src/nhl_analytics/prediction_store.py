"""Validated saved predictions indexed once into a disposable, read-only serving cache."""

import csv
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4

MODE='reconstructed_latest_corrected'
CACHE_VERSION=2
MODELS=('pace','prior_shrinkage')
TARGETS=('goals','assists','points','games')


class ArtifactError(ValueError):
    pass


def checksum(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def instant(value):
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ArtifactError('Prediction timestamps must include a timezone')
    return result.astimezone(timezone.utc).isoformat(timespec='microseconds')


def document(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as error:
        raise ArtifactError(f'Cannot read prediction document: {path.name}') from error


def verify(path,expected):
    if not path.is_file() or checksum(path)!=expected:
        raise ArtifactError(f'Missing or changed prediction artifact: {path.name}')


def discover_run(root=Path('data/evaluations/reconstructed')):
    for path in sorted(root.glob('*/manifest.json'),reverse=True):
        try:
            item=document(path)
        except ArtifactError:
            continue
        if item.get('status')=='complete' and item.get('data_mode')==MODE:
            return path.parent
    raise ArtifactError('No complete saved evaluation found. Run nhl-evaluate first or set NHL_PREDICTIONS_RUN.')


def csv_rows(path):
    with path.open(encoding='utf-8',newline='') as stream:
        yield from csv.DictReader(stream)


def require_mode(row):
    if row.get('data_mode')!=MODE:
        raise ArtifactError('Only reconstructed saved predictions are supported by this API version')


def number(value):
    result=float(value)
    if not math.isfinite(result) or result<0:
        raise ArtifactError('Prediction values must be finite and nonnegative')
    return result


def batched_insert(connection,statement,rows):
    batch=[]
    for row in rows:
        batch.append(row)
        if len(batch)==1000:
            connection.executemany(statement,batch);batch.clear()
    if batch:connection.executemany(statement,batch)


SCHEMA='''
PRAGMA foreign_keys=ON;
PRAGMA cache_size=-8192;
CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE inputs (
 season INTEGER NOT NULL,prediction_at TEXT NOT NULL,player INTEGER NOT NULL,
 team INTEGER NOT NULL,games INTEGER NOT NULL,goals INTEGER NOT NULL,assists INTEGER NOT NULL,points INTEGER NOT NULL,
 remaining INTEGER NOT NULL,prior_games INTEGER NOT NULL,available_at TEXT NOT NULL,
 PRIMARY KEY(season,prediction_at,player));
CREATE TABLE prediction_origins (
 season INTEGER NOT NULL,prediction_at TEXT NOT NULL,data_mode TEXT NOT NULL,
 PRIMARY KEY(season,prediction_at));
CREATE TABLE projections (
 season INTEGER NOT NULL,prediction_at TEXT NOT NULL,model TEXT NOT NULL,player INTEGER NOT NULL,
 games REAL NOT NULL,goals REAL NOT NULL,assists REAL NOT NULL,points REAL NOT NULL,
 PRIMARY KEY(season,prediction_at,model,player),
 FOREIGN KEY(season,prediction_at,player) REFERENCES inputs(season,prediction_at,player));
CREATE TABLE intervals (
 season INTEGER NOT NULL,prediction_at TEXT NOT NULL,model TEXT NOT NULL,player INTEGER NOT NULL,
 target TEXT NOT NULL,nominal INTEGER NOT NULL,observed INTEGER NOT NULL,prediction REAL NOT NULL,
 available INTEGER NOT NULL,lower INTEGER,upper INTEGER,calibration_players INTEGER NOT NULL,
 calibration_seasons TEXT NOT NULL,calibration_available_at TEXT,scope TEXT NOT NULL,radius REAL,
 PRIMARY KEY(season,prediction_at,model,player,target,nominal),
 FOREIGN KEY(season,prediction_at,model,player) REFERENCES projections(season,prediction_at,model,player));
CREATE TABLE fixtures (
 game INTEGER PRIMARY KEY,season INTEGER NOT NULL,game_date TEXT NOT NULL,start TEXT NOT NULL,
 home INTEGER NOT NULL,away INTEGER NOT NULL,home_abbrev TEXT NOT NULL,away_abbrev TEXT NOT NULL,is_final INTEGER NOT NULL);
CREATE TABLE games (
 game INTEGER PRIMARY KEY,prediction_at TEXT NOT NULL,home_rating REAL NOT NULL,away_rating REAL NOT NULL,
 home_probability REAL NOT NULL,comparator REAL NOT NULL,available_at TEXT,
 FOREIGN KEY(game) REFERENCES fixtures(game));
CREATE INDEX inputs_team ON inputs(season,prediction_at,team,player);
CREATE INDEX fixtures_season_start ON fixtures(season,start,game);
'''


def build_cache(path,identity,run,baseline,history,metadata,expected_projections):
    connection=sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        with connection:
            def inputs():
                for row in csv_rows(baseline/'player_inputs.csv'):
                    require_mode(row)
                    cutoff=instant(row['prediction_at']);available=instant(row['max_assumed_available_at'])
                    if available>=cutoff:raise ArtifactError('Player source availability reaches its prediction timestamp')
                    counts=[int(row[name]) for name in ('games_played','goals','assists','points','team_games_remaining','prior_games')]
                    if min(counts)<0 or counts[1]+counts[2]!=counts[3]:raise ArtifactError('Invalid observed player counts')
                    yield (int(row['season_id']),cutoff,int(row['player_id']),int(row['last_played_team_id']),*counts,available)
            batched_insert(connection,'INSERT INTO inputs VALUES (?,?,?,?,?,?,?,?,?,?,?)',inputs())
            def projections():
                for row in csv_rows(baseline/'player_predictions.csv'):
                    require_mode(row)
                    for model in MODELS:
                        values=[number(row['projected_games']),*(number(row[f'{model}_{t}']) for t in ('goals','assists','points'))]
                        if abs(values[1]+values[2]-values[3])>1e-8:raise ArtifactError('Projected goals plus assists differ from points')
                        yield (int(row['season_id']),instant(row['prediction_at']),model,int(row['player_id']),*values)
            batched_insert(connection,'INSERT INTO projections VALUES (?,?,?,?,?,?,?,?)',projections())
            connection.execute("INSERT INTO prediction_origins SELECT DISTINCT season,prediction_at,'reconstructed_latest_corrected' FROM inputs")
            invalid=connection.execute('''SELECT count(*) FROM projections p JOIN inputs i USING(season,prediction_at,player)
                WHERE p.games<i.games-1e-8 OR p.goals<i.goals-1e-8 OR p.assists<i.assists-1e-8 OR p.points<i.points-1e-8''').fetchone()[0]
            if invalid:raise ArtifactError('Projection is below already recorded statistics')
            if connection.execute('SELECT count(*) FROM inputs').fetchone()[0]!=expected_projections:
                raise ArtifactError('Player input row count differs from saved manifest')
            if connection.execute('SELECT count(*) FROM projections').fetchone()[0]!=2*expected_projections:
                raise ArtifactError('Projection row count differs from saved manifest')
            def intervals():
                for row in csv_rows(run/'player_intervals.csv'):
                    require_mode(row)
                    if row['model'] not in MODELS or row['target'] not in TARGETS or float(row['nominal']) not in (0.8,0.95):
                        raise ArtifactError('Unsupported interval model, target or nominal level')
                    if row['available'] not in ('True','False'):raise ArtifactError('Invalid interval availability flag')
                    available=row['available']=='True';observed=int(row['observed']);point=number(row['prediction'])
                    cutoff=instant(row['prediction_at'])
                    source=instant(row['max_calibration_label_available_at']) if row['max_calibration_label_available_at'] else None
                    lower=int(row['lower']) if row['lower'] else None;upper=int(row['upper']) if row['upper'] else None
                    count=int(row['calibration_players'])
                    if available and (source is None or source>=cutoff or lower is None or upper is None or lower<observed or upper<lower or count<1):
                        raise ArtifactError('Invalid interval bounds, support or calibration timestamp')
                    if not available and (lower is not None or upper is not None):raise ArtifactError('Unavailable interval has bounds')
                    yield (int(row['season_id']),cutoff,row['model'],int(row['player_id']),row['target'],round(float(row['nominal'])*100),
                        observed,point,int(available),lower,upper,count,row['calibration_seasons'],source,row['calibration_scope'],
                        number(row['radius']) if row['radius'] else None)
            batched_insert(connection,'INSERT INTO intervals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',intervals())
            invalid=connection.execute('''SELECT count(*) FROM intervals b JOIN projections p USING(season,prediction_at,model,player)
                JOIN inputs i USING(season,prediction_at,player) WHERE
                abs(b.prediction-CASE b.target WHEN 'games' THEN p.games WHEN 'goals' THEN p.goals WHEN 'assists' THEN p.assists ELSE p.points END)>1e-8
                OR b.observed<>CASE b.target WHEN 'games' THEN i.games WHEN 'goals' THEN i.goals WHEN 'assists' THEN i.assists ELSE i.points END''').fetchone()[0]
            if invalid:raise ArtifactError('Intervals do not match their saved projections')
            n_intervals=connection.execute('SELECT count(*) FROM intervals').fetchone()[0]
            if n_intervals!=expected_projections*16:raise ArtifactError('Incomplete interval coverage for saved projections')
            import pyarrow.parquet as pq  # Startup only; requests use SQLite alone.
            def fixtures():
                for batch in pq.ParquetFile(history/'games.parquet').iter_batches(batch_size=1000):
                    for row in batch.to_pylist():
                        yield (row['game_id'],row['season_id'],row['game_date'].isoformat(),instant(row['start_time_utc']),
                            row['home_team_id'],row['away_team_id'],row['home_team_abbrev'],row['away_team_abbrev'],int(row['is_final']))
            batched_insert(connection,'INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?)',fixtures())
            def games():
                for row in csv_rows(run/'selected_elo_predictions.csv'):
                    require_mode(row)
                    cutoff=instant(row['prediction_at']);source=instant(row['max_assumed_available_at']) if row['max_assumed_available_at'] else None
                    if source and source>=cutoff:raise ArtifactError('Elo source availability reaches its forecast timestamp')
                    p=number(row['home_win_probability']);comparator=number(row['historical_home_win_probability'])
                    if p>1 or comparator>1:raise ArtifactError('Invalid game probability')
                    fixture=connection.execute('SELECT season,home,away FROM fixtures WHERE game=?',(int(row['game_id']),)).fetchone()
                    if fixture!=(int(row['season_id']),int(row['home_team_id']),int(row['away_team_id'])):
                        raise ArtifactError('Elo forecast does not match its frozen fixture')
                    yield (int(row['game_id']),cutoff,number(row['home_rating']),number(row['away_rating']),p,comparator,source)
            batched_insert(connection,'INSERT INTO games VALUES (?,?,?,?,?,?,?)',games())
            if connection.execute('SELECT count(*) FROM games').fetchone()[0]!=connection.execute('SELECT count(*) FROM fixtures').fetchone()[0]:
                raise ArtifactError('Missing improved Elo forecasts for fixtures')
            snapshots=[dict(season_id=s,prediction_at=t,players=n) for s,t,n in connection.execute(
                'SELECT season,prediction_at,count(*) FROM inputs GROUP BY season,prediction_at ORDER BY season,prediction_at')]
            metadata=dict(metadata,snapshots=snapshots,seasons=sorted({r['season_id'] for r in snapshots}),
                row_counts=dict(player_snapshots=expected_projections,intervals=n_intervals,games=connection.execute('SELECT count(*) FROM games').fetchone()[0]))
            connection.execute('INSERT INTO metadata VALUES (?,?)',('identity',identity))
            connection.execute('INSERT INTO metadata VALUES (?,?)',('bundle',json.dumps(metadata)))
    except (KeyError,TypeError,ValueError,sqlite3.Error,OSError) as error:
        raise ArtifactError(f'Cannot index saved predictions: {error}') from error
    finally:
        connection.close()


class PredictionStore:
    def __init__(self,path,metadata):
        self.path=path
        self.metadata=metadata

    def connect(self):
        connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection

    @classmethod
    def load(cls,run,cache_dir):
        run=Path(run).resolve();cache_dir=Path(cache_dir).resolve()
        manifest=document(run/'manifest.json')
        if manifest.get('status')!='complete' or manifest.get('data_mode')!=MODE or manifest.get('version')!='1.0.0':
            raise ArtifactError('Expected a complete reconstructed evaluation version 1.0.0')
        baseline=Path(manifest['original_baseline']);b=document(baseline/'manifest.json')
        verify(baseline/'manifest.json',manifest['original_manifest_sha256'])
        if b.get('status')!='complete' or b.get('data_mode')!=MODE:raise ArtifactError('Invalid saved player baseline')
        history=Path(b['history']);h=document(history/'manifest.json')
        verify(history/'manifest.json',manifest['history_manifest_sha256'])
        if b['history_manifest_sha256']!=manifest['history_manifest_sha256'] or h.get('data_mode')!=MODE or h.get('status')!='complete':
            raise ArtifactError('Evaluation and baseline history do not match')
        for name in ('player_intervals.csv','selected_elo_predictions.csv','selected_elo_model.json','elo_selection.json'):
            verify(run/name,manifest['files'][name]['sha256'])
        for name in ('player_inputs','player_predictions'):
            verify(baseline/f'{name}.csv',b['artifacts'][name]['sha256'])
        verify(history/'games.parquet',h['artifacts']['games']['sha256'])
        model=document(run/'selected_elo_model.json')
        if model.get('data_mode')!=MODE or instant(model['as_of'])!=instant(manifest['as_of']) or instant(b['as_of'])!=instant(manifest['as_of']):
            raise ArtifactError('Saved model and prediction clocks do not match')
        verify(run/'elo_selection.json',model['selection_sha256'])
        selection=document(run/'elo_selection.json')
        if model['parameters']!=selection['parameters'] or not selection['excluded_holdout_from_selection']:
            raise ArtifactError('Improved Elo model does not match its development selection')
        identity=hashlib.sha256(f'{CACHE_VERSION}:{checksum(run/"manifest.json")}'.encode()).hexdigest()
        cache_dir.mkdir(parents=True,exist_ok=True)
        path=cache_dir/f'{identity}.sqlite3'
        metadata=dict(bundle_id=run.name,bundle_sha256=checksum(run/'manifest.json'),baseline_id=baseline.name,
            data_mode=MODE,as_of=instant(manifest['as_of']),availability_policy=manifest['reconstructed_policy'],
            evaluation_version=manifest['version'],elo_model='development_selected',elo_parameters=model['parameters'],
            franchise_mapping=model.get('franchise_mapping',{}),
            limitations=['Historical availability is reconstructed, not verified point-in-time knowledge.',
                'Intervals are marginal prediction bands, not confidence intervals or a joint region; they may be conservative.',
                'Upcoming game probabilities hold saved current ratings fixed; fixture status is from the saved snapshot.'])
        if not path.exists():
            temporary=cache_dir/f'{identity}.{uuid4().hex}.tmp'
            try:
                build_cache(temporary,identity,run,baseline,history,metadata,b['artifacts']['player_predictions']['rows'])
                # Do not publish a cache if a source changed during indexing.
                verify(run/'manifest.json',metadata['bundle_sha256'])
                for name in ('player_intervals.csv','selected_elo_predictions.csv','selected_elo_model.json','elo_selection.json'):
                    verify(run/name,manifest['files'][name]['sha256'])
                for name in ('player_inputs','player_predictions'):
                    verify(baseline/f'{name}.csv',b['artifacts'][name]['sha256'])
                verify(history/'games.parquet',h['artifacts']['games']['sha256'])
                if path.exists():temporary.unlink()
                else:
                    try:temporary.replace(path)
                    except PermissionError:
                        if not path.exists():raise
                        temporary.unlink()
            finally:
                temporary.unlink(missing_ok=True)
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as connection:
            if connection.execute('SELECT value FROM metadata WHERE key=?',('identity',)).fetchone()!=(identity,):
                raise ArtifactError('Prediction cache identity mismatch; remove this disposable cache and restart')
            metadata=json.loads(connection.execute('SELECT value FROM metadata WHERE key=?',('bundle',)).fetchone()[0])
        return cls(path,metadata)

    def snapshot(self,season,cutoff=None):
        season=season if season is not None else max(self.metadata['seasons'])
        snapshots=[r['prediction_at'] for r in self.metadata['snapshots'] if r['season_id']==season]
        if cutoff is not None:
            cutoff=instant(cutoff)
            snapshots=[t for t in snapshots if t==cutoff]
        if not snapshots:raise LookupError('No saved player snapshot for this season/prediction_at')
        return season,max(snapshots)

    def page(self,table,where,parameters,order,limit,offset):
        # Table/columns/order are fixed by the API, never taken from user text.
        with closing(self.connect()) as connection:
            total=connection.execute(f'SELECT count(*) FROM {table} WHERE {where}',parameters).fetchone()[0]
            rows=connection.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',(*parameters,limit,offset)).fetchall()
        return total,[dict(row) for row in rows]
