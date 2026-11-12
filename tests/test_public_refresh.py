import json
import subprocess
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from nhl_analytics.public_refresh import Outbox,PublicPublisher,POLICY,artifact_fingerprint,configuration,request_publication,pin_frontend,frontend_fingerprint
from nhl_analytics.refresh import job_lock,run_refresh


class FakePublisher(PublicPublisher):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.verifications=[False,True];self.uploads=0;self.preparations=0;self.auths=0;self.action=None
    def discover(self):pass
    def authorize(self,config):self.auths+=1
    def prepare(self,config,pending):
        self.preparations+=1;jid=uuid4().hex;site=self.project/'.work/public-publisher'/jid/'site';site.mkdir(parents=True)
        (site/'index.html').write_text('saved-site')
        job=dict(id=jid,highwater=pending['highwater'],site=str(site),phase='prepared',lease_until=0,
            release_id='a'*64,fingerprint=artifact_fingerprint(site),target={k:config[k] for k in ('account_id','project','site')})
        self.box.set('active_job',job);return job
    def verify(self,config,job):return self.verifications.pop(0) if self.verifications else True
    def command(self,args,config,job=None,**kwargs):
        self.uploads+=1
        if self.action:self.action()
        self.last_command=args
        return SimpleNamespace(returncode=0)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.queue=self.root/'queue';self.queue.mkdir()
        self.project=self.root/'project';self.project.mkdir()
        self.config=dict(enabled=True,account_id='0'*32,project='test-site',site='https://test-site.pages.dev')
        (self.queue/'config.json').write_text(json.dumps(self.config))
        self.now=datetime(2026,11,15,12,tzinfo=timezone.utc).timestamp()
        self.publisher=FakePublisher(self.queue,self.project,clock=lambda:self.now)
        self.memory=patch('nhl_analytics.public_refresh.memory_ready',return_value=True);self.memory.start();self.addCleanup(self.memory.stop)
    def event(self,key='daily:one',age=180):self.publisher.box.enqueue(key,'fixture',self.now-age)

    def test_duplicate_events_and_restart_do_not_duplicate_uploads(self):
        self.event();self.event()
        self.assertEqual(self.publisher.tick()['status'],'published')
        self.assertEqual(self.publisher.uploads,1)
        self.assertIn('deploy',self.publisher.last_command)
        self.assertNotIn('--force',self.publisher.last_command)
        self.event()
        restarted=FakePublisher(self.queue,self.project,clock=lambda:self.now)
        self.assertEqual(restarted.tick()['status'],'idle');self.assertEqual(restarted.uploads,0)
        self.assertEqual(restarted.box.counts(self.now)['day'],1)

    def test_already_hosted_snapshot_never_uses_deployment_budget(self):
        self.event();self.publisher.verifications=[True]
        self.assertEqual(self.publisher.tick()['status'],'unchanged')
        self.assertEqual(self.publisher.uploads,0);self.assertEqual(self.publisher.box.counts(self.now)['day'],0)

    def test_coalesces_events_but_max_wait_prevents_starvation(self):
        self.event(age=0)
        self.assertEqual(self.publisher.tick()['status'],'batching')
        self.now+=90;self.event('seal:two',age=0)
        self.assertEqual(self.publisher.tick()['status'],'batching')
        self.now+=31
        self.assertEqual(self.publisher.tick()['status'],'published')
        self.assertEqual(self.publisher.box.pending()['n'],0)

    def test_events_arriving_during_upload_are_retained_and_cooled_down(self):
        self.event();self.publisher.action=lambda:self.event('seal:late',age=0)
        self.publisher.tick();self.assertEqual(self.publisher.box.pending()['n'],1)
        self.now+=121
        self.assertEqual(self.publisher.tick(flush=True)['status'],'cooldown')
        self.assertEqual(self.publisher.uploads,1)

    def test_unknown_upload_outcome_reconciles_after_restart(self):
        self.event()
        self.publisher.action=lambda:(_ for _ in ()).throw(subprocess.TimeoutExpired('wrangler',300))
        self.assertEqual(self.publisher.tick()['status'],'retry_wait')
        self.assertEqual(self.publisher.box.pending()['n'],1)
        self.now+=301
        restarted=FakePublisher(self.queue,self.project,clock=lambda:self.now);restarted.verifications=[True]
        self.assertEqual(restarted.tick()['status'],'published')
        self.assertEqual(restarted.uploads,0);self.assertEqual(restarted.box.pending()['n'],0)
        self.assertEqual(restarted.box.counts(self.now)['day'],1)

    def test_inflight_lease_prevents_a_second_deployment(self):
        self.event();self.publisher.action=lambda:(_ for _ in ()).throw(subprocess.TimeoutExpired('wrangler',300))
        self.publisher.tick();self.now+=301
        self.publisher.action=None;self.publisher.verifications=[False]
        self.assertEqual(self.publisher.tick()['status'],'recovering')
        self.assertEqual(self.publisher.uploads,1)

    def test_budget_caps_and_flush_cannot_override(self):
        self.event()
        for _ in range(POLICY['daily_attempts']):self.publisher.box.reserve(dict(id='reserved'),self.now)
        self.assertEqual(self.publisher.tick(flush=True)['status'],'budget_wait')
        self.assertEqual(self.publisher.preparations,0);self.assertEqual(self.publisher.box.pending()['n'],1)
        other=Outbox(self.root/'monthly')
        for _ in range(POLICY['monthly_attempts']):
            with other.connect() as c:c.execute("INSERT INTO attempts(created,job_id,status) VALUES (?,'x','unknown')",(self.now-86400,))
        self.assertIsNone(other.reserve(dict(id='blocked'),self.now))

    def test_resource_and_auth_failures_keep_events(self):
        self.event()
        with patch('nhl_analytics.public_refresh.memory_ready',return_value=False):
            self.assertEqual(self.publisher.tick()['status'],'resource_wait')
        with patch.object(self.publisher,'authorize',side_effect=PermissionError('not logged in')):
            self.assertEqual(self.publisher.tick()['status'],'auth_required')
        self.assertEqual(self.publisher.preparations,0);self.assertEqual(self.publisher.box.pending()['n'],1)

    def test_global_lock_and_disabled_config_are_safe(self):
        self.event()
        with job_lock(self.queue/'pipeline.lock'):self.assertEqual(self.publisher.tick()['status'],'busy')
        (self.queue/'config.json').write_text('{broken')
        self.assertEqual(self.publisher.tick()['status'],'configuration_error')
        (self.queue/'config.json').write_text(json.dumps(dict(self.config,enabled=False)))
        self.assertFalse(request_publication('x','disabled',self.queue))
        self.assertEqual(self.publisher.tick()['status'],'disabled')

    def test_target_mismatch_is_rejected_before_upload(self):
        self.event();job=self.publisher.prepare(self.config,self.publisher.box.pending())
        job['target']['project']='other-site';self.publisher.box.set('active_job',job)
        self.assertEqual(self.publisher.tick()['status'],'auth_required');self.assertEqual(self.publisher.uploads,0)

    def test_ingestion_lock_blocks_heavy_preparation(self):
        real=PublicPublisher(self.queue,self.project,clock=lambda:self.now,runner=lambda *a,**k:self.fail('Child must not start'))
        template=self.root/'template';template.mkdir();(template/'index.html').write_text('reviewed');(template/'_headers').write_text('/*')
        real.box.set('frontend',dict(id=frontend_fingerprint(template),path=str(template)))
        self.event()
        with job_lock(self.project/'data/refresh/refresh.lock'),self.assertRaises(RuntimeError):
            real.prepare(self.config,real.box.pending())

    def test_completed_daily_refresh_enqueues_without_repeating_imports(self):
        runner=lambda *a,**k:SimpleNamespace(returncode=0)
        with patch('nhl_analytics.refresh.connect'),patch('nhl_analytics.public_refresh.queue_daily_publication') as hook:
            result=run_refresh(self.root/'refresh',20262027,Path('.env'),runner=runner,publisher=lambda *a:dict(bundle_sha256='saved'))
            self.assertEqual(result['status'],'complete');hook.assert_called_once_with(dict(bundle_sha256='saved'))
            hook.reset_mock()
            run_refresh(self.root/'refresh',20262027,Path('.env'),runner=lambda *a,**k:self.fail('Imports must not repeat'))
            hook.assert_called_once_with(dict(bundle_sha256='saved'))

    def test_failed_refresh_resumes_after_completed_stage(self):
        calls=[]
        def interrupted(args,**kwargs):
            calls.append(args[2])
            return SimpleNamespace(returncode=1 if args[2]=='nhl_analytics.schedules' else 0)
        with patch('nhl_analytics.refresh.connect'),patch('nhl_analytics.public_refresh.queue_daily_publication'):
            with self.assertRaises(RuntimeError):
                run_refresh(self.root/'refresh',20262027,Path('.env'),runner=interrupted)
            self.assertEqual(calls,['nhl_analytics.ingest','nhl_analytics.schedules'])
            calls.clear()
            def resumed(args,**kwargs):
                calls.append(args[2]);return SimpleNamespace(returncode=0)
            result=run_refresh(self.root/'refresh',20262027,Path('.env'),runner=resumed,
                publisher=lambda *a:dict(bundle_sha256='resumed'))
            self.assertEqual(result['status'],'complete')
            self.assertNotIn('nhl_analytics.ingest',calls)
            self.assertEqual(calls[0],'nhl_analytics.schedules')

    def test_frontend_pin_freezes_reviewed_files_and_excludes_snapshots(self):
        template=self.root/'template';template.mkdir();(template/'index.html').write_text('reviewed');(template/'_headers').write_text('/*')
        (template/'publication').mkdir();(template/'publication/private.json').write_text('not part of the frontend')
        identity=pin_frontend(template,self.queue,self.project)
        pin=self.publisher.box.get('frontend');self.assertEqual(pin['id'],identity)
        self.assertFalse((Path(pin['path'])/'publication').exists())
        (template/'index.html').write_text('unreviewed edit')
        self.assertEqual((Path(pin['path'])/'index.html').read_text(),'reviewed')
        self.assertEqual(self.publisher.box.pending()['n'],1)

    def test_api_mode_frontend_cannot_be_pinned(self):
        template=self.root/'template';(template/'assets').mkdir(parents=True)
        (template/'index.html').write_text('api');(template/'_headers').write_text('/*')
        (template/'assets/app.js').write_text('new EventSource("/api/live/stream")')
        with self.assertRaisesRegex(ValueError,'Static public build'):pin_frontend(template,self.queue,self.project)

    def test_missing_cloudflare_project_never_reaches_creation(self):
        publisher=PublicPublisher(self.queue,self.project)
        auth=dict(loggedIn=True,accounts=[dict(id=self.config['account_id'])],tokenPermissions=['pages:write'])
        replies=[SimpleNamespace(returncode=0,stdout=json.dumps(auth)),SimpleNamespace(returncode=0,stdout='[]')]
        with patch.object(publisher,'command',side_effect=replies) as calls:
            with self.assertRaisesRegex(PermissionError,'creation is forbidden'):publisher.authorize(self.config)
            self.assertEqual(calls.call_count,2)

    def test_auth_check_clears_backoff_only_after_verified_access(self):
        from nhl_analytics.public_refresh import main
        self.publisher.box.set('retry_at',9999999999)
        self.publisher.box.set('failures',8)
        args=['public_refresh','--root',str(self.queue),'--check-auth']
        with patch('sys.argv',args),patch.object(PublicPublisher,'authorize',side_effect=PermissionError('expired')):
            with self.assertRaises(PermissionError):main()
        self.assertEqual(self.publisher.box.get('retry_at'),9999999999)
        with patch('sys.argv',args),patch.object(PublicPublisher,'authorize'),patch.object(PublicPublisher,'tick',side_effect=AssertionError('Must not deploy')):
            main()
        self.assertEqual(self.publisher.box.get('retry_at'),0)
        self.assertEqual(self.publisher.box.get('failures'),0)

    def test_installed_wrangler_project_list_display_keys_are_supported(self):
        publisher=PublicPublisher(self.queue,self.project)
        auth=dict(loggedIn=True,accounts=[dict(id=self.config['account_id'])],tokenPermissions=['pages:write'])
        replies=[SimpleNamespace(returncode=0,stdout=json.dumps(auth)),
            SimpleNamespace(returncode=0,stdout=json.dumps([{'Project Name':self.config['project']}]))]
        with patch.object(publisher,'command',side_effect=replies):publisher.authorize(self.config)

    def test_atomic_pointer_retry_keeps_old_snapshot_until_replace(self):
        from nhl_analytics.features import atomic_replace
        pointer=self.root/'latest.json';pointer.write_text('old')
        temporary=self.root/'latest.tmp';temporary.write_text('new')
        error=PermissionError('busy reader');error.winerror=32
        original=Path.replace;calls=[]
        def replace(path,target):
            calls.append(path)
            if len(calls)==1:
                self.assertEqual(pointer.read_text(),'old');raise error
            return original(path,target)
        with patch('nhl_analytics.features.os.name','nt'),patch.object(Path,'replace',replace),patch('nhl_analytics.features.time.sleep'):
            atomic_replace(temporary,pointer)
        self.assertEqual(pointer.read_text(),'new');self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
