"""Durable local outbox and bounded static Pages publishing; never trains models."""

import argparse
import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from nhl_analytics.refresh import job_lock

PROJECT=Path(__file__).resolve().parents[2]
DEFAULT_ROOT=PROJECT/'data/publication_automation'
LOG=logging.getLogger(__name__)
POLICY=dict(quiet_seconds=60,max_wait_seconds=120,cooldown_seconds=600,daily_attempts=12,monthly_attempts=360)


def configuration(root=DEFAULT_ROOT):
    path=Path(root)/'config.json'
    if not path.exists():return dict(enabled=False)
    config=json.loads(path.read_text(encoding='utf-8'))
    if config.get('enabled'):
        if not re.fullmatch('[a-f0-9]{32}',config.get('account_id','')):raise ValueError('Invalid publication account')
        if not re.fullmatch('[a-z0-9][a-z0-9-]{1,56}',config.get('project','')):raise ValueError('Invalid Pages project')
        if config.get('site')!=f"https://{config['project']}.pages.dev":raise ValueError('Publication hostname must match project')
        if type(config.get('history_limit',16)) is not int or not 1<=config.get('history_limit',16)<=32:raise ValueError('Invalid history limit')
    return config


class Outbox:
    def __init__(self,root=DEFAULT_ROOT):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'state.sqlite3'
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,reason TEXT NOT NULL,created REAL NOT NULL,delivered REAL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,created REAL NOT NULL,job_id TEXT NOT NULL,status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS pending_events ON events(delivered,id);''')

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def enqueue(self,key,reason,now=None):
        self.enqueue_many([(key,reason)],now)

    def enqueue_many(self,events,now=None):
        with self.connect() as c:
            c.executemany('INSERT OR IGNORE INTO events(event_key,reason,created) VALUES (?,?,?)',
                [(key,reason,time.time() if now is None else now) for key,reason in events])

    def get(self,key,default=None):
        with self.connect() as c:row=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,json.dumps(value)))

    def pending(self):
        with self.connect() as c:return dict(c.execute('SELECT count(*) AS n,min(created) AS first,max(created) AS last,max(id) AS highwater FROM events WHERE delivered IS NULL').fetchone())

    def counts(self,now):
        instant=datetime.fromtimestamp(now,timezone.utc)
        day=instant.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        month=instant.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.connect() as c:
            return dict(day=c.execute('SELECT count(*) FROM attempts WHERE created>=?',(day,)).fetchone()[0],
                month=c.execute('SELECT count(*) FROM attempts WHERE created>=?',(month,)).fetchone()[0])

    def reserve(self,job,now):
        # The file lock owns the pipeline; SQLite makes reservation crash durable.
        counts=self.counts(now)
        if counts['day']>=POLICY['daily_attempts'] or counts['month']>=POLICY['monthly_attempts']:return None
        with self.connect() as c:
            cursor=c.execute("INSERT INTO attempts(created,job_id,status) VALUES (?,?,'uncertain')",(now,job['id']))
            return cursor.lastrowid

    def success(self,job,now,uploaded):
        versions=[job['release_id'],*[v for v in self.get('verified_releases',[]) if v!=job['release_id']]][:3]
        with self.connect() as c:
            c.execute('UPDATE events SET delivered=? WHERE delivered IS NULL AND id<=?',(now,job['highwater']))
            if job.get('attempt_id'):c.execute("UPDATE attempts SET status='verified' WHERE id=?",(job['attempt_id'],))
            values=dict(active_job=None,failures=0,retry_at=0,last_failure=None,verified_releases=versions,last_success=dict(at=now,release_id=job['release_id'],
                fingerprint=job['fingerprint'],uploaded=uploaded,site=job['target']['site'],job_id=job['id']))
            if uploaded:values['last_upload']=now
            for key,value in values.items():c.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,json.dumps(value)))


def request_publication(key,reason,root=DEFAULT_ROOT):
    if not configuration(root).get('enabled'):return False
    Outbox(root).enqueue(key,reason)
    return True


def queue_daily_publication(publication):
    try:
        if publication and publication.get('bundle_sha256'):
            request_publication('daily:'+publication['bundle_sha256'],'daily_refresh')
    except Exception as error:LOG.warning('Public queue unavailable (%s); reconciliation will recover the event',type(error).__name__)


def memory_ready():
    if os.name!='nt':return True
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong) for name in
            ('total_phys','available_phys','total_page','available_page','total_virtual','available_virtual','extended')]
    memory=Memory();memory.length=ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):return False
    return memory.available_phys>=256*1024**2 and memory.available_page>=384*1024**2


def artifact_fingerprint(site):
    digest=hashlib.sha256()
    for path in sorted(Path(site).rglob('*')):
        if path.is_symlink():raise ValueError('Symlink in publication artifact')
        if path.is_file():
            digest.update(path.relative_to(site).as_posix().encode()+b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def frontend_files(source):
    source=Path(source)
    if (source/'404.html').exists() or (source/'_redirects').exists():raise ValueError('Native SPA frontend required')
    selected=[]
    for path in sorted(source.rglob('*')):
        relative=path.relative_to(source).as_posix()
        if re.fullmatch(r'(?:index\.html|_headers|rink\.svg|inter-LICENSE\.txt|assets/[^/]+\.(?:js|css|woff2)|team-logos/\d+(?:-\d{8})?\.svg)',relative):
            if path.is_symlink() or not path.is_file():raise ValueError('Invalid frontend file')
            if path.stat().st_size>25*1024**2:raise ValueError('Frontend file exceeds Pages limit')
            selected.append(path)
    if not {'index.html','_headers'}<={p.name for p in selected}:raise ValueError('Incomplete reviewed frontend')
    if any('new EventSource' in p.read_text(encoding='utf-8') for p in selected if p.suffix=='.js'):
        raise ValueError('Static public build required, not API/live mode')
    return selected


def frontend_fingerprint(source):
    source=Path(source);digest=hashlib.sha256()
    for path in frontend_files(source):
        digest.update(path.relative_to(source).as_posix().encode()+b'\0');digest.update(path.read_bytes())
    return digest.hexdigest()


def pin_frontend(source,root=DEFAULT_ROOT,project=PROJECT):
    root=Path(root);source=Path(source).resolve();project=Path(project);box=Outbox(root)
    with job_lock(root/'pipeline.lock'):
        identity=frontend_fingerprint(source);destination=root/'frontends'/identity
        if not destination.exists():
            destination.mkdir(parents=True)
            for file in frontend_files(source):
                target=destination/file.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
        if frontend_fingerprint(destination)!=identity:raise ValueError('Frontend changed during pinning')
        box.set('frontend',dict(id=identity,path=str(destination.resolve())))
        key='frontend:'+identity;box.enqueue(key,'reviewed_frontend')
        # On initial setup the pinned UI may already be part of the verified
        # hosted artifact. Acknowledge only this UI event, never data events.
        last=box.get('last_success')
        previous=project/'.work/public-publisher'/(last or {}).get('job_id','missing')/'site'
        if last and previous.exists() and frontend_fingerprint(previous)==identity:
            with box.connect() as c:c.execute('UPDATE events SET delivered=? WHERE event_key=?',(last['at'],key))
        active=box.get('active_job')
        if active and active['phase']=='prepared':box.set('active_job',None)
        return identity


class PublicPublisher:
    def __init__(self,root=DEFAULT_ROOT,project=PROJECT,clock=time.time,runner=subprocess.run):
        LOG.setLevel(logging.INFO)
        if not LOG.handlers and not logging.getLogger().handlers:
            handler=logging.StreamHandler();handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'));LOG.addHandler(handler)
        self.root=Path(root);self.project=Path(project);self.clock=clock;self.runner=runner
        self.box=Outbox(root);self.health=dict(status='starting');self.dashboard=self.project/'dashboard'

    def status(self,status,**extra):
        self.health=dict(status=status,checked_at=self.clock(),pending_events=self.box.pending()['n'],
            attempts=self.box.counts(self.clock()),limits=POLICY,last_success=self.box.get('last_success'),last_failure=self.box.get('last_failure'),**extra)
        self.box.set('health',self.health)
        return self.health

    def discover(self):
        pointer=self.project/os.environ.get('NHL_DAILY_ROOT','data/predictions/daily')/'latest.json'
        if pointer.exists():
            queue=json.loads(pointer.read_text());self.box.enqueue('daily:'+queue['bundle_sha256'],'daily_refresh',self.clock())
        from nhl_analytics.database import connect
        with connect(self.project/'.env') as c,c.transaction():
            c.execute('SET TRANSACTION READ ONLY')
            ids=c.execute('SELECT batch_id FROM analytics.prospective_batches WHERE campaign=%s',('elo_vs_logistic_v1',)).fetchall()
            results=c.execute('''SELECT r.result_id FROM analytics.prospective_validated_results r JOIN analytics.games g USING(game_id)
                JOIN analytics.prospective_campaigns c ON c.season_id=g.season_id AND g.game_date>=c.tracking_date
                WHERE c.campaign=%s''',('elo_vs_logistic_v1',)).fetchall()
        self.box.enqueue_many([('batch:'+str(row[0]),'pregame_status') for row in ids]+
            [('result:'+str(row[0]),'validated_result') for row in results],self.clock())

    def environment(self,config,job=None):
        env=dict(os.environ,CI='true',NO_COLOR='1',WRANGLER_SEND_METRICS='false',WRANGLER_LOG_SANITIZE='true',
            WRANGLER_LOG_LEVEL='error',CLOUDFLARE_ACCOUNT_ID=config['account_id'],RAYON_NUM_THREADS='1',UV_THREADPOOL_SIZE='1',
            OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',PYTHONUNBUFFERED='1')
        temporary=self.project/'.work/tmp';temporary.mkdir(parents=True,exist_ok=True)
        (self.project/'.work/logs/public-publisher').mkdir(parents=True,exist_ok=True)
        env.update(TEMP=str(temporary),TMP=str(temporary),npm_config_cache=str(self.project/'.work/npm-cache'),
            WRANGLER_LOG_PATH=str(self.project/'.work/logs/public-publisher/wrangler.log'))
        if job:env.update(NHL_PUBLICATION_SOURCE=str(self.root/'publication'),NHL_PUBLIC_BUILD_OUTPUT=job['site'],
            NHL_PUBLIC_VERIFY_REPORT=str(Path(job['site']).parent/'verification.json'),
            NHL_PUBLIC_PREVIOUS_RELEASES=json.dumps(self.box.get('verified_releases',[])))
        if job and job.get('frontend'):env['NHL_PUBLIC_FRONTEND_TEMPLATE']=job['frontend']['path']
        return env

    def node(self,*arguments):
        executable=shutil.which('node')
        if not executable:raise RuntimeError('Node is unavailable')
        return [executable,'--max-old-space-size=192','--max-semi-space-size=2',*arguments]

    def command(self,args,config,job=None,timeout=300,capture=False,cwd=None):
        result=self.runner(args,cwd=cwd or self.dashboard,env=self.environment(config,job),stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',errors='replace',timeout=timeout,check=False)
        if capture:return result
        if result.returncode:raise RuntimeError('Publishing subprocess failed')
        return result

    def authorize(self,config):
        result=self.command(self.node('node_modules/wrangler/bin/wrangler.js','whoami','--json'),config,timeout=45,capture=True)
        if result.returncode:raise PermissionError('Wrangler authentication needs attention')
        auth=json.loads(result.stdout)
        if not auth.get('loggedIn') or config['account_id'] not in {a['id'] for a in auth.get('accounts',[])} or 'pages:write' not in auth.get('tokenPermissions',[]):
            raise PermissionError('Wrangler account/Pages permission mismatch')
        projects=self.command(self.node('node_modules/wrangler/bin/wrangler.js','pages','project','list','--json'),config,timeout=45,capture=True)
        if projects.returncode:raise PermissionError('Cannot verify existing Pages project')
        listed=json.loads(projects.stdout)
        target=next((p for p in listed if p.get('name',p.get('Project Name'))==config['project']),None)
        if target is None:raise PermissionError('Configured Pages project is missing; automatic creation is forbidden')
        if target.get('production_branch','main')!='main':raise PermissionError('Pages production branch changed')

    def verify(self,config,job):
        env=self.environment(config,job)
        result=self.runner(self.node('scripts/verify-deployment.mjs',config['site']),cwd=self.dashboard,env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90,check=False)
        return result.returncode==0

    def prepare(self,config,pending):
        frontend=self.box.get('frontend')
        if not frontend or frontend_fingerprint(Path(frontend['path']))!=frontend['id']:raise ValueError('Reviewed frontend is not pinned')
        jid=uuid4().hex;folder=self.project/'.work/public-publisher'/jid
        folder.mkdir(parents=True,exist_ok=False)
        job=dict(id=jid,highwater=pending['highwater'],site=str(folder/'site'),phase='preparing',lease_until=self.clock()+660,
            target={k:config[k] for k in ('account_id','project','site')},frontend=frontend)
        self.box.set('active_job',job)
        self.status('exporting')
        # Source ingestion and large exports/builds do not compete on this machine.
        with job_lock(self.project/'data/refresh/refresh.lock'):
            self.command([sys.executable,'-m','nhl_analytics.publication','--output',str(self.root/'publication'),
                '--history-limit',str(config.get('history_limit',16))],config,job,timeout=300,cwd=self.project)
            self.status('packaging')
            self.command(self.node('scripts/package-public.mjs'),config,job,timeout=240)
        pointer=json.loads((Path(job['site'])/'publication/latest.json').read_text())
        job.update(release_id=pointer['release_id'],fingerprint=artifact_fingerprint(Path(job['site'])),phase='prepared',lease_until=0)
        self.box.set('active_job',job)
        return job

    def cleanup(self,keep):
        # Only generated UUID job directories under this exact task root.
        root=(self.project/'.work/public-publisher').resolve()
        if root.exists():
            for folder in root.iterdir():
                if folder.name in keep or not re.fullmatch('[a-f0-9]{32}',folder.name) or folder.is_symlink():continue
                if folder.resolve().parent!=root:raise ValueError('Unsafe publication cleanup target')
                shutil.rmtree(folder)
        # Publication derivatives are reproducible; ledger/forecast history is untouched.
        releases=(self.root/'publication/releases').resolve()
        if releases.exists():
            versions=sorted((p for p in releases.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch('[a-f0-9]{64}',p.name)),key=lambda p:p.stat().st_mtime,reverse=True)
            retained={p.name for p in versions[:3]}|set(self.box.get('verified_releases',[]))
            for folder in versions:
                if folder.name in retained:continue
                if folder.resolve().parent!=releases:raise ValueError('Unsafe release cleanup target')
                shutil.rmtree(folder)

    def tick(self,flush=False):
        try:config=configuration(self.root)
        except (OSError,ValueError):return self.status('configuration_error')
        if not config.get('enabled'):return self.status('disabled')
        try:
            with job_lock(self.root/'pipeline.lock'):
                try:
                    self.discover()
                    pending=self.box.pending();now=self.clock()
                    if not pending['n']:return self.status('idle')
                    if self.box.get('retry_at',0)>now:return self.status('retry_wait',next_attempt=self.box.get('retry_at'))
                    if not flush and now-pending['last']<POLICY['quiet_seconds'] and now-pending['first']<POLICY['max_wait_seconds']:
                        return self.status('batching')
                    if now-self.box.get('last_upload',0)<POLICY['cooldown_seconds']:return self.status('cooldown',next_attempt=self.box.get('last_upload')+POLICY['cooldown_seconds'])
                    counts=self.box.counts(now)
                    if counts['day']>=POLICY['daily_attempts'] or counts['month']>=POLICY['monthly_attempts']:return self.status('budget_wait')
                    if not memory_ready():return self.status('resource_wait')
                    self.status('checking_auth')
                    self.authorize(config)
                    job=self.box.get('active_job')
                    if job and job['phase']=='preparing':
                        if job['lease_until']>now:return self.status('recovering',next_attempt=job['lease_until'])
                        self.box.set('active_job',None);job=None
                    if not job:
                        self.cleanup({(self.box.get('last_success') or {}).get('job_id')})
                        job=self.prepare(config,pending)
                    if job.get('target')!={k:config[k] for k in ('account_id','project','site')}:
                        raise PermissionError('Prepared job belongs to a different target')
                    if artifact_fingerprint(Path(job['site']))!=job['fingerprint']:raise ValueError('Prepared publication changed')
                    # Reconcile crashes/unknown upload outcomes and bootstrap against
                    # the already hosted content before issuing any new deployment.
                    self.status('verifying')
                    verified=self.verify(config,job)
                    if not verified:
                        if configuration(self.root)!=config:return self.status('configuration_changed')
                        if job.get('lease_until',0)>now:return self.status('recovering',next_attempt=job['lease_until'])
                        attempt=self.box.reserve(job,self.clock())
                        if attempt is None:return self.status('budget_wait')
                        job.update(attempt_id=attempt,phase='deploying',lease_until=self.clock()+390)
                        self.box.set('active_job',job)
                        self.status('uploading',release_id=job['release_id'])
                        # Run in the isolated job parent, outside both the checkout
                        # and site. Wrangler writes .wrangler/pages.json in cwd;
                        # that cache must not mutate the immutable site fingerprint.
                        self.command(self.node(str(self.dashboard/'node_modules/wrangler/bin/wrangler.js'),'pages','deploy',job['site'],
                            '--project-name',config['project'],'--branch','main','--commit-dirty=true','--commit-message','Automated validated NHL snapshot'),
                            config,job,timeout=300,cwd=str(Path(job['site']).parent))
                        job['phase']='verifying';self.box.set('active_job',job)
                        self.status('verifying',release_id=job['release_id'])
                        if not self.verify(config,job):raise RuntimeError('Hosted publication verification failed')
                    uploaded=not verified or bool(job.get('attempt_id'))
                    self.box.success(job,self.clock(),uploaded=uploaded)
                    self.cleanup({job['id']})
                    LOG.info('Public snapshot verified: %s (%s)',job['release_id'][:12],'uploaded' if not verified else 'already hosted; upload skipped')
                    return self.status('published' if uploaded else 'unchanged')
                except Exception as error:
                    job=self.box.get('active_job')
                    safe_reasons={
                        'Wrangler authentication needs attention':'wrangler_authentication_failed',
                        'Wrangler account/Pages permission mismatch':'account_or_scope_mismatch',
                        'Cannot verify existing Pages project':'project_access_failed',
                        'Configured Pages project is missing; automatic creation is forbidden':'project_missing',
                        'Pages production branch changed':'production_branch_changed'}
                    self.box.set('last_failure',dict(at=self.clock(),error=type(error).__name__,
                        phase=(job or {}).get('phase','discovery_or_auth'),reason=safe_reasons.get(str(error),'operation_failed')))
                    if job and job['phase']=='preparing' and not isinstance(error,subprocess.TimeoutExpired):self.box.set('active_job',None)
                    failures=self.box.get('failures',0)+1;retry=self.clock()+min(3600,300*2**min(failures-1,4))
                    self.box.set('failures',failures);self.box.set('retry_at',retry)
                    LOG.warning('Public publisher deferred (%s); events retained',type(error).__name__)
                    return self.status('auth_required' if isinstance(error,PermissionError) else 'retry_wait',error=type(error).__name__,
                        phase=(job or {}).get('phase','discovery_or_auth'),next_attempt=retry)
        except RuntimeError:
            self.health=dict(self.box.get('health',dict(status='busy')),following=True)
            return dict(status='busy',checked_at=self.clock())

    async def run(self):
        while True:
            task=asyncio.create_task(asyncio.to_thread(self.tick))
            try:await asyncio.shield(task)
            except asyncio.CancelledError:await task;raise
            await asyncio.sleep(30)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--flush',action='store_true',help='Skip quiet-period batching only, never budget/cooldown/leases')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--check-auth',action='store_true',help='Verify existing Pages access and clear retry backoff after successful authentication; never deploys')
    parser.add_argument('--pin-frontend',action='store_true',help='Pin the tested dist-public frontend for data-only deployments')
    args=parser.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s: %(message)s')
    publisher=PublicPublisher(args.root)
    if args.check_auth:
        with job_lock(args.root/'pipeline.lock'):
            publisher.authorize(configuration(args.root))
            publisher.box.set('retry_at',0)
            publisher.box.set('failures',0)
            publisher.box.set('last_failure',None)
        print(json.dumps(dict(status='auth_verified')))
        return
    if args.pin_frontend:print(json.dumps(dict(frontend_id=pin_frontend(PROJECT/'dashboard/dist-public',args.root))));return
    if args.status:print(json.dumps(publisher.box.get('health',dict(status='not_started')),indent=2));return
    if args.once:
        result=publisher.tick(args.flush);print(json.dumps(result,indent=2))
        if result['status'] in ('auth_required','retry_wait','configuration_error','configuration_changed'):raise SystemExit(1)
    else:asyncio.run(publisher.run())


if __name__=='__main__':main()
