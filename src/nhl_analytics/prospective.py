"""Prospective, immutable Elo/logistic forecasts and independent postgame scoring."""

import argparse
import asyncio
import logging
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path

import psycopg

from nhl_analytics.asof import LEAGUE_TZ
from nhl_analytics.database import connect,migrate
from nhl_analytics.live_store import digest
from nhl_analytics.prospective_features import DEFAULT_MODEL_RUN,FeatureUnavailable,load_bundle,load_observed_inputs,observed_features
from nhl_analytics.prospective_reports import game_item,performance,save_report
from nhl_analytics.prospective_store import ProspectiveRepository,jsonable

LOG=logging.getLogger(__name__)
DEFAULT_CAMPAIGN='elo_vs_logistic_v1'


def decision(fixture,now,early=False):
    if fixture['schedule_state']!='OK':return 'missing','postponed_or_unconfirmed'
    if fixture['game_state'] not in ('FUT','PRE'):return 'missing','already_started_or_final'
    if now>=fixture['cutoff_at']:return 'missing','missed_cutoff'
    if now>=fixture['cutoff_at']-timedelta(minutes=5):return 'generate','standard'
    if early and fixture['game_date']==now.astimezone(LEAGUE_TZ).date():return 'generate','early'
    return 'wait','awaiting_cutoff'


class ProspectiveTracker:
    def __init__(self,campaign=DEFAULT_CAMPAIGN,model_run=None,report_root=None,connection_factory=connect):
        if not campaign or len(campaign)>80 or not all(c.isalnum() or c in '_-' for c in campaign):raise ValueError('Invalid campaign name')
        LOG.setLevel(logging.INFO)
        if not LOG.handlers and not logging.getLogger().handlers:
            handler=logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
            LOG.addHandler(handler)
        self.name=campaign
        self.model_run=Path(model_run or os.environ.get('NHL_PROSPECTIVE_MODEL_RUN') or DEFAULT_MODEL_RUN)
        self.report_root=Path(report_root or os.environ.get('NHL_PROSPECTIVE_REPORT_ROOT','data/prospective/reports'))/campaign
        self.connection_factory=connection_factory;self.connection=None;self.repo=None;self.owner=False;self.campaign=None
        self.report_hash=None
        self.cache=dict(items=[],revisions=[],performance=None,health=dict(status='starting',error=None))

    def ensure(self):
        if self.connection is None or self.connection.closed:
            self.connection=self.connection_factory();migrate(self.connection);self.repo=ProspectiveRepository(self.connection)
            self.owner=self.repo.acquire()
        elif not self.owner:self.owner=self.repo.acquire()
        self.campaign=self.repo.campaign(self.name)
        if self.campaign is None and self.owner:
            self.campaign=self.repo.register(self.name,load_bundle(self.model_run))
            LOG.info('Prospective campaign registered: %s (%s)',self.name,self.campaign['model_set_id'][:12])

    def tick(self,early=False):
        try:
            self.ensure()
            if self.campaign is None:
                self.cache['health']=dict(status='waiting_for_campaign',error=None)
                return self.cache
            now=self.repo.clock()
            if self.owner:
                fixtures=self.repo.discover(self.campaign,now)
                prepared=None;as_of=None
                for fixture in fixtures:
                    if self.repo.batch(self.name,fixture['fixture_id']):continue
                    action,reason=decision(fixture,now,early)
                    if action=='wait':continue
                    if action=='missing':
                        if reason=='missed_cutoff' and fixture['cutoff_at']<self.campaign['registered_at']:
                            reason='tracking_started_after_cutoff'
                        self.repo.missing(self.name,fixture,reason)
                        LOG.info('Prospective missing %s: %s',fixture['game_id'],reason)
                        continue
                    try:
                        if prepared is None:
                            as_of=self.repo.clock()
                            with self.connection.transaction():
                                self.connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                                prepared=load_observed_inputs(self.connection,self.campaign,as_of)
                        features,elo,logistic,latest,provenance=observed_features(self.connection,self.campaign,fixture,as_of,prepared)
                        sealed=self.repo.seal(self.name,fixture,features,elo,logistic,as_of,latest,provenance,reason)
                        try:
                            from nhl_analytics.public_refresh import request_publication
                            request_publication('batch:'+str(sealed['batch_id']),'sealed_pregame')
                        except Exception as queue_error:LOG.warning('Public queue deferred (%s); reconciliation will recover the seal',type(queue_error).__name__)
                        LOG.info('Sealed prospective pair %s (%s), Elo %.4f logistic %.4f',fixture['game_id'],sealed['timing'],elo,logistic)
                    except FeatureUnavailable as error:
                        self.repo.attempt(self.name,fixture['fixture_id'],str(error))
                        LOG.warning('Prospective deferred %s: %s',fixture['game_id'],error)
                    except (ValueError,psycopg.Error) as error:
                        if self.connection.closed:raise
                        self.repo.attempt(self.name,fixture['fixture_id'],type(error).__name__)
                        if self.repo.clock()>=fixture['cutoff_at']:
                            self.repo.missing(self.name,fixture,'missed_during_generation')
                        LOG.warning('Prospective seal rejected %s (%s)',fixture['game_id'],type(error).__name__)
                self.repo.observe_results(self.campaign)
                self.repo.recover_results(self.campaign,now)
            now=self.repo.clock();rows=self.repo.snapshot(self.campaign)
            items=[game_item(r,self.campaign,now) for r in rows]
            report=performance(rows,self.campaign,now)
            identity=digest(jsonable(dict(items=items,coverage=report['coverage'],paired=report['paired_differences'])))
            if self.owner and identity!=self.report_hash:
                folder=save_report(report,items,self.report_root);self.report_hash=identity
                LOG.info('Prospective report: %s; matched finals=%s',folder,report['coverage']['validated_paired'])
            future=[r['cutoff_at'] for r in rows if r['status'] is None and r['cutoff_at']>now]
            self.cache=jsonable(dict(items=items,revisions=self.repo.revisions(self.campaign),performance=report,health=dict(status='running' if self.owner else 'following',error=None,
                checked_at=now,campaign=self.name,model_set_id=self.campaign['model_set_id'],next_cutoff=min(future,default=None),
                coverage=report['coverage'])))
            return self.cache
        except Exception as error:
            LOG.warning('Prospective tracker retry (%s)',type(error).__name__)
            self.cache['health']=dict(status='degraded',error=f'Tracking unavailable ({type(error).__name__}); missed cutoffs will not be backfilled',
                checked_at=datetime.now(timezone.utc).isoformat())
            if self.connection is not None:
                self.connection.close();self.connection=None;self.repo=None;self.owner=False
            return self.cache

    async def run(self):
        try:
            while True:
                poll=asyncio.create_task(asyncio.to_thread(self.tick))
                try:await asyncio.shield(poll)
                except asyncio.CancelledError:
                    await poll;raise
                await asyncio.sleep(max(10,int(os.environ.get('NHL_PROSPECTIVE_INTERVAL_SECONDS','30'))))
        finally:await asyncio.to_thread(self.close)

    def close(self):
        if self.connection is not None:self.connection.close()


def main():
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--seal-early',action='store_true',help='Explicit one-off early bootstrap for the current NHL calendar date; separately labelled')
    parser.add_argument('--campaign',default=DEFAULT_CAMPAIGN)
    parser.add_argument('--model-run',type=Path)
    args=parser.parse_args()
    if args.seal_early and not args.once:parser.error('--seal-early is only allowed with --once')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s: %(message)s')
    tracker=ProspectiveTracker(args.campaign,args.model_run)
    try:
        if args.once:
            result=tracker.tick(args.seal_early)
            print(json.dumps(result['health'],indent=2))
            if result['health']['status']=='degraded':raise SystemExit(1)
        else:asyncio.run(tracker.run())
    finally:tracker.close()


if __name__=='__main__':main()
