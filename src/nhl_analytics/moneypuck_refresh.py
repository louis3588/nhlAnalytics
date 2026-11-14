"""Daily published MoneyPuck team-game download: bounded stream, current season only."""
import argparse
import csv
import hashlib
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from psycopg.rows import dict_row

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.database import connect, migrate
from nhl_analytics.features import write_document
from nhl_analytics.moneypuck import csv_rows, detect_kind, fingerprint, import_file, transform
from nhl_analytics.refresh import current_season, job_lock

URL='https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv'
ROOT=Path('data/moneypuck_daily')
MAX_BYTES=256*1024*1024
MAX_ROWS=500000
LOG=logging.getLogger(__name__)


class BoundedReader(io.RawIOBase):
    def __init__(self,raw,limit=MAX_BYTES,deadline=300):
        self.raw=raw;self.limit=limit;self.deadline=time.monotonic()+deadline
        self.count=0;self.digest=hashlib.sha256()
    def readable(self):return True
    def readinto(self,buffer):
        if time.monotonic()>self.deadline:raise TimeoutError('Download deadline exceeded')
        chunk=self.raw.read(min(len(buffer),65536),decode_content=True)
        self.count+=len(chunk)
        if self.count>self.limit:raise ValueError('Team download exceeded byte limit')
        self.digest.update(chunk);buffer[:len(chunk)]=chunk
        return len(chunk)


def select_current(reader,destination,season,today):
    headers=next(reader)
    if detect_kind(headers)!='team_game' or not {'xGoalsFor','xGoalsAgainst'}<=set(headers):
        raise ValueError('Expected published team-game xG schema')
    pairs={};seen=set();count=selected=0;latest=None
    with destination.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(headers)
        for row in csv_rows(reader,headers):
            count+=1
            if count>MAX_ROWS:raise ValueError('Team download exceeded row limit')
            if row['season']!=str(season//10000):continue
            gid=int(row['gameId'])
            if gid//10000%100!=2:continue
            record=transform(row,'team_game',None,selected+1,None,2)
            if record[5]!=season or record[14]>today:raise ValueError('Invalid season/date in team download')
            if record[4] in seen:raise ValueError('Duplicate current-season team-game situation')
            seen.add(record[4])
            if record[12] not in ('all','5on5','5on4','4on5','other'):raise ValueError('Unknown game situation')
            if record[15] is None or record[16] is None or min(record[15:17])<0:
                raise ValueError('Missing or negative team expected goals')
            if record[12]=='5on5':
                pairs.setdefault(gid,set()).add(record[11])
                latest=max(latest or record[14],record[14])
            writer.writerow([row[h] for h in headers]);selected+=1
    if not selected or not pairs:raise ValueError('No current-season regular-season xG rows')
    if any(len(teams)!=2 for teams in pairs.values()):raise ValueError('Incomplete 5v5 team pair')
    return dict(source_rows=count,selected_rows=selected,latest_game_date=str(latest),
        five_on_five_games=len(pairs),pairs={str(k):sorted(v) for k,v in pairs.items()})


def download(root,season,session,now):
    cache_path=root/'http-cache.json'
    cached=json.loads(cache_path.read_text()) if cache_path.exists() else {}
    headers={}
    if cached.get('season_id')==season:
        if cached.get('etag'):headers['If-None-Match']=cached['etag']
        if cached.get('last_modified'):headers['If-Modified-Since']=cached['last_modified']
    with session.get(URL,headers=headers,stream=True,timeout=(10,30)) as response:
        if response.url!=URL:raise ValueError('Published download redirected; approval/access may be required')
        if response.status_code==304:
            if cached.get('season_id')!=season:raise ValueError('No matching season cache for HTTP 304')
            sha=cached.get('selected_sha256','')
            if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('Invalid cached content identity')
            path=root/'content'/f'{sha}.csv'
            if not path.exists() or fingerprint(path)!=sha:raise ValueError('Cached selection missing or changed')
            return path,dict(cached,not_modified=True,checked_at=now.isoformat())
        response.raise_for_status()
        if response.status_code!=200 or 'text/csv' not in response.headers.get('Content-Type','').lower():
            raise ValueError('Published endpoint did not return CSV')
        temporary=root/'download.partial.csv'
        try:
            bounded=BoundedReader(response.raw)
            with io.TextIOWrapper(io.BufferedReader(bounded),encoding='utf-8-sig',newline='') as text:
                details=select_current(csv.reader(text),temporary,season,now.astimezone(LEAGUE_TZ).date())
            sha=fingerprint(temporary);content=root/'content';content.mkdir(exist_ok=True)
            path=content/f'{sha}.csv'
            if path.exists():
                if fingerprint(path)!=sha:raise ValueError('Immutable selection content changed')
                temporary.unlink()
            else:temporary.replace(path)
            metadata=dict(details,season_id=season,url=URL,selected_sha256=sha,
                source_decompressed_sha256=bounded.digest.hexdigest(),source_decompressed_bytes=bounded.count,
                etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'),
                downloaded_at=datetime.now(timezone.utc).isoformat(),checked_at=now.isoformat(),not_modified=False,
                attribution='MoneyPuck.com',selection='Current-season regular-season rows, all published situations; values unchanged')
            write_document(cache_path,metadata)
            return path,metadata
        finally:temporary.unlink(missing_ok=True)


def coverage(connection,season,metadata,now):
    expected=connection.execute('''SELECT game_id,home_team_abbrev,away_team_abbrev FROM analytics.games
        WHERE season_id=%s AND game_type_id=2 AND game_state IN ('FINAL','OFF') AND start_time_utc<%s''',
        (season,now-timedelta(hours=8))).fetchall()
    pairs=metadata['pairs'];missing=[]
    for gid,home,away in expected:
        if pairs.get(str(gid))!=sorted([home,away]):missing.append(gid)
    return dict(expected_completed_games=len(expected),missing_completed_games=missing,
        freshness='stale' if missing else 'current',final_result_grace_hours=8)


def read_observed_team_xg(connection,season,cutoff):
    """Latest complete WHOLE snapshot before cutoff; no resurrected retracted rows."""
    selected=connection.execute('''SELECT f.file_id,f.completed_at,f.sha256 FROM analytics.moneypuck_files f
        WHERE f.kind='team_game' AND f.status='complete' AND f.completed_at<%s AND f.observed_at<%s
        AND EXISTS(SELECT 1 FROM analytics.moneypuck_records r WHERE r.file_id=f.file_id AND r.season_id=%s AND r.kind='team_game')
        ORDER BY f.completed_at DESC,f.file_id DESC LIMIT 1''',(cutoff,cutoff,season)).fetchone()
    if not selected:return [],None
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT game_id,team_code,game_date,xgoals_for,xgoals_against,
            greatest(observed_at,%s) AS available_at FROM analytics.moneypuck_records
            WHERE file_id=%s AND season_id=%s AND game_type_id=2 AND situation='5on5' AND kind='team_game' AND observed_at<%s
            ORDER BY game_id,team_code''',(selected[1],selected[0],season,cutoff))
        rows=list(cursor)
    return rows,dict(file_id=str(selected[0]),completed_at=selected[1].isoformat(),sha256=selected[2])


def run(root=ROOT,season=None,env_file=Path('.env'),force_check=False,session=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc);season=season or current_season(now)
    day=root/now.date().isoformat();day.mkdir(exist_ok=True)
    manifest_path=day/'latest.json'
    with job_lock(root/'refresh.lock'):
        if manifest_path.exists() and not force_check:
            previous=json.loads(manifest_path.read_text())
            if previous.get('status')=='complete' and previous.get('season_id')==season:
                LOG.info('MoneyPuck already refreshed today; no download or import repeated');return previous
        manifest=dict(status='running',season_id=season,started_at=now.isoformat(),production_models_changed=False)
        write_document(manifest_path,manifest)
        owned=session is None;session=session or requests.Session()
        try:
            with connect(env_file) as connection:
                migrate(connection)  # Fail before downloading if Timescale is unavailable.
                path,metadata=download(root,season,session,now)
                rows=import_file(connection,path,chunk_size=500,game_type=2)
                state=coverage(connection,season,metadata,datetime.now(timezone.utc))
                manifest.update(status='stale' if state['missing_completed_games'] else 'complete',
                    completed_at=datetime.now(timezone.utc).isoformat(),source=metadata,coverage=state,imported_or_existing_rows=rows)
                write_document(manifest_path,manifest);write_document(root/'latest.json',manifest)
                return manifest
        except Exception as error:
            manifest.update(status='failed',error=type(error).__name__)
            write_document(manifest_path,manifest)
            raise
        finally:
            if owned:session.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT)
    parser.add_argument('--season',type=int)
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--force-check',action='store_true',help='Recheck HTTP validators; immutable content imports remain deduplicated')
    args=parser.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s: %(message)s')
    try:result=run(args.output,args.season,args.env_file,args.force_check)
    except Exception as error:parser.exit(1,f'MoneyPuck refresh stopped ({type(error).__name__}); checkpoints retained.\n')
    print(json.dumps(dict(status=result['status'],season_id=result['season_id'],
        latest_game_date=result.get('source',{}).get('latest_game_date'),coverage=result.get('coverage'))))
    if result['status']!='complete':raise SystemExit(1)


if __name__=='__main__':main()
