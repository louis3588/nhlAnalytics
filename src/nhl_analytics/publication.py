"""Build small, allowlisted, content-addressed public snapshots. No HTTP server."""

import argparse
import gzip
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from nhl_analytics.api import PROJECTIONS, INTERVALS, GAMES, player_item, interval_item, game_item
from nhl_analytics.api import PlayerProjection, PlayerInterval, GamePrediction
from nhl_analytics.daily_predictions import load_daily
from nhl_analytics.prediction_store import document, checksum
from nhl_analytics.prospective_store import ProspectiveRepository, jsonable
from nhl_analytics.prospective_reports import game_item as prospective_item, performance
from nhl_analytics.database import timestamp
from nhl_analytics.features import atomic_replace

SCHEMA=1
MAX_FILE=8*1024*1024
MAX_TOTAL=150*1024*1024
ROOT=Path('dashboard/public/publication')
META_FIELDS=('bundle_id','bundle_sha256','baseline_id','evaluation_version','data_mode','as_of','availability_policy',
    'limitations','elo_model','elo_parameters','franchise_mapping')
PRIVATE=re.compile(r'(?:\b[A-Za-z]:[\\/]|postgres(?:ql)?://|/Users/|/home/|Bearer\s|-----BEGIN)',re.I)


def encoded(value):
    return json.dumps(jsonable(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def public_values(value):
    """Fail closed on accidentally exported filesystem paths or secret fields."""
    if isinstance(value,dict):
        for key,item in value.items():
            if re.search(r'password|secret|token|raw_path|connection_string|training_artifact',key,re.I):
                raise ValueError(f'Private field in publication: {key}')
            public_values(item)
    elif isinstance(value,list):
        for item in value:public_values(item)
    elif isinstance(value,str) and PRIVATE.search(value):raise ValueError('Private path or credential in publication')
    elif isinstance(value,float) and not math.isfinite(value):raise ValueError('Nonfinite publication value')


def database_public(connection,season):
    """Read one consistent database snapshot, without inference or ledger writes."""
    repo=ProspectiveRepository(connection)
    campaigns=repo.rows('SELECT * FROM analytics.prospective_campaigns WHERE campaign=%s',('elo_vs_logistic_v1',))
    prospective=dict(items=[],revisions=[],report=None)
    versions={}
    if campaigns:
        campaign=campaigns[0];now=repo.clock();rows=repo.snapshot(campaign)
        versions=dict(prospective_frozen_elo=campaign['elo_version'],prospective_logistic=campaign['logistic_version'])
        report=performance(rows,campaign,now)
        stable=max([campaign['registered_at']]+[r['generated_at'] for r in rows if r['generated_at']]+[r['result_checked_at'] for r in rows if r['result_checked_at']])
        report['generated_at']=stable
        prospective=dict(items=[prospective_item(r,campaign,now) for r in rows],revisions=repo.revisions(campaign),report=report)
    standings=repo.rows('''SELECT team_id,season_id,standings_date,games_played,wins,losses,ot_losses,points,
        points_pct,goals_for,goals_against,goal_differential,conference,division,league_rank,observed_at
        FROM analytics.latest_team_standings WHERE season_id=%s ORDER BY league_rank NULLS LAST,team_id''',(season,))
    results=repo.rows('''SELECT game_id,home_score,away_score,winner_team_id,is_final,schedule_checked_at
        FROM analytics.games WHERE season_id=%s AND game_type_id=2 ORDER BY game_id''',(season,))
    return jsonable(dict(prospective=prospective,standings=standings,results=results,model_versions=versions))


def pages(store,table,where,params,order):
    offset=0
    while True:
        total,rows=store.page(table,where,params,order,1000,offset)
        yield from rows
        offset+=len(rows)
        if offset>=total:break
        if not rows:raise ValueError('Incomplete serving cache page')


def write_release(store,catalog,extras,output=ROOT,history_limit=32):
    if not 1<=history_limit<=64:raise ValueError('history_limit must be 1..64')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    season,cutoff=store.snapshot(None)
    dates=sorted({s['prediction_at'] for s in store.metadata['snapshots'] if s['season_id']==season})[-history_limit:]
    metadata={key:store.metadata[key] for key in META_FIELDS if key in store.metadata}
    metadata.update(seasons=[season],snapshots=sorted([s for s in store.metadata['snapshots'] if s['season_id']==season and s['prediction_at'] in dates],key=lambda s:s['prediction_at']),
        age_hours=0,stale=False,stale_after_hours=24,publication_history_limit=history_limit,
        delivery='static_snapshot',live_available=False)
    metadata['model_versions']=dict(player_projection_engine=metadata['evaluation_version'],
        archived_prediction_bundle=metadata['bundle_sha256'],**extras.get('model_versions',{}))
    players=[player_item(r) for r in pages(store,PROJECTIONS,'season=? AND prediction_at=?',[season,cutoff],'player,model')]
    bands=[dict(interval_item(r),last_played_team_id=r['team']) for r in pages(store,INTERVALS,'season=? AND prediction_at=?',[season,cutoff],'player,model,target,nominal')]
    fixtures=[game_item(r,metadata['as_of']) for r in pages(store,GAMES,'season=?',[season],'start,game')]
    results={r['game_id']:r for r in extras.get('results',[])}
    for fixture in fixtures:
        result=results.get(fixture['game_id'])
        if result:
            fixture['result']=result
            if result['is_final']:fixture['snapshot_status']='final'
    player_ids=sorted({r['player_id'] for r in players})
    teams=catalog['seasons'].get(str(season),catalog['seasons'].get(season,[]))
    identities=dict(players={str(pid):{k:v for k,v in catalog['players'].get(str(pid),{}).items() if k in ('name','kind','headshot')} for pid in player_ids},
        teams={str(tid):{k:v for k,v in catalog['teams'].get(str(tid),{}).items() if k in ('id','name','abbreviation','logo','seasonLogos')} for tid in teams},seasons={str(season):teams})
    if not players or not fixtures or not teams:raise ValueError('Incomplete public season dataset')
    for p in players:
        PlayerProjection.model_validate(p)
        if not identities['players'][str(p['player_id'])].get('name') or str(p['last_played_team_id']) not in identities['teams']:
            raise ValueError('Player identity or team missing')
        if abs(p['projected']['goals']+p['projected']['assists']-p['projected']['points'])>1e-6:
            raise ValueError('Inconsistent projected scoring totals')
        if any(p['projected'][key]<value for key,value in p['observed'].items()):raise ValueError('Projection below recorded total')
        if p['max_assumed_available_at'] and timestamp(p['max_assumed_available_at'])>=timestamp(p['prediction_at']):
            raise ValueError('Player source is not before prediction')
    for f in fixtures:
        GamePrediction.model_validate(f)
        if not 0<=f['home_win_probability']<=1 or abs(f['home_win_probability']+f['away_win_probability']-1)>1e-9:raise ValueError('Invalid game probability')
    for b in bands:
        PlayerInterval.model_validate(b)
        if b['available'] and (b['lower'] is None or b['upper'] is None or not b['observed']<=b['lower']<=b['upper'] or not b['lower']<=b['prediction']<=b['upper']):
            raise ValueError('Invalid prediction interval')
        if b['max_calibration_label_available_at'] and timestamp(b['max_calibration_label_available_at'])>=timestamp(b['prediction_at']):
            raise ValueError('Calibration source is not before prediction')
    for r in extras.get('prospective',{}).get('items',[]):
        if r['status']!='sealed':continue
        if not timestamp(r['max_source_available_at'])<timestamp(r['feature_as_of'])<=timestamp(r['generated_at'])<timestamp(r['cutoff_at']):
            raise ValueError('Invalid prospective cutoff provenance')
        if {m['model'] for m in r['models']}!={'frozen_elo','logistic_team_context'}:raise ValueError('Incomplete prospective pair')
        for m in r['models']:
            if not 0<=m['home_win_probability']<=1 or abs(m['home_win_probability']+m['away_win_probability']-1)>1e-9:
                raise ValueError('Invalid prospective probability')
    for pid in player_ids:
        if {p['model'] for p in players if p['player_id']==pid}!={'pace','prior_shrinkage'}:
            raise ValueError('Missing player projection model')
        if len([b for b in bands if b['player_id']==pid])!=16:raise ValueError('Incomplete player uncertainty records')
    files={};total=0
    with TemporaryDirectory(prefix='.staging-',dir=output) as temporary:
        stage=Path(temporary)
        def save(name,value):
            nonlocal total
            public_values(value);data=encoded(value);total+=len(data)
            if len(data)>MAX_FILE or total>MAX_TOTAL:raise ValueError('Public snapshot exceeds payload budget')
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            # Sidecars support offline transfer; Pages serves its negotiated compression.
            compressed=gzip.compress(data,mtime=0)
            path.with_suffix(path.suffix+'.gz').write_bytes(compressed)
            files[name]=dict(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),gzip_bytes=len(compressed))
        save('metadata.json',metadata);save('catalog.json',identities)
        save('players.json',players);save('intervals.json',bands);save('fixtures.json',fixtures)
        save('standings.json',extras.get('standings',[]));save('prospective.json',extras.get('prospective',dict(items=[],revisions=[],report=None)))
        placeholders=','.join('?' for _ in dates)
        for pid in player_ids:
            condition=f'season=? AND player=? AND prediction_at IN ({placeholders})';params=[season,pid,*dates]
            history=dict(projections=[player_item(r) for r in pages(store,PROJECTIONS,condition,params,'prediction_at,model')],
                intervals=[dict(interval_item(r),last_played_team_id=r['team']) for r in pages(store,INTERVALS,condition,params,'prediction_at,model,target,nominal')])
            save(f'players/{pid}.json',history)
        release=hashlib.sha256(encoded(dict(schema=SCHEMA,files=files))).hexdigest()
        manifest=dict(schema=SCHEMA,release_id=release,files=files,source_as_of=metadata['as_of'],season_id=season,
            player_count=len(player_ids),total_bytes=total,features=dict(live=False,standings=bool(extras.get('standings')),prospective=bool(extras.get('prospective',{}).get('report'))))
        (stage/'manifest.json').write_bytes(encoded(manifest))
        destination=output/'releases'/release
        destination.parent.mkdir(exist_ok=True)
        if destination.exists():validate_release(destination)
        else:stage.rename(destination)
        validate_release(destination)
        pointer=output/'latest.json';temp=output/'latest.tmp'
        temp.write_bytes(encoded(dict(schema=SCHEMA,release_id=release,source_as_of=metadata['as_of'])))
        atomic_replace(temp,pointer)
    return manifest


def validate_release(folder):
    folder=Path(folder);manifest=document(folder/'manifest.json')
    if manifest['schema']!=SCHEMA or not re.fullmatch('[a-f0-9]{64}',manifest['release_id']):raise ValueError('Invalid release manifest')
    expected=hashlib.sha256(encoded(dict(schema=SCHEMA,files=manifest['files']))).hexdigest()
    if expected!=manifest['release_id']:raise ValueError('Release identity mismatch')
    if not {'metadata.json','catalog.json','players.json','intervals.json','fixtures.json','standings.json','prospective.json'}<=manifest['files'].keys():
        raise ValueError('Incomplete release manifest')
    if sum(info['bytes'] for info in manifest['files'].values())>MAX_TOTAL or any(info['bytes']>MAX_FILE for info in manifest['files'].values()):
        raise ValueError('Release exceeds payload budget')
    for name,info in manifest['files'].items():
        if not re.fullmatch(r'(?:players/\d+|metadata|catalog|players|intervals|fixtures|standings|prospective)\.json',name):raise ValueError('Unexpected publication path')
        path=folder/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=info['bytes'] or checksum(path)!=info['sha256']:
            raise ValueError('Publication file hash/size mismatch')
        public_values(document(path))
        with gzip.open(path.with_suffix('.json.gz'),'rb') as compressed:
            if compressed.read(info['bytes']+1)!=path.read_bytes():raise ValueError('Compression mismatch')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT)
    parser.add_argument('--daily-root',type=Path,default=Path('data/predictions/daily'))
    parser.add_argument('--history-limit',type=int,default=32)
    parser.add_argument('--offline',action='store_true',help='Explicitly omit database standings/prospective results')
    parser.add_argument('--validate',type=Path,help='Validate an existing immutable release only')
    args=parser.parse_args()
    if args.validate:print(json.dumps(validate_release(args.validate),indent=2));return
    store=load_daily(args.daily_root);season=max(store.metadata['seasons'])
    if args.offline:extras={}
    else:
        from nhl_analytics.database import connect
        with connect() as connection,connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            extras=database_public(connection,season)
    result=write_release(store,document(Path('dashboard/public/catalog.json')),extras,args.output,args.history_limit)
    print(json.dumps({key:result[key] for key in ('release_id','season_id','player_count','total_bytes','features')},indent=2))


if __name__=='__main__':main()
