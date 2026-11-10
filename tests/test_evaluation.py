import copy
import csv
import json
import tempfile
import unittest
from datetime import date,datetime,timedelta,timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nhl_analytics.baselines import elo_predictions
from nhl_analytics.database import timestamp
from nhl_analytics.elo_development import select_elo
from nhl_analytics.evaluation import run_evaluation
from nhl_analytics.evaluation_support import sample_key,save_csv
from nhl_analytics.features import write_document
from nhl_analytics.reconstructed import MODE,assumed_available,file_hash
from nhl_analytics.uncertainty import Coverage,finite_quantile,fit_pools,interval


def inputs():
    output={name:{} for name in ('player_inputs','player_labels','player_predictions')}
    for year in range(2021,2027):
        season=year*10000+year+1
        for month in (11,12):
            for player in range(100,150):
                row=dict(player_id=player,season_id=season,prediction_at=f'{year}-{month:02d}-01T17:00:00Z',data_mode=MODE,
                    games_played=1,goals=3,assists=2,points=5,team_games_played=1,team_games_remaining=1,
                    last_played_team_id=1,prior_games=0 if year==2021 else 2,prior_goals=6,prior_assists=6,prior_points=12,
                    max_assumed_available_at=f'{year}-10-02T16:00:00Z')
                key=sample_key(row)
                output['player_inputs'][key]=row
                output['player_predictions'][key]=dict(player_id=player,season_id=season,prediction_at=row['prediction_at'],data_mode=MODE,
                    projected_games=1.5,pace_goals=5,pace_assists=5,pace_points=10,
                    prior_shrinkage_goals=4,prior_shrinkage_assists=5,prior_shrinkage_points=9)
                output['player_labels'][key]=dict(player_id=player,season_id=season,prediction_at=row['prediction_at'],data_mode=MODE,
                    final_games=2,final_goals=6,final_assists=6,final_points=12)
    return output


def games():
    rows=[]
    for year in range(2021,2027):
        for month,calendar_year in ((10,year),(4,year+1)):
            day=date(calendar_year,month,1)
            rows.append(dict(game_id=len(rows)+1,season_id=year*10000+year+1,game_date=day,
                start_time_utc=datetime.combine(day,datetime.min.time(),timezone.utc)+timedelta(hours=23),
                assumed_available_at=assumed_available(day),home_team_id=1,away_team_id=2,
                home_score=2,away_score=1,is_final=True,result_type='REG',winner_team_id=1))
    return rows


class UncertaintyTests(unittest.TestCase):
    def test_finite_sample_rank_and_insufficient_tail(self):
        self.assertEqual(finite_quantile(range(1,20),0.95),19)
        self.assertIsNone(finite_quantile([1,2],0.95))

    def test_repeated_player_snapshots_count_once_and_holdout_never_fits(self):
        artifacts=inputs()
        available={s:max(g['assumed_available_at'] for g in games() if g['season_id']==s) for s in range(20212022,20272028,10001)}
        start=timestamp('2026-11-01T17:00Z')
        pool=fit_pools(artifacts,20262027,start,available,20252026)
        before=pool['pace','points','00-20',None]
        self.assertEqual(before['n_players'],50)
        for key,label in artifacts['player_labels'].items():
            if key[1]==20252026:label['final_points']=100000
        after=fit_pools(artifacts,20262027,start,available,20252026)
        self.assertEqual(pool,after)
        self.assertNotIn(20252026,before['seasons'])
        self.assertLess(before['latest'],start)

    def test_clamp_outward_rounding_and_sparse_fallback(self):
        artifacts=inputs()
        available={g['season_id']:g['assumed_available_at'] for g in games()}
        start=timestamp('2022-11-01T17:00Z')
        pools=fit_pools(artifacts,20222023,start,available,20252026)
        key=next(k for k in artifacts['player_inputs'] if k[1]==20222023)
        row=artifacts['player_inputs'][key]
        band=interval(row,artifacts['player_predictions'][key],'goals','pace',0.95,pools)
        self.assertTrue(band['available'])
        self.assertGreaterEqual(band['lower'],int(row['goals']))
        self.assertIn('prior=None',band['calibration_scope'])
        self.assertIsInstance(band['upper'],int)

    def test_labels_at_period_start_are_excluded(self):
        artifacts=inputs()
        start=timestamp('2022-11-01T17:00Z')
        self.assertFalse(fit_pools(artifacts,20222023,start,{20212022:start},20252026))

    def test_first_season_reports_missing_uncertainty(self):
        artifacts=inputs()
        key=next(iter(artifacts['player_inputs']))
        result=interval(artifacts['player_inputs'][key],artifacts['player_predictions'][key],'points','pace',0.8,{})
        self.assertFalse(result['available'])
        self.assertIsNone(result['lower'])

    def test_coverage_is_inclusive_and_player_weighted(self):
        c=Coverage()
        c.add(1,5,5,10);c.add(1,4,5,10);c.add(2,5,5,10)
        self.assertEqual(c.result()['coverage'],2/3)
        self.assertEqual(c.result()['player_weighted_coverage'],0.75)


class DevelopmentTests(unittest.TestCase):
    def test_holdout_and_later_outcomes_cannot_change_selection(self):
        original=games()
        now=timestamp('2027-06-01T00:00Z')
        before=select_elo(original,now,20252026)
        revised=copy.deepcopy(original)
        for row in revised:
            if row['season_id']>=20252026:row['winner_team_id']=2
        self.assertEqual(before,select_elo(revised,now,20252026))
        for fold in before[2]:
            self.assertTrue(all(int(s)<fold['evaluation_season'] for s in fold['selection_seasons'].split(',')))


class EvaluationIntegrationTests(unittest.TestCase):
    def test_complete_pipeline_preserves_source_and_publishes_additive_reports(self):
        Path('.work/tmp').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.work/tmp') as directory:
            root=Path(directory).resolve();history=root/'history';baseline=root/'baseline';output=root/'evaluation'
            history.mkdir();baseline.mkdir()
            g=games()
            pq.write_table(pa.Table.from_pylist(g),history/'games.parquet')
            pq.write_table(pa.Table.from_pylist([dict(game_id=r['game_id'],home_road='H',goals=2) for r in g]),history/'skaters.parquet')
            h=dict(status='complete',data_mode=MODE,availability_policy='test next-day assumption',
                artifacts={name:dict(sha256=file_hash(history/f'{name}.parquet')) for name in ('games','skaters')})
            write_document(history/'manifest.json',h)
            data=inputs()
            now=timestamp('2027-06-01T00:00Z')
            e,_=elo_predictions(g,now)
            data['elo_inputs_predictions']={r['game_id']:r for r in e}
            data['elo_labels']={r['game_id']:dict(game_id=r['game_id'],season_id=r['season_id'],data_mode=MODE,home_win=1) for r in g}
            files={name:save_csv(baseline/f'{name}.csv',rows.values()) for name,rows in data.items()}
            write_document(baseline/'manifest.json',dict(status='complete',data_mode=MODE,history=str(history),
                history_manifest_sha256=file_hash(history/'manifest.json'),as_of=now,completed_seasons=sorted({r['season_id'] for r in g}),artifacts=files))
            before={p.name:file_hash(p) for p in baseline.iterdir()}
            result=run_evaluation(baseline,output,plots=False)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(before,{p.name:file_hash(p) for p in baseline.iterdir()})
            self.assertTrue((output/'elo_selection.json').exists())
            self.assertTrue((output/'interval_coverage.csv').exists())
            self.assertEqual(json.loads((output/'manifest.json').read_text())['data_mode'],MODE)
            with (output/'player_intervals.csv').open(newline='') as stream:
                rows=list(csv.DictReader(stream))
            for row in rows:
                if row['available']=='True':
                    self.assertLess(timestamp(row['max_calibration_label_available_at']),timestamp(row['prediction_at']))
                    self.assertGreaterEqual(float(row['lower']),float(row['observed']))
                    self.assertNotIn('20252026',row['calibration_seasons'])
            with self.assertRaises(ValueError):run_evaluation(baseline,output,plots=False)
            # Even a correctly rehashed artifact must not admit a feature
            # observed exactly at the forecast instant.
            first=next(iter(data['player_inputs'].values()))
            first['max_assumed_available_at']=first['prediction_at']
            manifest=json.loads((baseline/'manifest.json').read_text())
            manifest['artifacts']['player_inputs']=save_csv(baseline/'player_inputs.csv',data['player_inputs'].values())
            write_document(baseline/'manifest.json',manifest)
            with self.assertRaisesRegex(ValueError,'availability'):
                run_evaluation(baseline,root/'invalid_evaluation',plots=False)


if __name__=='__main__':unittest.main()
