import copy
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg

from nhl_analytics.database import connect, migrate
from nhl_analytics.game_logs import fetch_report_fallback, load_game_log, run_import, toi_seconds, validate_log
from nhl_analytics.ingest import write_json


def fixture(kind='skater'):
    row = dict(gameId=2025029998,gameDate='2026-04-01',teamAbbrev='TST',opponentAbbrev='ZZZ',
               homeRoadFlag='H',goals=1,assists=2,points=3,plusMinus=-1,pim=0,shots=3,shifts=20,toi='18:02')
    if kind == 'goalie':
        row.update(goals=0,assists=0,shotsAgainst=30,goalsAgainst=2,savePctg=28/30,gamesStarted=1,decision='W')
    return dict(playerId=999999997,seasonId=20252026,gameTypeId=2,kind=kind,fullName='Log Fixture',
                expectedGames=1,retrievedAt='2030-01-01T00:00:00+00:00',url='https://example.invalid/fixture',
                payload=dict(seasonId=20252026,gameTypeId=2,gameLog=[row]))


class GameLogValidationTests(unittest.TestCase):
    def test_ice_time(self):
        self.assertEqual(toi_seconds('18:02'),1082)
        self.assertEqual(toi_seconds('65:00'),3900)
        self.assertIsNone(toi_seconds(None))
        for invalid in ('18:60','-1:20','invalid'):
            with self.assertRaises(ValueError):
                toi_seconds(invalid)

    def test_empty_incomplete_response_is_rejected(self):
        envelope = fixture()
        envelope['expectedGames'] = 2
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            validate_log(envelope)

    def test_duplicate_games_are_rejected(self):
        envelope = fixture()
        envelope['payload']['gameLog'] *= 2
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            validate_log(envelope)

    def test_season_and_game_type_must_match(self):
        envelope = fixture()
        envelope['payload']['gameLog'][0]['gameId'] = 2026029998
        with self.assertRaisesRegex(ValueError,'disagrees'):
            validate_log(envelope)

    def test_impossible_goalie_counts_are_rejected(self):
        envelope = fixture('goalie')
        envelope['payload']['gameLog'][0]['goalsAgainst'] = 31
        with self.assertRaisesRegex(ValueError,'exceeds'):
            validate_log(envelope)

    def test_dropdown_metadata_does_not_create_false_revisions(self):
        first = fixture()
        second = copy.deepcopy(first)
        second['payload']['playerStatsSeasons'] = [20262027,20252026]
        self.assertEqual(validate_log(first)[1],validate_log(second)[1])


class GameLogResumeTests(unittest.TestCase):
    def test_cached_responses_resume_without_network_and_isolate_failed_player(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.work/tmp') as directory:
            run = Path(directory)
            first = fixture()
            second = fixture()
            second['playerId'] = 999999996
            plan = [first,second]
            write_json(run/'plan.json',{'participants':plan})
            write_json(run/'players/20252026/skater/999999997.json',first)
            path = run/'players/20252026/skater/999999996.json'
            path.write_text('not valid JSON',encoding='utf-8')
            with patch('nhl_analytics.game_logs.Client') as client, \
                 patch('nhl_analytics.game_logs.discover') as discovery, \
                 patch('nhl_analytics.game_logs.load_game_log',return_value='unchanged') as loader:
                failures = run_import(None,run,[20262027],2,resume=True,workers=2)
                discovery.assert_not_called()
                client.return_value.get.assert_not_called()
                self.assertEqual(loader.call_count,1)
                self.assertEqual(failures[0]['playerId'],999999996)
                self.assertEqual(json.loads((run/'manifest.json').read_text())['status'],'failed')


class GameLogFallbackTests(unittest.TestCase):
    def test_official_skater_reports_preserve_metrics_and_raw_rows(self):
        participant = fixture()
        summary = dict(playerId=999999997,gameId=2025029998,gameDate='2026-04-01',teamAbbrev='TST',
                       opponentTeamAbbrev='ZZZ',homeRoad='R',goals=1,assists=2,points=3,plusMinus=-1,
                       ppGoals=1,ppPoints=2,shGoals=0,shPoints=0,penaltyMinutes=2,shots=4,timeOnIcePerGame=169.0)
        ice = dict(playerId=999999997,gameId=2025029998,shifts=4)
        client = Mock()
        client.get.side_effect = [dict(data=[summary],total=1),dict(data=[ice],total=1)]
        envelope = fetch_report_fallback(client,participant,'Incomplete log','https://example.invalid')
        record = envelope['payload']['gameLog'][0]
        self.assertEqual((record['toi'],record['shifts'],record['powerPlayPoints']),('2:49',4,2))
        self.assertEqual(envelope['payload']['summaryGameRows'],[summary])
        self.assertIn('isGame=true',envelope['url'])
        self.assertEqual(len(validate_log(envelope)[0]),1)

    def test_incomplete_fallback_is_still_rejected(self):
        participant = fixture()
        client = Mock()
        client.get.side_effect = [dict(data=[],total=0),dict(data=[],total=0)]
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            fetch_report_fallback(client,participant,'Incomplete log','https://example.invalid')


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS') == '1','Set NHL_DB_TESTS=1 for live database tests')
class GameLogDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection = connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        tx = self.connection.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)

    def count(self,table):
        return self.connection.execute('SELECT count(*) FROM analytics.'+table+' WHERE player_id=999999997').fetchone()[0]

    def test_rerun_and_correction_preserve_unique_canonical_rows(self):
        envelope = fixture()
        self.assertEqual(load_game_log(self.connection,envelope,'fixture.json'),'loaded')
        self.assertEqual(load_game_log(self.connection,envelope,'fixture.json'),'unchanged')
        self.assertEqual(self.count('skater_game_stats'),1)
        self.assertEqual(self.count('player_game_observations'),1)
        envelope['retrievedAt'] = '2030-01-02T00:00:00Z'
        envelope['payload']['gameLog'][0].update(goals=2,points=4)
        self.assertEqual(load_game_log(self.connection,envelope,'fixture.json'),'loaded')
        self.assertEqual(self.count('skater_game_stats'),1)
        self.assertEqual(self.count('player_game_observations'),2)
        self.assertEqual(self.connection.execute('SELECT goals,toi_seconds FROM analytics.skater_game_stats WHERE player_id=999999997').fetchone(),(2,1082))
        self.assertEqual(load_game_log(self.connection,fixture(),'fixture.json'),'older-cache-skipped')

    def test_goalie_saves_and_game_link(self):
        load_game_log(self.connection,fixture('goalie'),'fixture.json')
        self.assertEqual(self.connection.execute('''SELECT s.saves,s.toi_seconds,g.home_team_abbrev
            FROM analytics.goalie_game_stats s JOIN analytics.games g USING(game_id)
            WHERE player_id=999999997''').fetchone(),(28,1082,'TST'))

    def test_database_failure_rolls_back_whole_player_season(self):
        envelope = fixture()
        envelope['payload']['gameLog'][0]['shots'] = -1
        with self.assertRaises(psycopg.errors.CheckViolation):
            load_game_log(self.connection,envelope,'fixture.json')
        self.assertEqual(self.count('players'),0)
        self.assertEqual(self.count('game_log_sources'),0)
        self.assertIsNone(self.connection.execute('SELECT 1 FROM analytics.games WHERE game_id=2025029998').fetchone())

    def test_retracted_game_keeps_history(self):
        envelope = fixture()
        extra = {**envelope['payload']['gameLog'][0],'gameId':2025029999}
        envelope['payload']['gameLog'].append(extra)
        envelope['expectedGames'] = 2
        load_game_log(self.connection,envelope,'fixture.json')
        newer = fixture()
        newer['retrievedAt'] = '2030-01-02T00:00:00Z'
        load_game_log(self.connection,newer,'fixture.json')
        self.assertEqual(self.count('skater_game_stats'),1)
        self.assertEqual(self.count('player_game_observations'),3)

    def test_traded_player_and_road_game_keep_historical_teams(self):
        envelope = fixture()
        envelope['payload']['gameLog'].append({**envelope['payload']['gameLog'][0],
            'gameId':2025029999,'teamAbbrev':'ABC','homeRoadFlag':'R'})
        envelope['expectedGames'] = 2
        load_game_log(self.connection,envelope,'fixture.json')
        games = self.connection.execute('''SELECT s.team_abbrev,g.home_team_abbrev,g.away_team_abbrev
            FROM analytics.skater_game_stats s JOIN analytics.games g USING(game_id)
            WHERE player_id=999999997 ORDER BY game_id''').fetchall()
        self.assertEqual(games,[('TST','TST','ZZZ'),('ABC','ZZZ','ABC')])
