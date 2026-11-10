import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nhl_analytics.database import connect, migrate
from nhl_analytics.moneypuck import csv_rows, detect_kind, import_file, integer, transform


class MoneyPuckParsingTests(unittest.TestCase):
    def test_decimal_ids_are_exact(self):
        self.assertEqual(integer('8478542.0'),8478542)
        self.assertIsNone(integer('0.0',True))
        self.assertIsNone(integer('0.00',True))
        self.assertIsNone(integer('-1.00',True))
        self.assertIsNone(integer('',True))
        for value in ('8478542.5','NaN','Infinity','-2'):
            with self.assertRaises(ValueError):
                integer(value)

    def test_duplicate_csv_headers_agree(self):
        rows = csv_rows(csv.reader(['NYR,NYR,2026']),['team','team','season'])
        self.assertEqual(next(rows)['team'],'NYR')
        with self.assertRaisesRegex(ValueError,'conflicting'):
            list(csv_rows(csv.reader(['NYR,BOS,2026']),['team','team','season']))

    def test_row_width_is_validated(self):
        with self.assertRaisesRegex(ValueError,'expected'):
            list(csv_rows(csv.reader(['NYR,2026,extra']),['team','season']))

    def test_line_members_remain_individual_player_ids(self):
        row = dict(lineId='84808078480839',season='2026',team='BUF',situation='5on5')
        result = transform(row,'line_season',None,1,None,2)
        self.assertEqual(result[5],20262027)
        self.assertEqual(result[10],[8480807,8480839])

    def test_shot_game_id_and_shooter_goalie_links(self):
        row = dict(season='2025',game_id='20001',id='14',isPlayoffGame='0',team='HOME',
                   homeTeamCode='FLA',awayTeamCode='CHI',shooterPlayerId='8478542.0',
                   goalieIdForShot='8481519.0',xGoal='0.1',goal='0')
        result = transform(row,'shot',None,1,None,2)
        self.assertEqual(result[8:10],(8478542,8481519))
        self.assertEqual(result[13],2025020001)
        self.assertIsNone(result[14])  # No fabricated shot game date.
        self.assertEqual(result[11],'FLA')
        row['isPlayoffGame']='1'
        with self.assertRaisesRegex(ValueError,'disagrees'):
            transform(row,'shot',None,1,None,2)

    def test_unsupported_csv_is_rejected(self):
        with self.assertRaises(ValueError):
            detect_kind(['unrelated','fields'])


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS') == '1','Set NHL_DB_TESTS=1 for live database tests')
class MoneyPuckDatabaseTests(unittest.TestCase):
    def setUp(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.work/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.csv'
        with self.path.open('w',newline='',encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['playerId','season','name','team','situation','I_F_xGoals'])
            writer.writerow(['999999998','2026','Fixture Player','TST','all','1.25'])
            writer.writerow(['999999998','2026','Fixture Player','TST','5on5','0.75'])
        self.connection = connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        tx = self.connection.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)

    def test_copy_player_link_and_duplicate_file(self):
        self.assertEqual(import_file(self.connection,self.path,1),2)
        self.assertEqual(import_file(self.connection,self.path,1),2)
        result = self.connection.execute('''SELECT count(*),max(xgoals_for)
            FROM analytics.moneypuck_complete WHERE player_id=999999998''').fetchone()
        self.assertEqual(result,(2,1.25))
        self.assertEqual(self.connection.execute('SELECT full_name FROM analytics.players WHERE player_id=999999998').fetchone()[0],'Fixture Player')

    def test_partial_import_is_hidden_and_resumes(self):
        original = transform
        def interruption(*args):
            if args[3] == 2:
                raise ValueError('Simulated interruption')
            return original(*args)
        with patch('nhl_analytics.moneypuck.transform',side_effect=interruption):
            with self.assertRaisesRegex(ValueError,'Simulated interruption'):
                import_file(self.connection,self.path,1)
        checkpoint = self.connection.execute("SELECT status,rows_processed FROM analytics.moneypuck_files WHERE paths @> %s::jsonb",
                                            ('["'+str(self.path.resolve()).replace('\\','\\\\')+'"]',)).fetchone()
        self.assertEqual(checkpoint,('failed',1))
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.moneypuck_complete WHERE player_id=999999998').fetchone()[0],0)
        self.assertEqual(import_file(self.connection,self.path,1),2)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.moneypuck_complete WHERE player_id=999999998').fetchone()[0],2)

    def test_duplicate_source_shots_are_retained_but_canonical_event_is_unique(self):
        with self.path.open('w',newline='',encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['shotID','season','game_id','id','isPlayoffGame','team',
                             'homeTeamCode','awayTeamCode','shooterPlayerId','goalieIdForShot','xGoal'])
            writer.writerow(['0','2026','29999','361','0','HOME','TST','TST','999999998.0','','0.1'])
            writer.writerow(['1','2026','29999','361','0','HOME','TST','TST','999999998.0','','0.1'])
            writer.writerow(['2','2026','29999','362','0','HOME','TST','TST','999999998.0','','0.2'])
        self.assertEqual(import_file(self.connection,self.path,1),3)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM analytics.moneypuck_complete WHERE kind='shot' AND player_id=999999998").fetchone()[0],3)
        canonical = self.connection.execute("SELECT count(*),sum(xgoals_for) FROM analytics.moneypuck_latest_shots WHERE player_id=999999998").fetchone()
        self.assertEqual(canonical[0],2)
        self.assertAlmostEqual(canonical[1],0.3)
