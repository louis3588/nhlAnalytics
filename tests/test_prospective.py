import copy
import math
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from nhl_analytics.prospective import decision
from nhl_analytics.prospective_features import predict_logistic, observed_features, observed_elo, FeatureUnavailable
from nhl_analytics.pregame import FEATURES
from nhl_analytics.prospective_reports import performance, model_values, paired_interval
from nhl_analytics.prospective_store import ProspectiveRepository, final_sample

NOW=datetime(2026,11,11,17,tzinfo=timezone.utc)


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.f=dict(schedule_state='OK',game_state='FUT',cutoff_at=NOW,game_date=NOW.date())

    def test_window_and_exact_cutoff(self):
        self.assertEqual(decision(self.f,NOW-timedelta(minutes=6))[0],'wait')
        self.assertEqual(decision(self.f,NOW-timedelta(minutes=5)),('generate','standard'))
        self.assertEqual(decision(self.f,NOW),('missing','missed_cutoff'))
        self.assertEqual(decision(self.f,NOW+timedelta(minutes=5),True)[0],'missing')

    def test_early_is_explicit_and_current_nhl_day_only(self):
        self.assertEqual(decision(self.f,NOW-timedelta(hours=2),True),('generate','early'))
        self.f['game_date']+=timedelta(days=1)
        self.assertEqual(decision(self.f,NOW-timedelta(hours=2),True)[0],'wait')

    def test_postponed_and_started_are_missing(self):
        self.f['schedule_state']='PPD'
        self.assertEqual(decision(self.f,NOW-timedelta(hours=1))[1],'postponed_or_unconfirmed')
        self.f.update(schedule_state='OK',game_state='LIVE')
        self.assertEqual(decision(self.f,NOW-timedelta(hours=1))[1],'already_started_or_final')


class PortableInferenceTests(unittest.TestCase):
    def test_missing_indicator_scaling_and_numerics(self):
        model=dict(feature_columns=['a','b'],imputer_statistics=[2,3],missing_indicator_indices=[1],
            scaler_mean=[1,2,0],scaler_scale=[2,2,1],coefficients=[1,2,3],intercept=-1)
        self.assertAlmostEqual(predict_logistic(model,dict(a=5,b=None)),1/(1+math.exp(-5)))
        model['intercept']=10000
        self.assertEqual(predict_logistic(model,dict(a=5,b=None)),1)
        model['intercept']=-10000
        self.assertEqual(predict_logistic(model,dict(a=5,b=None)),0)
        with self.assertRaises(FeatureUnavailable):predict_logistic(model,dict(a=float('nan'),b=0))

    def test_frozen_artifact_matches_existing_numeric_inference(self):
        from nhl_analytics.prediction_store import document
        from nhl_analytics.team_models import predict_saved
        from pathlib import Path
        path=Path('data/team_models/reconstructed/20261111T150925233807Z/models/20262027_logistic_team_context.json')
        if not path.exists():self.skipTest('Local frozen artifact is not checked into git')
        model=document(path)
        rows=[{name:None if i%3==0 else (i-4)/10 for i,name in enumerate(FEATURES)},dict.fromkeys(FEATURES,0)]
        for row,p in zip(rows,predict_saved(model,rows)):self.assertAlmostEqual(predict_logistic(model,row),p,places=13)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.g=dict(game_id=1,season_id=20262027,game_date='2026-11-09',start_time_utc='2026-11-09T23:00:00+00:00',
            home_team_id=1,away_team_id=2,home_score=3,away_score=1,winner_team_id=1,is_final=True,
            result_type='REG',available_at=NOW-timedelta(days=1))
        self.bundle=dict(season_id=20262027,seeded_game_ids=[],rating_history={},
            elo=dict(as_of='2026-11-01T00:00:00+00:00',franchise_ratings={},parameters=dict(elo_home_advantage=25,elo_k=10)),
            logistic=dict(feature_columns=FEATURES,imputer_statistics=[0]*len(FEATURES),missing_indicator_indices=[],
                scaler_mean=[0]*len(FEATURES),scaler_scale=[1]*len(FEATURES),coefficients=[.01]*len(FEATURES),intercept=0))
        self.campaign=dict(bundle=self.bundle,season_id=20262027,registered_at=NOW-timedelta(hours=2))
        self.fixture=dict(game_id=3,season_id=20262027,game_date=NOW.date(),home_team_id=1,away_team_id=2,provider_checked_at=NOW-timedelta(seconds=2))
        self.records=[]
        for team in (1,2):
            for p in range(16):self.records.append(dict(game_id=1,team_id=team,kind='skater',available_at=self.g['available_at'],payload=dict(shots=2)))
            self.records.append(dict(game_id=1,team_id=team,kind='goalie',available_at=self.g['available_at'],payload=dict(shotsAgainst=32,goalsAgainst=1)))

    def run_features(self,games=None,records=None):
        return observed_features(None,self.campaign,self.fixture,NOW,(games or {1:self.g},records if records is not None else self.records,{}))

    def test_future_and_target_game_are_excluded(self):
        baseline=self.run_features()
        future=dict(self.g,game_id=2,available_at=NOW,home_score=100)
        target=dict(self.g,game_id=3,home_score=200)
        changed=self.run_features({1:self.g,2:future,3:target})
        self.assertEqual(baseline[0:4],changed[0:4]);self.assertEqual(changed[4]['source_game_ids'],[1])
        self.assertTrue(changed[4]['target_game_excluded']);self.assertFalse(changed[4]['live_statistics_used'])

    def test_missing_or_late_stats_cannot_be_reconstructed(self):
        rows=copy.deepcopy(self.records);rows[0]['available_at']=NOW
        # Both are genuine observations, but one cannot enter this snapshot.
        rows[1]['available_at']=NOW
        with self.assertRaisesRegex(FeatureUnavailable,'incomplete_known_final'):self.run_features(records=rows)
        self.fixture['provider_checked_at']=NOW
        with self.assertRaisesRegex(FeatureUnavailable,'schedule_not_recently_confirmed'):self.run_features()

    def test_elo_updates_wait_for_observed_result(self):
        before=dict(self.g,available_at=NOW)
        r,_,_=observed_elo(self.bundle,{1:before},NOW,3)
        self.assertEqual(r[1],1500)
        r,_,_=observed_elo(self.bundle,{1:self.g},NOW,3)
        self.assertGreater(r[1],1500)


def sample(state='OFF',score=3):
    raw=dict(id=123,season=20262027,gameState=state,startTimeUTC=(NOW-timedelta(hours=4)).isoformat(),
        homeTeam=dict(id=1,score=score),awayTeam=dict(id=2,score=1),
        periodDescriptor=dict(number=3,periodType='REG'),clock=dict(secondsRemaining=0))
    return dict(game_id=123,checked_at=NOW,content_sha256='raw',payload=dict(boxscore=copy.deepcopy(raw),play_by_play=copy.deepcopy(raw),
        view=dict(season_id=20262027,start_time_utc=raw['startTimeUTC'],home=dict(id=1),away=dict(id=2))))


class ResultTests(unittest.TestCase):
    def test_live_not_final_and_two_views_must_agree(self):
        self.assertEqual(final_sample(sample('LIVE'))['status'],'nonfinal')
        self.assertEqual(final_sample(sample())['status'],'candidate_final')
        row=sample();row['payload']['play_by_play']['homeTeam']['score']=4
        self.assertEqual(final_sample(row)['status'],'conflict')

    def test_wrong_game_and_impossible_result(self):
        row=sample();row['payload']['boxscore']['id']=999
        self.assertEqual(final_sample(row)['status'],'conflict')
        self.assertEqual(final_sample(sample(score=1))['status'],'conflict')
        row=sample();row['checked_at']=NOW-timedelta(days=1)
        self.assertEqual(final_sample(row)['status'],'conflict')

    def test_correction_changes_result_identity_not_forecasts(self):
        self.assertNotEqual(final_sample(sample())['hash'],final_sample(sample(score=4))['hash'])
        row=sample();row['content_sha256']='new-player-stat-correction'
        self.assertEqual(final_sample(sample())['hash'],final_sample(row)['hash'])

    def test_sudden_death_and_shootout_need_stopped_clock_not_zero(self):
        row=sample(score=2)
        for kind,number in [('OT',4),('SO',5)]:
            for key in ('boxscore','play_by_play'):
                row['payload'][key].update(periodDescriptor=dict(number=number,periodType=kind),
                    clock=dict(secondsRemaining=226,timeRemaining='03:46',running=False))
            self.assertEqual(final_sample(row)['status'],'candidate_final')
            row['payload']['play_by_play']['clock']['running']=True
            self.assertEqual(final_sample(row)['status'],'conflict')


class ScoringTests(unittest.TestCase):
    def test_shared_cohort_and_numerical_scores(self):
        row=dict(valid_for_scoring=True,game_id=1,season_id=20262027,game_date=NOW.date(),scheduled_start=NOW,
            cutoff_at=NOW-timedelta(hours=1),home_team_id=1,away_team_id=2,winner_team_id=1,
            elo_home_probability=.6,logistic_home_probability=.7,status='sealed',timing='early',reason=None,outcome_status='validated')
        campaign=dict(campaign='x',model_set_id='x',registered_at=NOW,tracking_date=NOW.date(),elo_version='e',logistic_version='l',
            bundle=dict(reference_as_of='historic',references=[]))
        excluded=dict(row,game_id=2,valid_for_scoring=False,status='missing',reason='missed_cutoff')
        report=performance([row,excluded],campaign,NOW)
        scores=[r for r in report['metrics'] if r['dimension']=='all' and r['timing']=='all']
        self.assertEqual([r['n'] for r in scores],[1,1])
        self.assertAlmostEqual(scores[0]['brier'],.16)
        self.assertAlmostEqual(scores[1]['log_loss'],-math.log(.7))
        self.assertEqual(report['coverage']['missing'],1)
        self.assertFalse(report['paired_differences'][0]['available'])
        self.assertIsNone(model_values(dict(row,valid_for_scoring=False),campaign)[0]['brier'])
        self.assertIsNone(paired_interval([])['brier_delta'])


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for database safeguards')
class DatabaseGuardTests(unittest.TestCase):
    def setUp(self):
        from nhl_analytics.database import connect,migrate
        self.c=connect();self.addCleanup(self.c.close);migrate(self.c)
        tx=self.c.transaction(force_rollback=True);tx.__enter__();self.addCleanup(tx.__exit__,None,None,None)
        self.r=ProspectiveRepository(self.c);self.name='test_'+uuid4().hex
        self.campaign=self.r.register(self.name,dict(season_id=20262027,elo_version='e',logistic_version='l',policy={},references=[]))
        g=self.r.rows('SELECT * FROM analytics.games WHERE season_id=20262027 ORDER BY start_time_utc DESC LIMIT 1')[0]
        self.now=self.r.clock()
        self.f=self.r.observe_fixture(dict(game_id=g['game_id'],season_id=g['season_id'],home_team_id=g['home_team_id'],
            away_team_id=g['away_team_id'],game_date=self.now.date(),scheduled_start=self.now+timedelta(hours=3),
            schedule_state='OK',game_state='FUT',provider_checked_at=self.now-timedelta(seconds=1)))

    def seal(self,fixture=None,**kwargs):
        values=dict(features={'x':1},elo=.6,logistic=.55,feature_as_of=self.r.clock(),latest=self.now-timedelta(seconds=1),provenance={},timing='early')
        values.update(kwargs)
        return self.r.seal(self.name,fixture or self.f,**values)

    def test_seal_idempotent_immutable_and_database_stamped(self):
        first=self.seal();second=self.seal(elo=.9)
        self.assertEqual(first['batch_id'],second['batch_id']);self.assertEqual(second['elo_home_probability'],.6)
        self.assertGreaterEqual(first['recorded_at'],first['feature_as_of'])
        for statement in ['UPDATE analytics.prospective_batches SET elo_home_probability=.9 WHERE batch_id=%s',
                          'DELETE FROM analytics.prospective_batches WHERE batch_id=%s']:
            with self.assertRaises(psycopg.Error),self.c.transaction():self.c.execute(statement,(first['batch_id'],))
        with self.assertRaises(psycopg.Error),self.c.transaction():self.c.execute('TRUNCATE analytics.prospective_batches')

    def test_probability_source_and_timing_rejection(self):
        for kwargs in [dict(elo=1.1),dict(logistic=float('nan')),dict(latest=self.now+timedelta(days=1)),dict(timing='standard')]:
            with self.assertRaises(psycopg.Error):self.seal(**kwargs)

    def test_late_cannot_be_backdated(self):
        late=self.r.observe_fixture(dict(self.f,scheduled_start=self.now+timedelta(minutes=30)))
        with self.assertRaises(psycopg.Error):self.seal(late,feature_as_of=self.now-timedelta(hours=2))
        missing=self.r.missing(self.name,late,'missed_cutoff')
        self.assertEqual(missing['status'],'missing')

    def test_reschedule_appends_and_wrong_game_head_cannot_seal(self):
        old=self.seal()
        changed=self.r.observe_fixture(dict(self.f,scheduled_start=self.f['scheduled_start']+timedelta(hours=1),provider_checked_at=self.r.clock()))
        self.assertNotEqual(changed['fixture_id'],self.f['fixture_id'])
        self.assertEqual(self.seal()['batch_id'],old['batch_id'])
        self.assertEqual(self.r.batch(self.name,self.f['fixture_id'])['batch_id'],old['batch_id'])
        self.assertNotEqual(self.seal(changed)['batch_id'],old['batch_id'])

    def test_new_live_state_after_discovery_blocks_sealing(self):
        from nhl_analytics.prospective_store import jsonb
        view=dict(game_state='LIVE',schedule_state='OK',start_time_utc=self.f['scheduled_start'].isoformat(),
            home={'id':self.f['home_team_id']},away={'id':self.f['away_team_id']})
        at=self.r.clock()
        self.c.execute('''INSERT INTO analytics.live_games VALUES (%s,%s,'LIVE',%s,%s,'test',%s)
            ON CONFLICT(game_id) DO UPDATE SET checked_at=EXCLUDED.checked_at,payload=EXCLUDED.payload''',
            (self.f['game_id'],self.now.date(),at,at,jsonb({'view':view})))
        with self.assertRaisesRegex(ValueError,'Latest provider'):self.seal()

    def test_final_confirmation_and_correction_append_revisions(self):
        from nhl_analytics.prospective_store import jsonb
        from nhl_analytics.live_store import digest
        raw=sample();gid=self.f['game_id']
        start=(self.now-timedelta(hours=5)).isoformat()
        for key in ('boxscore','play_by_play'):
            v=raw['payload'][key];v.update(id=gid,startTimeUTC=start)
            v['homeTeam']['id']=self.f['home_team_id'];v['awayTeam']['id']=self.f['away_team_id']
        raw['payload']['view'].update(start_time_utc=start,home={'id':self.f['home_team_id']},away={'id':self.f['away_team_id']})
        def check(minutes,home_score=3):
            for key in ('boxscore','play_by_play'):raw['payload'][key]['homeTeam']['score']=home_score
            at=self.now-timedelta(minutes=minutes)
            self.c.execute('''INSERT INTO analytics.live_games VALUES (%s,%s,'OFF',%s,%s,%s,%s)
                ON CONFLICT(game_id) DO UPDATE SET checked_at=EXCLUDED.checked_at,payload=EXCLUDED.payload,content_sha256=EXCLUDED.content_sha256''',
                (gid,self.now.date(),at,at,digest(raw['payload']),jsonb(raw['payload'])))
            self.r.observe_results(self.campaign)
            return self.c.execute('SELECT count(*) FROM analytics.prospective_validated_results WHERE game_id=%s',(gid,)).fetchone()[0]
        self.assertEqual(check(40),0)
        self.assertEqual(check(29),1)
        self.assertEqual(check(20,4),1)
        self.assertEqual(check(9,4),2)
        # Restart/replay of the same provider check never duplicates a result.
        self.assertEqual(check(9,4),2)


if __name__=='__main__':unittest.main()
