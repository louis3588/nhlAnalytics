import asyncio
import copy
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psycopg
import requests

from nhl_analytics.database import connect, migrate
from nhl_analytics.live import BackoffError, LiveCollector, LiveHub, cadence, normalize, valid_game_id, poll_priority
from nhl_analytics.live_store import LiveRepository
from test_api import API_AVAILABLE, fixture as api_fixture


def fixture():
    box = dict(id=2026029998, season=20262027, gameType=2, gameDate='2026-11-11', startTimeUTC='2026-11-12T00:00:00Z',
        gameState='LIVE', gameScheduleState='OK', venue={'default':'Test Arena'},
        homeTeam=dict(id=1,abbrev='NJD',score=1,sog=4,commonName={'default':'Devils'}),
        awayTeam=dict(id=2,abbrev='NYI',score=0,sog=3,commonName={'default':'Islanders'}),
        playerByGameStats={'homeTeam':{'forwards':[dict(playerId=999999993,name={'default':'Test Player'},position='C',
            goals=1,assists=0,points=1,sog=2,pim=0,hits=1,blockedShots=0,toi='03:00')]},'awayTeam':{}},
        periodDescriptor={'number':1,'periodType':'REG'},clock={'timeRemaining':'12:00','running':True,'inIntermission':False})
    pbp = {k:copy.deepcopy(v) for k,v in box.items() if k!='playerByGameStats'}
    pbp.update(plays=[dict(eventId=10,sortOrder=1,typeDescKey='goal',periodDescriptor={'number':1,'periodType':'REG'},
        timeInPeriod='08:00',details=dict(scoringPlayerId=999999993,eventOwnerTeamId=1,homeScore=1,awayScore=0))],
        rosterSpots=[dict(playerId=999999993,firstName={'default':'Test'},lastName={'default':'Player'})])
    return box,pbp


class LiveValidationTests(unittest.TestCase):
    def test_missing_pregame_stats_are_not_zeroes(self):
        box,pbp=fixture();box.pop('playerByGameStats');pbp['plays']=[];box['gameState']=pbp['gameState']='FUT'
        box['homeTeam'].pop('score');pbp['homeTeam'].pop('score')
        view=normalize(box,pbp)
        self.assertFalse(view['stats_available']);self.assertEqual(view['players']['home'],[])
        self.assertIsNone(view['home']['score'])
        box['playerByGameStats']=None;pbp['rosterSpots']=None
        self.assertFalse(normalize(box,pbp)['stats_available'])

    def test_identity_and_complete_event_list_validation(self):
        box,pbp=fixture();pbp['id']+=1
        with self.assertRaisesRegex(ValueError,'identity'):normalize(box,pbp)
        box,pbp=fixture();pbp.pop('plays')
        with self.assertRaisesRegex(ValueError,'complete play'):normalize(box,pbp)
        box,pbp=fixture();pbp['plays']*=2
        with self.assertRaisesRegex(ValueError,'duplicate'):normalize(box,pbp)
        self.assertFalse(valid_game_id(42));self.assertTrue(valid_game_id(2026029998))

    def test_cadence_slows_down_outside_active_games(self):
        now=datetime(2026,11,11,12,tzinfo=timezone.utc)
        self.assertEqual(cadence('LIVE','2026-11-11T11:00:00Z',now),20)
        self.assertEqual(cadence('FUT','2026-11-12T00:00:00Z',now),300)
        self.assertEqual(cadence('FUT','2026-11-11T12:10:00Z',now),60)
        self.assertEqual(cadence('OFF','2026-11-10T00:00:00Z',now),3600)

    def test_rate_limit_honors_retry_after(self):
        session=Mock();session.get.return_value=SimpleNamespace(status_code=429,headers={'Retry-After':'120'})
        collector=LiveCollector(session=session)
        self.addCleanup(collector.close)
        start=time.monotonic()
        with self.assertRaises(BackoffError):collector.get('/score/2026-11-11')
        self.assertGreaterEqual(collector.network_due,start+119)
        with self.assertRaises(BackoffError):collector.get('/score/2026-11-11')
        self.assertEqual(session.get.call_count,1)

    def test_database_outage_does_not_fetch_unpersistable_source_data(self):
        session=Mock();collector=LiveCollector(connection_factory=Mock(side_effect=psycopg.OperationalError('offline')),session=session)
        self.addCleanup(collector.close)
        result=collector.tick()
        self.assertEqual(result['health']['status'],'degraded')
        collector.tick()
        self.assertEqual(collector.connection_factory.call_count,1)
        session.get.assert_not_called()

    def test_shared_admission_and_bounded_queue(self):
        hub=LiveHub();self.addCleanup(hub.collector.close)
        for _ in range(20):hub.request(2026029998)
        self.assertEqual(len(hub.pending),1)
        for gid in range(2026029900,2026029915):hub.request(gid)
        with self.assertRaises(OverflowError):hub.request(2026029916)

    def test_shared_polling_respects_due_times_not_viewer_count(self):
        collector=LiveCollector(session=Mock())
        self.addCleanup(collector.close)
        box,pbp=fixture();gid=box['id']
        repository=Mock();repository.requested.return_value=[gid]
        repository.save.return_value={'game':normalize(box,pbp)}
        collector.repository=repository;collector.owner=True
        collector.ensure_database=Mock()
        collector.get=Mock(side_effect=[box,pbp])
        collector.tick(discover=False)
        collector.tick(discover=False)
        self.assertEqual(collector.get.call_count,2)
        self.assertEqual(repository.save.call_count,1)

    def test_cutoff_confirmation_precedes_live_details(self):
        now=datetime(2026,11,12,0,55,tzinfo=timezone.utc)
        due=poll_priority(dict(gameState='FUT',startTimeUTC='2026-11-12T02:00:00Z'),False,0,now)
        live=poll_priority(dict(gameState='LIVE'),False,0,now)
        self.assertLess(due,live)
        past=poll_priority(dict(gameState='FUT',startTimeUTC='2026-11-12T01:30:00Z'),False,0,now)
        self.assertGreater(past,live)

    def test_one_timeout_does_not_block_other_games(self):
        import requests
        collector=LiveCollector(session=Mock());self.addCleanup(collector.close)
        box,pbp=fixture();gid=box['id'];slow=gid-1
        repository=Mock();repository.requested.return_value=[slow,gid]
        repository.save.return_value={'game':normalize(box,pbp)}
        collector.repository=repository;collector.owner=True;collector.ensure_database=Mock()
        collector.due[slow]=-1;collector.due[gid]=0
        collector.get=Mock(side_effect=[requests.ReadTimeout(),box,pbp])
        result=collector.tick(discover=False)
        self.assertEqual(repository.save.call_count,1)
        self.assertEqual(collector.network_due,0)
        self.assertEqual(result['health']['status'],'degraded')


class LiveStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_replace_reconnect_and_disconnect_cleanup(self):
        collector=LiveCollector();hub=LiveHub(collector)
        box,pbp=fixture();view=normalize(box,pbp)
        hub.health={'status':'running','error':None,'active_poll_seconds':20}
        hub.states[view['game_id']]=dict(game=view,checked_at=datetime.now(timezone.utc).isoformat(),revision='one')
        request=SimpleNamespace(is_disconnected=Mock(side_effect=lambda:asyncio.sleep(0,result=False)))
        stream=hub.events(request,view['game_id'])
        self.assertIn('retry:',await anext(stream))
        self.assertIn('event: game',await anext(stream));self.assertEqual(hub.clients,1)
        waiter=asyncio.create_task(anext(stream));await asyncio.sleep(0)
        corrected=copy.deepcopy(view);corrected['home']['score']=2
        hub.states[view['game_id']]=dict(game=corrected,checked_at=datetime.now(timezone.utc).isoformat(),revision='two')
        async with hub.condition:hub.condition.notify_all()
        update=await asyncio.wait_for(waiter,1)
        self.assertIn('"score":2',update)
        await stream.aclose();self.assertEqual(hub.clients,0)
        reconnect=hub.events(request,view['game_id'])
        await anext(reconnect);self.assertIn('"score":2',await anext(reconnect))
        await reconnect.aclose();collector.close()


@unittest.skipUnless(API_AVAILABLE,'Install api dependencies for HTTP checks')
class LiveAPITests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from nhl_analytics.api import create_app
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        root=Path(temporary.name).resolve();run=api_fixture(root)
        self.app=create_app(run,root/'cache',live_enabled=False)
        self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def test_disabled_collector_does_not_break_saved_forecasts(self):
        self.assertEqual(self.client.get('/api/live/games').status_code,503)
        self.assertEqual(self.client.get('/api/predictions/players').status_code,200)

    def test_live_requests_read_shared_cache_and_coalesce_admission(self):
        hub=LiveHub();self.addCleanup(hub.collector.close);self.app.state.live=hub
        box,pbp=fixture();view=normalize(box,pbp)
        hub.states[view['game_id']]=dict(game=view,checked_at=datetime.now(timezone.utc).isoformat(),revision='one')
        hub.health={'status':'running','error':None,'active_poll_seconds':20}
        with patch.object(hub.collector.session,'get',side_effect=AssertionError('Request fetched NHL')):
            for _ in range(3):
                self.assertEqual(self.client.get('/api/live/games/2026029998').json()['game']['home']['score'],1)
            self.assertEqual(self.client.get('/api/live/games').json()['items'][0]['game_id'],2026029998)
            for _ in range(3):
                self.assertEqual(self.client.get('/api/live/games/2026029999').json()['status'],'loading')
        self.assertEqual(len(hub.pending),1)
        self.assertEqual(self.client.get('/api/live/games/42').status_code,422)
        hub.states[2026029999]={'not_found':True,'game_id':2026029999}
        self.assertEqual(self.client.get('/api/live/games/2026029999').status_code,404)


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for Timescale integration tests')
class LiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection=connect();self.addCleanup(self.connection.close);migrate(self.connection)
        tx=self.connection.transaction(force_rollback=True);tx.__enter__();self.addCleanup(tx.__exit__,None,None,None)
        self.repository=LiveRepository(self.connection)
        self.now=datetime(2030,1,1,tzinfo=timezone.utc)

    def save(self,box,pbp,seconds=0):
        return self.repository.save(dict(view=normalize(box,pbp),boxscore=box,play_by_play=pbp),self.now+timedelta(seconds=seconds))

    def count(self,table):
        return self.connection.execute('SELECT count(*) FROM analytics.'+table+' WHERE game_id=2026029998').fetchone()[0]

    def test_unchanged_replays_advance_check_time_without_duplicate_snapshots(self):
        box,pbp=fixture();first=self.save(box,pbp);second=self.save(box,pbp,20)
        self.assertEqual(self.count('live_game_snapshots'),1)
        self.assertEqual(self.count('live_game_events'),1)
        self.assertEqual(self.count('live_game_event_revisions'),1)
        self.assertEqual(first['observed_at'],second['observed_at'])
        self.assertNotEqual(first['checked_at'],second['checked_at'])
        self.assertEqual(self.connection.execute('SELECT last_seen_at FROM analytics.live_game_events WHERE game_id=2026029998').fetchone()[0],self.now+timedelta(seconds=20))

    def test_event_correction_deletion_and_reinstatement_preserve_revisions(self):
        box,pbp=fixture()
        pbp['plays'].append(dict(eventId=9,sortOrder=0,typeDescKey='period-start',periodDescriptor={'number':1,'periodType':'REG'},timeInPeriod='00:00'))
        self.save(box,pbp)
        pbp['plays'][0]['details']['scoringPlayerId']=999999992
        self.save(box,pbp,20)
        self.assertEqual(self.count('live_game_events'),2)
        self.assertEqual(self.count('live_game_event_revisions'),3)
        original=pbp['plays'];pbp['plays']=[original[1]];self.save(box,pbp,40)
        row=self.connection.execute('SELECT is_deleted FROM analytics.live_game_events WHERE game_id=2026029998 AND event_id=10').fetchone()
        self.assertTrue(row[0]);self.assertEqual(self.count('live_game_event_revisions'),4)
        pbp['plays']=original;self.save(box,pbp,60)
        self.assertEqual(self.count('live_game_event_revisions'),5)
        self.assertFalse(self.connection.execute('SELECT is_deleted FROM analytics.live_game_events WHERE game_id=2026029998 AND event_id=10').fetchone()[0])

    def test_empty_midgame_feed_cannot_erase_event_history(self):
        box,pbp=fixture();self.save(box,pbp)
        pbp['plays']=[]
        with self.assertRaisesRegex(ValueError,'Empty play feed'):self.save(box,pbp,20)
        self.assertEqual(self.count('live_game_snapshots'),1)
        self.assertFalse(self.connection.execute('SELECT is_deleted FROM analytics.live_game_events WHERE game_id=2026029998').fetchone()[0])

    def test_failed_player_validation_rolls_back_state_and_history(self):
        box,pbp=fixture();self.save(box,pbp)
        box['playerByGameStats']['homeTeam']['forwards'][0]['playerId']=-1
        with self.assertRaises(psycopg.Error):self.save(box,pbp,20)
        self.assertEqual(self.count('live_game_snapshots'),1)
        self.assertEqual(self.count('live_game_event_revisions'),1)

    def test_older_observations_are_rejected(self):
        box,pbp=fixture();self.save(box,pbp,20)
        with self.assertRaisesRegex(ValueError,'backdate'):self.save(box,pbp)

    def test_preseason_game_link_does_not_change_regular_season_training_grain(self):
        box,pbp=fixture()
        box['id']=pbp['id']=2026019998;box['gameType']=pbp['gameType']=1
        self.save(box,pbp)
        self.assertEqual(self.connection.execute('SELECT game_type_id FROM analytics.games WHERE game_id=2026019998').fetchone()[0],1)


if __name__=='__main__':unittest.main()
