import gzip
import json
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nhl_analytics.daily_predictions import frozen_pools, load_daily, player_rows, publish_archive, publish_daily
from nhl_analytics.prediction_store import ArtifactError, PredictionStore, checksum
from nhl_analytics.refresh import commands, current_season, job_lock, run_refresh
from test_api import API_AVAILABLE, fixture


class RefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='.work/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_season_rollover_and_resume_plan(self):
        self.assertEqual(current_season(datetime(2027, 2, 1)), 20262027)
        self.assertEqual(current_season(datetime(2027, 10, 1)), 20272028)
        plan = self.root / 'game_logs/run/plan.json'
        plan.parent.mkdir(parents=True); plan.write_text('{}')
        steps = dict(commands(self.root, 20262027, Path('.env'), 1))
        self.assertIn('--resume', steps['skater_goalie_logs'])
        self.assertNotIn('--seasons', steps['skater_goalie_logs'])

    def test_custom_publication_root_is_shared_with_api_configuration(self):
        root=self.root/'custom-publications'
        archive=root/'archive'/f'{datetime.now(timezone.utc).date().isoformat()}.json.gz'
        archive.parent.mkdir(parents=True);archive.write_bytes(b'already archived')
        with patch.dict('os.environ',{'NHL_DAILY_ROOT':str(root)}), \
             patch('nhl_analytics.daily_predictions.PredictionStore.load',return_value=SimpleNamespace(metadata={'bundle_sha256':'base'})), \
             patch('nhl_analytics.daily_predictions.publish_archive',return_value={'published':True}) as publisher:
            self.assertTrue(publish_daily(20262027,Path('.env'),self.root)['published'])
            self.assertEqual(publisher.call_args.args[2],root)

    def test_lock_prevents_overlapping_refreshes_and_releases(self):
        path = self.root / 'refresh.lock'
        with job_lock(path):
            with self.assertRaisesRegex(RuntimeError, 'Another daily'):
                with job_lock(path):
                    pass
        with job_lock(path):
            pass

    def test_failed_job_retries_only_unfinished_stages_and_success_is_noop(self):
        calls = []
        def runner(arguments, **kwargs):
            calls.append(arguments)
            return SimpleNamespace(returncode=1 if len(calls) == 2 else 0)
        with patch('nhl_analytics.refresh.connect'):
            with self.assertRaises(RuntimeError):
                run_refresh(self.root, 20262027, Path('.env'), runner=runner)
            result = run_refresh(self.root, 20262027, Path('.env'), runner=runner, publisher=lambda *a: {'ok': True})
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(sum('nhl_analytics.ingest' in c for c in calls), 1)
            before = len(calls)
            run_refresh(self.root, 20262027, Path('.env'), runner=runner)
            self.assertEqual(len(calls), before)

    def test_player_rows_use_observed_counts_and_no_fake_intervals(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        records, games = [], {}
        for i in range(5):
            day = now - timedelta(days=i + 2)
            games[i] = dict(game_id=i, season_id=20262027, home_team_id=1, away_team_id=2, is_final=True,
                available_at=day, start_time_utc=day.isoformat())
            records.append(dict(player_id=101, season_id=20262027, kind='skater', game_id=i, game_date=day.date(),
                team_id=1, available_at=day, payload=dict(goals=1, assists=2, points=3)))
        games[10] = dict(game_id=10, season_id=20262027, home_team_id=1, away_team_id=2, is_final=False)
        inputs, projections, intervals = player_rows(records, games, 20262027, now, {})
        self.assertEqual(inputs[0]['points'], 15)
        self.assertEqual(inputs[0]['team_games_remaining'], 1)
        self.assertEqual(projections[0]['pace_points'], 18)
        self.assertEqual(len(intervals), 16)
        self.assertTrue(all(not r['available'] for r in intervals))
        records[0]['available_at'] = now
        with self.assertRaisesRegex(ValueError, 'available before'):
            player_rows(records, games, 20262027, now, {})

    def test_frozen_calibration_rejects_holdout_or_future_labels(self):
        path=self.root/'interval_calibration.json'
        fit=dict(evaluation_season=20262027, model='pace', target='goals', remaining_bucket='61+', prior_records=True,
            unique_calibration_players=50, eligible=True, radius80=10, radius95=20, calibration_seasons=[20242025],
            max_calibration_label_available_at='2025-06-01T12:00:00Z')
        def save():
            path.write_text(json.dumps(dict(holdout_excluded=20252026,fits=[fit])))
            (self.root/'manifest.json').write_text(json.dumps(dict(files={'interval_calibration.json':dict(sha256=checksum(path))})))
        save()
        cutoff=datetime(2026,11,11,tzinfo=timezone.utc)
        self.assertEqual(len(frozen_pools(self.root,20262027,cutoff)),1)
        fit['calibration_seasons']=[20252026];save()
        with self.assertRaisesRegex(ValueError,'holdout'):
            frozen_pools(self.root,20262027,cutoff)
        fit['calibration_seasons']=[20242025]
        fit['max_calibration_label_available_at']=cutoff.isoformat();save()
        with self.assertRaisesRegex(ValueError,'unavailable'):
            frozen_pools(self.root,20262027,cutoff)


@unittest.skipUnless(API_AVAILABLE, 'API dependencies required for serving integration fixture')
class DailyPublicationTests(unittest.TestCase):
    setUp = RefreshTests.setUp
    def test_archives_append_without_changing_historical_predictions_and_are_rebuildable(self):
        run = fixture(self.root)
        base = PredictionStore.load(run, self.root / 'cache')
        root = self.root / 'daily'
        (root / 'archive').mkdir(parents=True)
        with closing(base.connect()) as connection:
            before = connection.execute('SELECT * FROM projections ORDER BY season,prediction_at,model,player').fetchall()
        def archive(day):
            cutoff = f'2026-11-{day:02}T17:00:00.000000+00:00'
            row = dict(player_id=101, last_played_team_id=1, games_played=5, goals=2, assists=3, points=5,
                team_games_remaining=70, prior_games=20, max_assumed_available_at='2026-11-10T16:00:00.000000+00:00')
            prediction = dict(player_id=101, projected_games=12, pace_goals=4, pace_assists=6, pace_points=10,
                prior_shrinkage_goals=3, prior_shrinkage_assists=6, prior_shrinkage_points=9)
            intervals = []
            for model in ('pace', 'prior_shrinkage'):
                for target in ('goals', 'assists', 'points', 'games'):
                    for nominal in (0.8, 0.95):
                        intervals.append(dict(player_id=101, model=model, target=target, nominal=nominal,
                            observed=row['games_played' if target == 'games' else target], prediction=prediction['projected_games' if target == 'games' else f'{model}_{target}'],
                            available=False, lower=None, upper=None, calibration_players=0, calibration_seasons='',
                            max_calibration_label_available_at=None, calibration_scope='insufficient earlier clusters', radius=None))
            payload = dict(season=20262027, day=f'2026-11-{day:02}', prediction_at=cutoff, source_policy='observed before cutoff',
                base_sha256=base.metadata['bundle_sha256'], inputs=[row], projections=[prediction], intervals=intervals, games=[], fixtures=[])
            path = root / 'archive' / f'2026-11-{day:02}.json.gz'
            with gzip.open(path, 'wt') as stream:
                json.dump(payload, stream)
            return path
        first = archive(11)
        publish_archive(run, first, root, self.root / 'cache')
        original_bytes = first.read_bytes()
        second = archive(12)
        pointer = publish_archive(run, second, root, self.root / 'cache')
        store = load_daily(root)
        self.assertEqual(first.read_bytes(), original_bytes)
        with closing(store.connect()) as connection:
            old = connection.execute("SELECT * FROM projections WHERE prediction_at<'2026-11-11' ORDER BY season,prediction_at,model,player").fetchall()
            self.assertEqual([tuple(r) for r in old], [tuple(r) for r in before])
            self.assertEqual(connection.execute('SELECT count(*) FROM inputs').fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT count(*) FROM prediction_origins WHERE data_mode='observed_daily'").fetchone()[0], 2)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        # A retry uses the same immutable generation, not duplicate rows.
        self.assertEqual(publish_archive(run, second, root, self.root / 'cache'), pointer)
        # Serving files are disposable: original benchmark + archives rebuild them.
        Path(pointer['database']).unlink()
        self.assertEqual(publish_archive(run, second, root, self.root / 'cache')['bundle_sha256'], pointer['bundle_sha256'])
        # The API serves saved daily rows with their actual provenance.
        from fastapi.testclient import TestClient
        from nhl_analytics.api import create_app
        with patch.dict('os.environ', {'NHL_DAILY_ROOT': str(root), 'NHL_PREDICTIONS_RUN': ''}):
            with TestClient(create_app(cache_dir=self.root / 'cache',refresh_seconds=0.02,live_enabled=False)) as client:
                data = client.get('/api/predictions/players/101/history').json()
                self.assertEqual(data['total'], 4)
                self.assertEqual(data['items'][0]['projection']['data_mode'], 'reconstructed_latest_corrected')
                self.assertEqual(data['items'][-1]['projection']['data_mode'], 'observed_daily')
                self.assertEqual(data['metadata']['data_mode'], 'operational_daily_with_reconstructed_history')
                third=archive(13)
                publish_archive(run,third,root,self.root/'cache')
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if client.get('/api/metadata').json()['bundle_id']=='daily-2026-11-13':
                        break
                    time.sleep(0.03)
                self.assertEqual(client.get('/api/metadata').json()['bundle_id'],'daily-2026-11-13')
                self.assertEqual(client.get('/api/predictions/players/101/history').json()['total'],5)
        # Altering an archived forecast is detected and cannot replace latest.
        pointer_bytes=(root/'latest.json').read_bytes()
        first.write_bytes(first.read_bytes()+b'changed')
        with self.assertRaises(ArtifactError):
            publish_archive(run,third,root,self.root/'cache')
        self.assertEqual((root/'latest.json').read_bytes(),pointer_bytes)


if __name__ == '__main__':
    unittest.main()
