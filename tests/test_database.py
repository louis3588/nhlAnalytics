import json
import os
import tempfile
import unittest
from pathlib import Path

import psycopg

from nhl_analytics.database import connect, load_snapshot, migrate, read_snapshot
from nhl_analytics.ingest import write_json


def fixture_snapshot(path, collected='2030-01-01T00:00:00+00:00', goals=1):
    metadata = dict(schemaVersion=1, source='NHL', seasonId=20262027, gameTypeId=2,
                    collectedAt=collected, rosterObservedAt=collected)
    player = dict(id=999999999, firstName='Database', lastName='Test', birthDate='2000-01-01',
                  number=42, position='C', positionGroup='forwards')
    team = dict(id=999999, abbreviation='TST', name='Test Team', players=[player])
    write_json(path / 'manifest.json', dict(metadata, status='complete', playerCount=1,
                                           teamCount=1, skaterCount=1, goalieCount=0))
    write_json(path / 'players.json', dict(metadata, players=[player]))
    write_json(path / 'nhl-rosters.json', dict(metadata, teams=[team]))
    write_json(path / 'player_season_stats.json', dict(metadata, skaters=[dict(
        playerId=999999999, seasonId=20262027, gameTypeId=2, gamesPlayed=2, goals=goals,
        assists=2, points=goals+2, shots=5, teamAbbrevs='TST')], goalies=[]))


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        Path('.work/tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.work/tmp')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        fixture_snapshot(self.path)

    def change(self, name, update):
        path = self.path / name
        document = json.loads(path.read_text())
        update(document)
        write_json(path, document)

    def test_valid_snapshot(self):
        manifest, players, _, _, checksum = read_snapshot(self.path)
        self.assertEqual(manifest['playerCount'], len(players))
        self.assertEqual(len(checksum), 64)

    def test_metadata_mismatch(self):
        self.change('players.json', lambda d: d.update(seasonId=20252026))
        with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
            read_snapshot(self.path)

    def test_missing_player_link(self):
        self.change('player_season_stats.json', lambda d: d['skaters'][0].update(playerId=123))
        with self.assertRaisesRegex(ValueError, 'player missing'):
            read_snapshot(self.path)

    def test_duplicate_players(self):
        self.change('players.json', lambda d: d['players'].append(d['players'][0]))
        self.change('manifest.json', lambda d: d.update(playerCount=2))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            read_snapshot(self.path)

    def test_incomplete_snapshot(self):
        self.change('manifest.json', lambda d: d.update(status='failed'))
        with self.assertRaisesRegex(ValueError, 'completed'):
            read_snapshot(self.path)


@unittest.skipUnless(os.environ.get('NHL_DB_TESTS') == '1', 'Set NHL_DB_TESTS=1 for live database tests')
class DatabaseIntegrationTests(SnapshotValidationTests):
    def setUp(self):
        super().setUp()
        self.connection = connect()
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        # Every test rolls back its fixture data, including successful loads.
        self.transaction = self.connection.transaction(force_rollback=True)
        self.transaction.__enter__()
        self.addCleanup(self.transaction.__exit__, None, None, None)

    def test_load_twice_then_changed_contents(self):
        self.assertTrue(load_snapshot(self.connection, self.path))
        self.assertFalse(load_snapshot(self.connection, self.path))
        count = self.connection.execute('SELECT count(*) FROM analytics.skater_season_stats WHERE player_id=999999999').fetchone()[0]
        self.assertEqual(count, 1)
        self.change('players.json', lambda d: d['players'][0].update(firstName='Changed'))
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            load_snapshot(self.connection, self.path)

    def test_failed_stats_roll_back_players_rosters_and_run(self):
        fixture_snapshot(self.path, goals=-1)
        before = self.connection.execute('SELECT count(*) FROM analytics.import_runs').fetchone()[0]
        with self.assertRaises(psycopg.errors.CheckViolation):
            load_snapshot(self.connection, self.path)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.import_runs').fetchone()[0], before)
        self.assertIsNone(self.connection.execute('SELECT 1 FROM analytics.players WHERE player_id=999999999').fetchone())
        self.assertIsNone(self.connection.execute('SELECT 1 FROM analytics.roster_observations WHERE player_id=999999999').fetchone())

    def test_new_observation_keeps_history_and_old_identity_does_not_replace_new(self):
        load_snapshot(self.connection, self.path)
        fixture_snapshot(self.path, collected='2030-01-02T00:00:00+00:00', goals=2)
        load_snapshot(self.connection, self.path)
        fixture_snapshot(self.path, collected='2029-12-31T00:00:00+00:00')
        self.change('players.json', lambda d: d['players'][0].update(firstName='Older'))
        load_snapshot(self.connection, self.path)
        latest = self.connection.execute('SELECT goals FROM analytics.latest_skater_season_stats WHERE player_id=999999999').fetchone()[0]
        name = self.connection.execute('SELECT first_name FROM analytics.players WHERE player_id=999999999').fetchone()[0]
        self.assertEqual(latest, 2)
        self.assertEqual(name, 'Database')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM analytics.skater_season_stats WHERE player_id=999999999').fetchone()[0], 3)
