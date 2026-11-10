import copy
import os
import unittest

import psycopg

from nhl_analytics.database import connect, migrate
from nhl_analytics.schedules import fetch_season, load_season, normalize_game
from nhl_analytics.game_logs import load_game_log


def fixture():
    return dict(schemaVersion=1,source='NHL',seasonId=20252026,gameTypes=[2,3],
        retrievedAt='2030-01-01T00:00:00Z',teams=[
            dict(id=999995,seasonId=20252026,abbrev='TST',name={'default':'Test Home'}),
            dict(id=999996,seasonId=20252026,abbrev='ZZZ',name={'default':'Test Away'})],
        games=[dict(id=2025029997,season=20252026,gameType=2,gameDate='2026-04-01',
            homeTeam=dict(id=999995,abbrev='TST'),awayTeam=dict(id=999996,abbrev='ZZZ'),
            gameState='FUT',gameScheduleState='OK',startTimeUTC='2026-04-02T00:00:00Z',
            venue={'default':'Test Arena'},venueTimezone='America/New_York',neutralSite=False)])


class ScheduleValidationTests(unittest.TestCase):
    def test_future_game_has_no_invented_scores_or_winner(self):
        core = normalize_game(fixture()['games'][0],20252026,{2,3})
        self.assertFalse(core['is_final'])
        self.assertIsNone(core['home_score'])
        self.assertIsNone(core['winner_team_id'])
        self.assertIsNone(core['result_type'])

    def test_final_regulation_overtime_and_shootout(self):
        for period in ('REG','OT','SO'):
            game = fixture()['games'][0]
            game.update(gameState='OFF',gameOutcome=dict(lastPeriodType=period))
            game['homeTeam']['score']=2
            game['awayTeam']['score']=3
            core = normalize_game(game,20252026,{2})
            self.assertEqual((core['winner_team_id'],core['result_type']),(999996,period))

    def test_final_tie_and_wrong_season_fail(self):
        game = fixture()['games'][0]
        game.update(gameState='FINAL',gameOutcome={'lastPeriodType':'REG'})
        game['homeTeam']['score']=0
        game['awayTeam']['score']=0
        with self.assertRaisesRegex(ValueError,'unequal'):
            normalize_game(game,20252026,{2})
        with self.assertRaisesRegex(ValueError,'season'):
            normalize_game(game,20262027,{2})

    def test_club_schedule_duplicates_merge_and_both_clubs_are_checked(self):
        envelope = fixture()
        game = envelope['games'][0]
        second = copy.deepcopy(game)
        second['ticketsLink']='https://example.invalid/different-club-link'
        class Client:
            def __init__(self):
                self.responses=iter([{'teams':envelope['teams']},{'games':[game]},{'games':[second]}])
            def get(self,url):
                return next(self.responses)
        result = fetch_season(Client(),20252026,{2,3})
        self.assertEqual(len(result['games']),1)
        self.assertIn('ticketsLink',result['games'][0])

    def test_missing_opponent_club_game_is_rejected(self):
        envelope = fixture()
        class Client:
            def __init__(self):
                self.responses=iter([{'teams':envelope['teams']},{'games':envelope['games']},{'games':[]}])
            def get(self,url):
                return next(self.responses)
        with self.assertRaises(ValueError):
            fetch_season(Client(),20252026,{2})


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for live database tests')
class ScheduleDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection=connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        tx=self.connection.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)

    def observation_count(self):
        return self.connection.execute('SELECT count(*) FROM analytics.game_schedule_observations WHERE game_id=2025029997').fetchone()[0]

    def test_rerun_result_updates_and_older_cache_protection(self):
        envelope=fixture()
        self.assertEqual(load_season(self.connection,envelope,'fixture.json')['changed'],1)
        self.assertEqual(load_season(self.connection,envelope,'fixture.json')['unchanged'],1)
        self.assertEqual(self.observation_count(),1)
        envelope['retrievedAt']='2030-01-02T00:00:00Z'
        game=envelope['games'][0]
        game.update(gameState='OFF',gameOutcome={'lastPeriodType':'OT'})
        game['homeTeam']['score']=4
        game['awayTeam']['score']=3
        load_season(self.connection,envelope,'fixture.json')
        self.assertEqual(self.observation_count(),2)
        self.assertEqual(load_season(self.connection,fixture(),'old.json')['older_skipped'],1)
        row=self.connection.execute('SELECT home_score,away_score,winner_team_id,is_final,result_type FROM analytics.games WHERE game_id=2025029997').fetchone()
        self.assertEqual(row,(4,3,999995,True,'OT'))

    def test_rescheduling_preserves_history_and_game_id(self):
        envelope=fixture()
        load_season(self.connection,envelope,'fixture.json')
        envelope['retrievedAt']='2030-01-02T00:00:00Z'
        envelope['games'][0].update(gameDate='2026-04-02',startTimeUTC='2026-04-03T00:00:00Z')
        load_season(self.connection,envelope,'new.json')
        self.assertEqual(self.observation_count(),2)
        self.assertEqual(self.connection.execute('SELECT game_date::text FROM analytics.games WHERE game_id=2025029997').fetchone()[0],'2026-04-02')

    def test_modified_snapshot_at_same_time_is_rejected(self):
        envelope=fixture()
        load_season(self.connection,envelope,'fixture.json')
        envelope['games'][0]['venue']['default']='Another Arena'
        with self.assertRaisesRegex(ValueError,'modified'):
            load_season(self.connection,envelope,'fixture.json')

    def test_database_failure_rolls_back_import(self):
        envelope=fixture()
        envelope['teams'][0]['name']['default']=None
        before=self.connection.execute('SELECT count(*) FROM analytics.schedule_imports').fetchone()[0]
        with self.assertRaises(psycopg.errors.NotNullViolation):
            load_season(self.connection,envelope,'fixture.json')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.schedule_imports').fetchone()[0],before)
        self.assertIsNone(self.connection.execute('SELECT 1 FROM analytics.games WHERE game_id=2025029997').fetchone())

    def test_player_game_updates_preserve_schedule_results_and_independent_clocks(self):
        envelope=fixture()
        load_season(self.connection,envelope,'fixture.json')
        log=dict(playerId=999999994,seasonId=20252026,gameTypeId=2,kind='skater',
            expectedGames=1,retrievedAt='2030-01-03T00:00:00Z',url='https://example.invalid',
            payload=dict(seasonId=20252026,gameTypeId=2,gameLog=[dict(gameId=2025029997,
                gameDate='2026-04-01',teamAbbrev='TST',opponentAbbrev='ZZZ',homeRoadFlag='H',
                goals=0,assists=0,points=0,toi='18:02')]))
        load_game_log(self.connection,log,'player.json')
        envelope['retrievedAt']='2030-01-02T00:00:00Z'
        game=envelope['games'][0]
        game.update(gameState='OFF',gameOutcome={'lastPeriodType':'REG'})
        game['homeTeam']['score']=4
        game['awayTeam']['score']=3
        self.assertEqual(load_season(self.connection,envelope,'result.json')['changed'],1)
        log['retrievedAt']='2030-01-04T00:00:00Z'
        log['payload']['gameLog'][0].update(goals=1,points=1)
        load_game_log(self.connection,log,'player-new.json')
        self.assertEqual(self.connection.execute('SELECT home_score,away_score,is_final FROM analytics.games WHERE game_id=2025029997').fetchone(),(4,3,True))
