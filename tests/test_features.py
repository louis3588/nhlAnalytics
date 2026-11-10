import copy
import os
import unittest
from datetime import date,datetime,timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from nhl_analytics.asof import past_final,read_context,read_games,read_player_games,read_shots
from nhl_analytics.database import connect,migrate,timestamp
from nhl_analytics.features import attach_xg,build_features,schemas
from nhl_analytics.game_logs import load_game_log
from nhl_analytics.schedules import load_season
from nhl_analytics.team_context import load_edge

CUTOFF=timestamp('2026-11-10T12:00:00Z')
AVAILABLE=timestamp('2026-11-10T09:00:00Z')


def game(identifier=2026029991,day=8,home_score=4,away_score=2,result='REG'):
    return dict(game_id=identifier,season_id=20262027,game_type_id=2,game_date=f'2026-11-{day:02d}',
        home_team_id=1,away_team_id=2,home_team_abbrev='AAA',away_team_abbrev='BBB',
        start_time_utc=f'2026-11-{day:02d}T23:00:00Z',available_at=AVAILABLE,
        is_final=True,home_score=home_score,away_score=away_score,result_type=result,game_state='OFF',schedule_state='OK')


def record(g,kind='skater',goals=1,assists=1,shots=5,toi='20:00'):
    payload=dict(goals=goals,assists=assists,points=goals+assists,shots=shots,toi=toi,powerPlayPoints=1)
    if kind=='goalie':
        payload.update(shotsAgainst=10,goalsAgainst=1,gamesStarted=1)
    return dict(player_id=99 if kind=='skater' else 98,season_id=g['season_id'],kind=kind,game_id=g['game_id'],
        game_date=date.fromisoformat(g['game_date']),start_at=timestamp(g['start_time_utc']),team_id=1,
        available_at=AVAILABLE,payload=payload)


class FeatureTests(unittest.TestCase):
    def test_future_results_future_corrections_and_future_team_universe_are_excluded(self):
        past=game()
        future=game(2026029992,day=11,home_score=99)
        unknown=game(2026029993)
        unknown.update(home_team_id=3,away_team_id=4,available_at=CUTOFF)
        rows=[record(past),record(future,goals=99)]
        revised=record(past,goals=88)
        revised['available_at']=CUTOFF
        rows.append(revised)
        outputs,_=build_features({g['game_id']:g for g in (past,future,unknown)},rows,{}, {},{}, {},CUTOFF,2)
        self.assertEqual(outputs['players'][0]['season_goals'],1)
        self.assertEqual({r['team_id'] for r in outputs['teams']},{1,2})
        self.assertEqual(outputs['teams'][0]['season_games'],1)

    def test_scoring_rates_weighted_goalie_rates_and_unknown_metrics(self):
        first=game()
        second=game(2026029992,day=9,home_score=2,away_score=3,result='OT')
        p1=record(first)
        p2=record(second,goals=2,assists=0,shots=4,toi='40:00')
        p2['payload']['powerPlayPoints']=None
        g1=record(first,'goalie')
        g2=record(second,'goalie',toi='60:00')
        g2['payload'].update(shotsAgainst=30,goalsAgainst=6)
        outputs,_=build_features({first['game_id']:first,second['game_id']:second},[p1,p2,g1,g2],{}, {},{}, {},CUTOFF,2)
        player=outputs['players'][0]
        goalie=outputs['goalies'][0]
        self.assertEqual(player['season_points_per60'],4.0)
        self.assertAlmostEqual(player['season_shooting_pct'],3/9)
        self.assertIsNone(player['season_power_play_points'])
        self.assertAlmostEqual(goalie['season_save_pct'],33/40)
        self.assertEqual(goalie['appearances_last7_days'],2)
        self.assertIsNone(player['season_unblocked_xg'])

    def test_known_schedule_rest_and_future_context_gating(self):
        last=game(day=9)
        upcoming=game(2026029992,day=10)
        upcoming.update(is_final=False,game_state='FUT',home_score=None,away_score=None,result_type=None)
        future_context=dict(team_id=1,season_id=20262027,observed_at=CUTOFF,
            standings_date=date(2026,11,9),payload=dict(points=999,pointPctg=1,leagueSequence=1))
        outputs,_=build_features({last['game_id']:last,upcoming['game_id']:upcoming},[],{(1,20262027):future_context},{},{},{},CUTOFF,2)
        team=next(r for r in outputs['teams'] if r['team_id']==1)
        self.assertTrue(team['next_game_back_to_back'])
        self.assertEqual(team['rest_days_before_next_game'],0)
        self.assertEqual(team['next_opponent_team_id'],2)
        self.assertIsNone(team['standings_points'])
        self.assertLess(team['max_source_available_at'],CUTOFF)

    def test_zero_xg_requires_coverage_and_matching_counts(self):
        g=game()
        row={**record(g,goals=0,assists=0,shots=0),'cutoff':CUTOFF}
        coverage={g['game_id']:dict(available_at=AVAILABLE,unknown_player=False,unknown_goalie=False)}
        self.assertEqual(attach_xg(row,{},coverage)[0],0.0)
        self.assertIsNone(attach_xg(row,{}, {})[0])
        coverage[g['game_id']]['available_at']=CUTOFF
        self.assertIsNone(attach_xg(row,{},coverage)[0])

    def test_team_xg_uses_observed_game_side_and_excludes_shootout_decider(self):
        g=game(home_score=4,away_score=3,result='SO')
        source=dict(game_date=date.fromisoformat(g['game_date']),available_at=AVAILABLE,
                    gf=3.0,ga='3.0',xgf=2.2,xga=1.9)
        outputs,_=build_features({g['game_id']:g},[],{},{},{},{},CUTOFF,2,{(g['game_id'],True):source})
        home=next(r for r in outputs['teams'] if r['team_id']==1)
        self.assertEqual(home['season_xg_for'],2.2)
        self.assertEqual(home['season_xg_covered_games'],1)
        source['available_at']=CUTOFF
        outputs,_=build_features({g['game_id']:g},[],{},{},{},{},CUTOFF,2,{(g['game_id'],True):source})
        self.assertIsNone(next(r for r in outputs['teams'] if r['team_id']==1)['season_xg_for'])

    def test_empty_schema_and_naive_cutoff_safety(self):
        import pyarrow as pa
        for schema in schemas().values():
            table=pa.Table.from_pylist([],schema=schema)
            self.assertEqual(table.num_rows,0)
            self.assertNotIn('full_name',schema.names)
            self.assertNotIn('birth_date',schema.names)
        with self.assertRaisesRegex(ValueError,'timezone'):
            timestamp('2026-11-10')


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS')=='1','Set NHL_DB_TESTS=1 for live database tests')
class AsOfDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection=connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        tx=self.connection.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)

    def seed_history(self):
        teams=[dict(id=999983,seasonId=20252026,abbrev='TST',name={'default':'Test'}),
               dict(id=999984,seasonId=20252026,abbrev='ZZZ',name={'default':'Other'})]
        games=[]
        for identifier,day in ((2025029994,1),(2025029993,2)):
            games.append(dict(id=identifier,season=20252026,gameType=2,gameDate=f'2026-04-{day:02d}',
                homeTeam=dict(id=999983,abbrev='TST',score=3),awayTeam=dict(id=999984,abbrev='ZZZ',score=1),
                gameState='OFF',gameScheduleState='OK',gameOutcome={'lastPeriodType':'REG'},startTimeUTC=f'2026-04-{day:02d}T23:00:00Z'))
        schedule=dict(schemaVersion=1,source='NHL',seasonId=20252026,gameTypes=[2],retrievedAt='2030-01-01T00:00:00Z',teams=teams,games=games)
        load_season(self.connection,schedule,'test.json')
        rows=[dict(gameId=g['id'],gameDate=g['gameDate'],teamAbbrev='TST',opponentAbbrev='ZZZ',homeRoadFlag='H',goals=1,assists=0,points=1,shots=3,toi='20:00') for g in games]
        envelope=dict(playerId=999999983,seasonId=20252026,gameTypeId=2,kind='skater',expectedGames=2,
            retrievedAt='2030-01-02T00:00:00Z',url='https://example.invalid',payload=dict(seasonId=20252026,gameTypeId=2,gameLog=rows))
        load_game_log(self.connection,envelope,'test.json')
        return schedule,envelope

    def test_future_correction_and_retraction_do_not_change_earlier_features(self):
        schedule,envelope=self.seed_history()
        envelope['retrievedAt']='2030-01-05T00:00:00Z'
        envelope['expectedGames']=1
        envelope['payload']['gameLog']=envelope['payload']['gameLog'][:1]
        envelope['payload']['gameLog'][0].update(goals=2,points=2)
        load_game_log(self.connection,envelope,'correction.json')
        schedule['retrievedAt']='2030-01-05T00:00:00Z'
        schedule['games'][0]['homeTeam']['score']=4
        load_season(self.connection,schedule,'correction.json')
        cutoff=timestamp('2030-01-03T00:00:00Z')
        known=read_games(self.connection,cutoff,[20252026],2)
        rows,_=read_player_games(self.connection,cutoff,[20252026],2,known)
        selected=[r for r in rows if r['player_id']==999999983]
        self.assertEqual(len(selected),2)
        self.assertEqual(sum(r['payload']['goals'] for r in selected),2)
        self.assertEqual(known[2025029994]['home_score'],3)
        later=timestamp('2030-01-06T00:00:00Z')
        known=read_games(self.connection,later,[20252026],2)
        rows,_=read_player_games(self.connection,later,[20252026],2,known)
        self.assertEqual(len([r for r in rows if r['player_id']==999999983]),1)
        boundary=timestamp('2030-01-02T00:00:00Z')
        rows,_=read_player_games(self.connection,boundary,[20252026],2,read_games(self.connection,boundary,[20252026],2))
        self.assertFalse(any(r['player_id']==999999983 for r in rows))

    def test_completed_file_gate_and_duplicate_event_deduplication(self):
        self.connection.execute("INSERT INTO analytics.players(player_id,full_name,updated_at) VALUES (999999983,'Test Player',now())")
        file=uuid4()
        self.connection.execute('''INSERT INTO analytics.moneypuck_files
            (file_id,sha256,paths,kind,game_type_default,observed_at,status,rows_processed,completed_at)
            VALUES (%s,%s,'[]','shot',2,'2030-01-01Z','complete',2,'2030-01-05Z')''',(file,str(file)))
        for index in (1,2):
            self.connection.execute('''INSERT INTO analytics.moneypuck_records
                (file_id,row_number,observed_at,kind,record_key,season_id,game_type_id,entity_id,player_id,
                 line_player_ids,team_code,situation,game_id,xgoals_for,goals,payload)
                VALUES (%s,%s,'2030-01-01Z','shot','test-event',20252026,2,'1',999999983,'{}','TST','all',2025029994,0.2,0,%s)''',
                (file,index,Jsonb(dict(event='SHOT',shotOnEmptyNet='1',shotID=str(index)))))
        cutoff=timestamp('2030-01-04T00:00:00Z')
        actors,coverage,meta=read_shots(self.connection,cutoff,[20252026],2,{2025029994})
        self.assertEqual(meta['canonical_events'],0)
        self.connection.execute("UPDATE analytics.moneypuck_files SET completed_at='2030-01-03Z' WHERE file_id=%s",(file,))
        actors,coverage,meta=read_shots(self.connection,cutoff,[20252026],2,{2025029994})
        self.assertEqual(meta['canonical_events'],1)
        self.assertEqual(float(actors[('player',999999983,2025029994)]['xg']),0.2)

    def test_future_final_result_cannot_replace_known_pre_game_schedule(self):
        teams=[dict(id=999983,seasonId=20252026,abbrev='TST',name={'default':'Test'}),
               dict(id=999984,seasonId=20252026,abbrev='ZZZ',name={'default':'Other'})]
        g=dict(id=2025029994,season=20252026,gameType=2,gameDate='2026-04-01',
            homeTeam=dict(id=999983,abbrev='TST'),awayTeam=dict(id=999984,abbrev='ZZZ'),
            gameState='FUT',gameScheduleState='OK',startTimeUTC='2026-04-01T23:00:00Z')
        envelope=dict(schemaVersion=1,source='NHL',seasonId=20252026,gameTypes=[2],
            retrievedAt='2025-11-01T00:00:00Z',teams=teams,games=[g])
        load_season(self.connection,envelope,'before.json')
        envelope['retrievedAt']='2030-01-01T00:00:00Z'
        g.update(gameState='OFF',gameOutcome={'lastPeriodType':'REG'})
        g['homeTeam']['score']=3
        g['awayTeam']['score']=1
        load_season(self.connection,envelope,'after.json')
        cutoff=timestamp('2026-04-03T00:00:00Z')
        known=read_games(self.connection,cutoff,[20252026],2)
        self.assertFalse(known[2025029994]['is_final'])
        self.assertFalse(past_final(known[2025029994],cutoff))
        self.assertTrue(self.connection.execute('SELECT is_final FROM analytics.games WHERE game_id=2025029994').fetchone()[0])

    def test_future_edge_correction_is_not_read_from_current_table(self):
        schedule,_=self.seed_history()
        envelope=dict(schemaVersion=1,source='NHL',kind='team_edge',seasonId=20252026,gameTypeId=2,
            teamId=999983,teams=schedule['teams'],retrievedAt='2030-01-01T00:00:00Z',url='https://example.invalid',
            payload=dict(team=dict(id=999983,gamesPlayed=2),seasonsWithEdgeStats=[dict(id=20252026,gameTypes=[2])],
                zoneTimeDetails=dict(offensiveZonePctg=0.4,neutralZonePctg=0.2,defensiveZonePctg=0.4)))
        load_edge(self.connection,envelope,'old.json')
        envelope['retrievedAt']='2030-01-05T00:00:00Z'
        envelope['payload']['zoneTimeDetails'].update(offensiveZonePctg=0.5,defensiveZonePctg=0.3)
        load_edge(self.connection,envelope,'new.json')
        _,edge=read_context(self.connection,timestamp('2030-01-03T00:00:00Z'),[20252026],2)
        self.assertEqual(edge[(999983,20252026)]['payload']['zoneTimeDetails']['offensiveZonePctg'],0.4)
