import csv
import importlib.util
import json
import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date,datetime,timezone
from pathlib import Path
from unittest.mock import patch

API_AVAILABLE=bool(importlib.util.find_spec('fastapi') and (importlib.util.find_spec('httpx2') or importlib.util.find_spec('httpx')))
if API_AVAILABLE:
    from fastapi.testclient import TestClient
    from nhl_analytics.api import create_app
    from nhl_analytics.prediction_store import ArtifactError,PredictionStore,checksum


def write_json(path,value):
    path.write_text(json.dumps(value),encoding='utf-8')


def write_csv(path,rows):
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return dict(rows=len(rows),sha256=checksum(path))


def fixture(root):
    import pyarrow as pa
    import pyarrow.parquet as pq
    mode='reconstructed_latest_corrected';clock='2026-11-10T19:00:00Z'
    baseline=root/'baseline';history=root/'history';run=root/'evaluation'
    for path in (baseline,history,run):path.mkdir()
    inputs=[];predictions=[];intervals=[]
    for player,season,cutoff in [(101,20212022,'2021-11-01T17:00:00Z'),(101,20262027,'2026-11-01T17:00:00Z'),
                                (102,20262027,'2026-11-01T17:00:00Z'),(101,20262027,'2026-11-10T17:00:00Z')]:
        inputs.append(dict(player_id=player,season_id=season,prediction_at=cutoff,data_mode=mode,last_played_team_id=1,
            games_played=5,goals=2,assists=3,points=5,team_games_remaining=70,prior_games=20,
            max_assumed_available_at=cutoff[:8]+'01T16:00:00Z'))
        pred=dict(player_id=player,season_id=season,prediction_at=cutoff,data_mode=mode,projected_games=12,
            pace_goals=4,pace_assists=6,pace_points=10,prior_shrinkage_goals=3,prior_shrinkage_assists=6,prior_shrinkage_points=9)
        predictions.append(pred)
        for model in ('pace','prior_shrinkage'):
            for target in ('goals','assists','points','games'):
                point=pred['projected_games' if target=='games' else f'{model}_{target}']
                observed=inputs[-1]['games_played' if target=='games' else target]
                for nominal in (0.8,0.95):
                    available=season!=20212022
                    intervals.append(dict(player_id=player,season_id=season,prediction_at=cutoff,data_mode=mode,model=model,target=target,
                        nominal=nominal,observed=observed,prediction=point,available=available,
                        lower=max(observed,math.floor(point-3)) if available else '',upper=point+3 if available else '',
                        calibration_players=50 if available else 0,calibration_seasons='20242025' if available else '',
                        max_calibration_label_available_at='2025-06-01T12:00:00Z' if available else '',
                        calibration_scope='earlier player clusters' if available else 'insufficient earlier non-holdout player clusters',
                        radius=3 if available else ''))
    games=[];elo=[]
    for gid,season,day,final in [(2021020001,20212022,'2021-10-20',True),(2026020001,20262027,'2026-11-12',False),
                                (2026020002,20262027,'2026-11-08',True)]:
        start=datetime.fromisoformat(day+'T23:00:00+00:00')
        games.append(dict(game_id=gid,season_id=season,game_date=date.fromisoformat(day),start_time_utc=start,
            home_team_id=1,away_team_id=2,home_team_abbrev='AAA',away_team_abbrev='BBB',is_final=final))
        elo.append(dict(game_id=gid,season_id=season,prediction_at=clock if not final else day+'T22:00:00Z',data_mode=mode,
            home_team_id=1,away_team_id=2,home_rating=1510,away_rating=1490,home_win_probability=0.6,
            historical_home_win_probability=0.54,max_assumed_available_at=''))
    pq.write_table(pa.Table.from_pylist(games),history/'games.parquet')
    write_json(history/'manifest.json',dict(status='complete',data_mode=mode,artifacts={'games':dict(sha256=checksum(history/'games.parquet'))}))
    base_files={name:write_csv(baseline/f'{name}.csv',rows) for name,rows in [('player_inputs',inputs),('player_predictions',predictions)]}
    write_json(baseline/'manifest.json',dict(status='complete',data_mode=mode,as_of=clock,history=str(history),
        history_manifest_sha256=checksum(history/'manifest.json'),artifacts=base_files))
    params=dict(elo_k=10,elo_home_advantage=25,elo_season_retention=0.75)
    write_json(run/'elo_selection.json',dict(parameters=params,excluded_holdout_from_selection=True))
    write_json(run/'selected_elo_model.json',dict(parameters=params,data_mode=mode,as_of=clock,
        selection_sha256=checksum(run/'elo_selection.json'),franchise_mapping={'59':53,'68':53}))
    write_csv(run/'player_intervals.csv',intervals);write_csv(run/'selected_elo_predictions.csv',elo)
    write_json(run/'manifest.json',dict(status='complete',version='1.0.0',data_mode=mode,as_of=clock,
        original_baseline=str(baseline),original_manifest_sha256=checksum(baseline/'manifest.json'),
        history_manifest_sha256=checksum(history/'manifest.json'),reconstructed_policy='Assumed next-day noon Eastern availability',
        files={name:dict(sha256=checksum(run/name)) for name in ('player_intervals.csv','selected_elo_predictions.csv',
                                                               'selected_elo_model.json','elo_selection.json')}))
    return run


@unittest.skipUnless(API_AVAILABLE,'Install the api extra for HTTP tests')
class APITests(unittest.TestCase):
    def test_optional_publisher_failure_does_not_stop_prediction_api(self):
        with patch('nhl_analytics.public_refresh.PublicPublisher',side_effect=OSError('unavailable')):
            app=create_app(self.run,self.cache,live_enabled=False,publishing_enabled=True)
            with TestClient(app) as client:
                health=client.get('/health').json()
                self.assertEqual(health['status'],'ok')
                self.assertEqual(health['public_publishing']['status'],'unavailable')
                self.assertEqual(client.get('/api/predictions/players').status_code,200)
    def test_prospective_routes_read_shared_cache_without_inference(self):
        from types import SimpleNamespace
        self.assertEqual(self.client.get('/api/prospective/performance').status_code,503)
        item=dict(game_id=2026020001,season_id=20262027,home_team_id=1,away_team_id=2,status='sealed')
        self.app.state.prospective=SimpleNamespace(cache=dict(items=[item],revisions=[dict(game_id=item['game_id'])],
            performance=dict(data_mode='prospective_observed'),health=dict(status='running',error=None)))
        self.assertEqual(self.client.get('/api/prospective/games?team_id=1').json()['total'],1)
        self.assertEqual(self.client.get('/api/prospective/games?team_id=3').json()['total'],0)
        self.assertEqual(self.client.get('/api/prospective/games?season=20252026').json()['total'],0)
        self.assertEqual(self.client.get('/api/prospective/games/2026020001').json()['item'],item)
        self.assertEqual(self.client.get('/api/prospective/games/2026029999').status_code,404)
        self.assertEqual(self.client.get('/api/prospective/performance').json()['report']['data_mode'],'prospective_observed')
        self.assertEqual(self.client.get('/api/prospective/games?limit=0').status_code,422)

    def test_player_history_is_chronological_paginated_and_personal(self):
        result=self.client.get('/api/predictions/players/101/history',params={'season':20262027,'limit':1}).json()
        self.assertEqual(result['total'],2)
        self.assertEqual(result['items'][0]['projection']['prediction_at'],'2026-11-01T17:00:00Z')
        self.assertEqual(result['items'][0]['interval']['nominal'],0.8)
        second=self.client.get('/api/predictions/players/101/history',params={'season':20262027,'offset':1,'model':'pace','target':'goals','nominal':0.95}).json()['items'][0]
        self.assertEqual(second['projection']['projected']['goals'],4)
        self.assertEqual(second['interval']['prediction'],4)
        self.assertEqual(second['interval']['nominal'],0.95)
        self.assertEqual(second['projection']['prediction_at'],'2026-11-10T17:00:00Z')
        self.assertEqual(self.client.get('/api/predictions/players/102/history').json()['total'],1)
        historical=self.client.get('/api/predictions/players/101/history',params={'season':20212022}).json()
        self.assertFalse(historical['items'][0]['interval']['available'])
        self.assertEqual(self.client.get('/api/predictions/players/999/history').status_code,404)
        self.assertEqual(self.client.get('/api/predictions/players/101/history?target=bad').status_code,422)

    def setUp(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.run=fixture(self.root);self.cache=self.root/'cache'
        self.app=create_app(self.run,self.cache)
        self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def test_latest_snapshot_is_global_not_each_players_stale_forecast(self):
        result=self.client.get('/api/predictions/players').json()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['player_id'],101)
        self.assertEqual(result['items'][0]['projected']['points'],9)
        self.assertEqual(result['metadata']['data_mode'],'reconstructed_latest_corrected')
        self.assertEqual(self.client.get('/api/predictions/players/102').status_code,404)

    def test_pagination_exact_timestamp_and_model_filters(self):
        params=dict(prediction_at='2026-11-01T17:00:00Z',limit=1,offset=1,model='pace',team_id=1)
        response=self.client.get('/api/predictions/players',params=params)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['total'],2)
        self.assertEqual(response.json()['items'][0]['player_id'],102)
        self.assertEqual(response.json()['items'][0]['projected']['points'],10)
        self.assertEqual(self.client.get('/api/predictions/players',params={'team_id':999}).json()['total'],0)

    def test_intervals_are_saved_marginal_bands_not_labels(self):
        response=self.client.get('/api/predictions/players/101/intervals',params={'target':'points','nominal':0.8})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['total'],1)
        item=response.json()['items'][0]
        self.assertEqual(item['nominal'],0.8)
        self.assertGreaterEqual(item['lower'],item['observed'])
        self.assertEqual(item['calibration_seasons'],[20242025])
        self.assertNotIn('actual',item)
        self.assertEqual(self.client.get('/api/predictions/intervals',params={'nominal':0.95}).json()['total'],4)

    def test_missing_historical_calibration_is_explicit_not_fake_bounds(self):
        response=self.client.get('/api/predictions/players/101/intervals',params={'season':20212022})
        self.assertEqual(response.status_code,200)
        for item in response.json()['items']:
            self.assertFalse(item['available']);self.assertIsNone(item['lower']);self.assertIsNone(item['upper'])

    @patch('nhl_analytics.api.datetime',wraps=datetime)
    def test_improved_elo_fixtures_and_filters(self,clock):
        clock.now.return_value=datetime(2026,11,11,12,tzinfo=timezone.utc)
        response=self.client.get('/api/predictions/games')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['total'],1)
        item=response.json()['items'][0]
        self.assertEqual(item['model'],'development_selected')
        self.assertEqual(item['game_id'],2026020001)
        self.assertEqual(item['home_win_probability'],0.6)
        self.assertAlmostEqual(item['home_win_probability']+item['away_win_probability'],1)
        self.assertEqual(item['snapshot_status'],'scheduled')
        self.assertEqual(self.client.get('/api/predictions/games',params={'status':'all','team_id':2}).json()['total'],2)
        self.assertEqual(self.client.get('/api/predictions/games',params={'status':'final'}).json()['items'][0]['game_id'],2026020002)
        self.assertEqual(self.client.get('/api/predictions/games/2021020001').status_code,200)
        self.assertEqual(self.client.get('/api/predictions/games',params={'date_to':'2026-11-11'}).json()['total'],0)

    @patch('nhl_analytics.api.datetime',wraps=datetime)
    def test_elapsed_scheduled_game_is_not_upcoming(self,clock):
        clock.now.return_value=datetime(2026,11,13,12,tzinfo=timezone.utc)
        self.assertEqual(self.client.get('/api/predictions/games').json()['total'],0)
        self.assertEqual(self.client.get('/api/predictions/games',params={'status':'all'}).json()['total'],2)

    def test_invalid_filters_and_unknown_entities(self):
        for params in ({'limit':201},{'offset':-1},{'model':'made-up'},{'team_id':"1 OR 1=1"},
                       {'prediction_at':'2026-11-10'},{'season':20262028},{'player_id':10**100}):
            r=self.client.get('/api/predictions/players',params=params)
            self.assertEqual(r.status_code,422,(params,r.text))
        self.assertEqual(self.client.get('/api/predictions/intervals',params={'nominal':0.9}).status_code,422)
        self.assertEqual(self.client.get('/api/predictions/games',params={'date_from':'2026-12-01','date_to':'2026-11-01'}).status_code,422)
        self.assertEqual(self.client.get('/api/predictions/games/999').status_code,404)
        self.assertEqual(self.client.get('/api/predictions/players/-1').status_code,422)
        self.assertEqual(self.client.get('/api/predictions/players',params={'prediction_at':'2026-11-09T17:00:00Z'}).status_code,404)

    def test_upcoming_uses_current_clock_not_old_bundle_date(self):
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        with patch('nhl_analytics.api.datetime', Later):
            self.assertEqual(self.client.get('/api/predictions/games').json()['total'],0)
            old=self.client.get('/api/predictions/games/2026020001').json()['item']
            self.assertEqual(old['snapshot_status'],'unresolved')

    def test_requests_do_not_reload_scan_csv_or_hash_artifacts(self):
        with patch('nhl_analytics.prediction_store.csv_rows',side_effect=AssertionError('request parsed CSV')), \
             patch('nhl_analytics.prediction_store.checksum',side_effect=AssertionError('request hashed files')), \
             patch.object(PredictionStore,'load',side_effect=AssertionError('request reloaded artifacts')):
            for url in ('/health','/api/metadata','/api/predictions/players','/api/predictions/intervals','/api/predictions/games','/api/predictions/players/101/history'):
                self.assertEqual(self.client.get(url).status_code,200)
        with closing(self.app.state.predictions.connect()) as connection:
            with self.assertRaises(sqlite3.OperationalError):connection.execute('DELETE FROM games')

    def test_warm_start_reuses_index_but_still_checks_source_integrity(self):
        path=self.app.state.predictions.path;modified=path.stat().st_mtime_ns
        with patch('nhl_analytics.prediction_store.csv_rows',side_effect=AssertionError('cache rebuilt')):
            store=PredictionStore.load(self.run,self.cache)
        self.assertEqual(store.path,path);self.assertEqual(path.stat().st_mtime_ns,modified)
        with (self.run/'player_intervals.csv').open('a') as stream:stream.write('\n')
        with self.assertRaisesRegex(ArtifactError,'changed'):PredictionStore.load(self.run,self.cache)

    def test_metadata_docs_and_no_mutation_routes(self):
        result=self.client.get('/api/metadata').json()
        self.assertEqual(result['elo_parameters']['elo_k'],10)
        self.assertNotIn(str(self.root),json.dumps(result))
        self.assertIn('stale',result)
        self.assertEqual(self.client.get('/docs').status_code,200)
        self.assertEqual(self.client.get('/openapi.json').status_code,200)
        self.assertEqual(self.client.post('/api/predictions/players').status_code,405)

    def test_invalid_interval_is_rejected_even_with_updated_checksum(self):
        path=self.run/'player_intervals.csv'
        with path.open(newline='') as stream:rows=list(csv.DictReader(stream))
        rows[-1]['lower']=-1
        write_csv(path,rows)
        manifest=json.loads((self.run/'manifest.json').read_text())
        manifest['files'][path.name]['sha256']=checksum(path)
        write_json(self.run/'manifest.json',manifest)
        with self.assertRaisesRegex(ArtifactError,'bounds'):PredictionStore.load(self.run,self.cache)
        self.assertFalse(list(self.cache.glob('*.tmp')))

    def test_mixed_model_clock_and_missing_bundle_fail_without_fallback_training(self):
        path=self.run/'selected_elo_model.json'
        model=json.loads(path.read_text());model['as_of']='2026-11-11T19:00:00Z';write_json(path,model)
        manifest=json.loads((self.run/'manifest.json').read_text());manifest['files'][path.name]['sha256']=checksum(path)
        write_json(self.run/'manifest.json',manifest)
        with self.assertRaisesRegex(ArtifactError,'clocks'):PredictionStore.load(self.run,self.cache)
        with self.assertRaises(ArtifactError):PredictionStore.load(self.root/'missing',self.cache)

    def test_stale_flag_changes_with_wall_clock_without_refreshing_predictions(self):
        with patch('nhl_analytics.api.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime(2026,11,12,19,tzinfo=timezone.utc)
            response=self.client.get('/api/metadata').json()
        self.assertTrue(response['stale'])
        self.assertEqual(response['age_hours'],48)
        self.assertTrue(response['as_of'].startswith('2026-11-10'))

    def test_optional_cors_allows_only_configured_origin(self):
        with patch.dict(os.environ,{'NHL_API_CORS_ORIGINS':'http://localhost:5173'}):
            app=create_app(self.run,self.cache)
        with TestClient(app) as client:
            allowed=client.get('/health',headers={'Origin':'http://localhost:5173'})
            other=client.get('/health',headers={'Origin':'https://example.invalid'})
        self.assertEqual(allowed.headers['access-control-allow-origin'],'http://localhost:5173')
        self.assertNotIn('access-control-allow-origin',other.headers)


if __name__=='__main__':unittest.main()
