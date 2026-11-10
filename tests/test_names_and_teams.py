import copy
import os
import unittest
import tempfile
from pathlib import Path

from nhl_analytics.database import connect,load_snapshot,migrate
from nhl_analytics.ingest import write_json
from nhl_analytics.names import backfill,infer_names
from nhl_analytics.team_context import load_edge,load_standings,normalize_edge,validate_standings


def standing_fixture():
    teams=[dict(id=999990,seasonId=20252026,abbrev='TST',name={'default':'Test Team'})]
    row=dict(teamAbbrev={'default':'TST'},seasonId=20252026,gameTypeId=2,date='2026-04-17',
             gamesPlayed=3,wins=2,losses=0,otLosses=1,ties=0,points=5,pointPctg=0.83333,
             goalFor=9,goalAgainst=6,goalDifferential=3,regulationWins=1,regulationPlusOtWins=2,
             leagueSequence=1,conferenceSequence=1,divisionSequence=1,wildcardSequence=0,
             conferenceName='Test Conference',divisionName='Test Division',streakCode='W',streakCount=2)
    return dict(schemaVersion=1,source='NHL',kind='standings',seasonId=20252026,standingsDate='2026-04-17',
                retrievedAt='2030-01-01T00:00:00Z',url='https://example.invalid/standings',teams=teams,payload={'standings':[row]})


def edge_fixture():
    return dict(schemaVersion=1,source='NHL',kind='team_edge',seasonId=20252026,gameTypeId=2,teamId=999990,
        retrievedAt='2030-01-01T00:00:00Z',url='https://example.invalid/edge',teams=standing_fixture()['teams'],
        payload=dict(team=dict(id=999990,gamesPlayed=3),seasonsWithEdgeStats=[dict(id=20252026,gameTypes=[2])],
            skatingSpeed=dict(speedMax={'metric':36.0},burstsOver20={'value':12},burstsOver22={'value':2}),
            distanceSkated=dict(total={'metric':180.0}),shotSpeed=dict(topShotSpeed={'metric':160.0}),
            zoneTimeDetails=dict(offensiveZonePctg=0.4,offensiveZoneEvPctg=0.39,neutralZonePctg=0.2,defensiveZonePctg=0.4),
            sogSummary=[dict(locationCode='all',shots=30),dict(locationCode='high',shots=9,goals=2)]))


class NameAndContextValidationTests(unittest.TestCase):
    def test_hyphens_and_multiword_surnames_are_preserved(self):
        self.assertEqual(infer_names(None,None,'Jean-Gabriel Pageau'),('Jean-Gabriel','Pageau'))
        self.assertEqual(infer_names(None,None,'James van Riemsdyk'),('James','van Riemsdyk'))
        self.assertEqual(infer_names('Jean François',None,'Jean François Bérubé'),(None,'Bérubé'))
        self.assertEqual(infer_names(None,'van Riemsdyk','Trevor van Riemsdyk'),('Trevor',None))

    def test_known_names_are_not_overwritten_and_single_word_is_not_a_surname(self):
        self.assertEqual(infer_names('Existing','Surname','Other Name'),(None,None))
        self.assertEqual(infer_names(None,None,'Prince'),('Prince',None))
        self.assertEqual(infer_names(None,None,None),(None,None))

    def test_standings_must_cover_every_team_and_match_season(self):
        envelope=standing_fixture()
        self.assertEqual(validate_standings(envelope)[0][0]['team_id'],999990)
        envelope['payload']['standings'][0]['seasonId']=20262027
        with self.assertRaisesRegex(ValueError,'mismatch'):
            validate_standings(envelope)
        envelope=standing_fixture()
        envelope['payload']['standings']=[]
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            validate_standings(envelope)

    def test_standings_points_and_games_are_validated(self):
        envelope=standing_fixture()
        envelope['payload']['standings'][0]['points']=6
        with self.assertRaisesRegex(ValueError,'points'):
            validate_standings(envelope)

    def test_edge_units_and_missing_values(self):
        envelope=edge_fixture()
        core=normalize_edge(envelope)
        self.assertEqual(core['skating_speed_max_kmh'],36.0)
        self.assertEqual(core['speed_bursts_over20mph'],12)
        self.assertIsNone(core['shot_attempts_over90mph'])
        envelope['payload']['zoneTimeDetails']['offensiveZonePctg']=40
        with self.assertRaises(ValueError):
            normalize_edge(envelope)

    def test_edge_requested_season_must_be_available(self):
        envelope=edge_fixture()
        envelope['payload']['seasonsWithEdgeStats']=[]
        with self.assertRaisesRegex(ValueError,'advertise'):
            normalize_edge(envelope)


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for live database tests')
class NameAndTeamDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection=connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        tx=self.connection.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)

    def test_name_backfill_is_audited_and_idempotent(self):
        self.connection.execute('''INSERT INTO analytics.players(player_id,full_name,updated_at)
            VALUES (999999990,'Jean-Gabriel van Example','2000-01-01T00:00:00Z'::timestamptz)''')
        backfill(self.connection,dry_run=True)
        self.assertIsNone(self.connection.execute('SELECT first_name FROM analytics.players WHERE player_id=999999990').fetchone()[0])
        backfill(self.connection)
        row=self.connection.execute('''SELECT first_name,last_name,first_name_inferred,last_name_inferred,
            full_name,updated_at::date::text FROM analytics.players WHERE player_id=999999990''').fetchone()
        self.assertEqual(row,('Jean-Gabriel','van Example',True,True,'Jean-Gabriel van Example','2000-01-01'))
        before=self.connection.execute('SELECT count(*) FROM analytics.player_name_inferences WHERE player_id=999999990').fetchone()[0]
        backfill(self.connection)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.player_name_inferences WHERE player_id=999999990').fetchone()[0],before)

    def test_standings_reruns_corrections_and_older_cache(self):
        envelope=standing_fixture()
        self.assertEqual(load_standings(self.connection,envelope,'standing.json')['changed'],1)
        self.assertEqual(load_standings(self.connection,envelope,'standing.json')['unchanged'],1)
        envelope['retrievedAt']='2030-01-02T00:00:00Z'
        envelope['payload']['standings'][0].update(leagueSequence=2)
        load_standings(self.connection,envelope,'new.json')
        self.assertEqual(load_standings(self.connection,standing_fixture(),'old.json')['older_skipped'],1)
        self.assertEqual(self.connection.execute('SELECT league_rank FROM analytics.latest_team_standings WHERE team_id=999990').fetchone()[0],2)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.team_standings_observations WHERE team_id=999990').fetchone()[0],2)

    def test_new_standings_date_preserves_prior_day(self):
        envelope=standing_fixture()
        load_standings(self.connection,envelope,'standing.json')
        envelope['standingsDate']='2026-04-18'
        envelope['retrievedAt']='2030-01-02T00:00:00Z'
        envelope['payload']['standings'][0]['date']='2026-04-18'
        load_standings(self.connection,envelope,'new-day.json')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.team_standings WHERE team_id=999990').fetchone()[0],2)
        self.assertEqual(self.connection.execute('SELECT standings_date::text FROM analytics.latest_team_standings WHERE team_id=999990').fetchone()[0],'2026-04-18')

    def test_edge_rerun_and_per_game_features(self):
        envelope=edge_fixture()
        self.assertEqual(load_edge(self.connection,envelope,'edge.json'),'changed')
        self.assertEqual(load_edge(self.connection,envelope,'edge.json'),'unchanged')
        row=self.connection.execute('''SELECT distance_per_game_km,speed_bursts_over20mph_per_game,
            high_danger_shot_share FROM analytics.team_edge_features WHERE team_id=999990''').fetchone()
        self.assertEqual(row,(60.0,4.0,0.3))
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.team_edge_observations WHERE team_id=999990').fetchone()[0],1)

    def test_authoritative_nhl_names_replace_inference_and_clear_flags(self):
        self.connection.execute('''INSERT INTO analytics.players(player_id,full_name,updated_at)
            VALUES (999999991,'Pierre Luc Dubois','2000-01-01T00:00:00Z')''')
        backfill(self.connection)
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.work/tmp') as directory:
            root=Path(directory)
            meta=dict(schemaVersion=1,source='NHL',seasonId=20262027,gameTypeId=2,
                      collectedAt='2031-01-01T00:00:00Z',rosterObservedAt='2031-01-01T00:00:00Z')
            write_json(root/'manifest.json',dict(meta,status='complete',playerCount=1,teamCount=0,skaterCount=1,goalieCount=0))
            write_json(root/'players.json',dict(meta,players=[dict(id=999999991,firstName='Pierre-Luc',lastName='Dubois')]))
            write_json(root/'nhl-rosters.json',dict(meta,teams=[]))
            write_json(root/'player_season_stats.json',dict(meta,skaters=[dict(playerId=999999991,seasonId=20262027,gameTypeId=2)],goalies=[]))
            load_snapshot(self.connection,root)
        self.assertEqual(self.connection.execute('''SELECT first_name,last_name,first_name_inferred,last_name_inferred
            FROM analytics.players WHERE player_id=999999991''').fetchone(),('Pierre-Luc','Dubois',False,False))
