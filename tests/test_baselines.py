import copy
import json
import tempfile
import unittest
from datetime import date,datetime,timedelta,timezone
from pathlib import Path

from nhl_analytics.baselines import (elo_predictions,elo_probability,player_projection,
    player_datasets,probability_metrics,regression_metrics,validate_history)
from nhl_analytics.database import timestamp
from nhl_analytics.reconstructed import assumed_available,load_history


def game(identifier,day,home=1,away=2,won=True):
    return dict(game_id=identifier,season_id=20212022,game_date=day,
        start_time_utc=datetime.combine(day,datetime.min.time(),timezone.utc)+timedelta(hours=23),
        home_team_id=home,away_team_id=away,home_team_abbrev='AAA',away_team_abbrev='BBB',
        is_final=True,winner_team_id=home if won else away,retrieved_at=timestamp('2026-11-10T12:00Z'),
        assumed_available_at=assumed_available(day))


class BaselineTests(unittest.TestCase):
    def test_next_day_noon_eastern_handles_dst(self):
        self.assertEqual(assumed_available(date(2024,1,1)),timestamp('2024-01-02T17:00Z'))
        self.assertEqual(assumed_available(date(2024,7,1)),timestamp('2024-07-02T16:00Z'))

    def test_projection_attendance_and_scoring(self):
        current=[dict(goals=1,assists=2,points=3)]*10
        prior=[dict(goals=0,assists=1,points=1)]*20
        p=player_projection(current,prior,20,60)
        self.assertEqual(p['projected_games'],40)
        self.assertEqual(p['pace_points'],120)
        self.assertEqual(p['prior_shrinkage_points'],90)
        self.assertEqual(p['prior_shrinkage_goals']+p['prior_shrinkage_assists'],p['prior_shrinkage_points'])

    def test_missing_prior_reduces_to_pace(self):
        p=player_projection([dict(goals=1,assists=1,points=2)]*5,[],10,70)
        self.assertEqual(p['pace_points'],p['prior_shrinkage_points'])

    def test_own_outcome_does_not_change_own_elo_forecast(self):
        games=[game(1,date(2021,10,1)),game(2,date(2021,10,3)),game(3,date(2021,10,5))]
        now=timestamp('2022-07-01T00:00Z')
        before,_=elo_predictions(games,now)
        revised=copy.deepcopy(games)
        revised[1]['winner_team_id']=2
        after,_=elo_predictions(revised,now)
        self.assertEqual(before[1]['home_win_probability'],after[1]['home_win_probability'])
        self.assertNotEqual(before[2]['home_win_probability'],after[2]['home_win_probability'])
        for row in before:
            self.assertTrue(row['max_assumed_available_at'] is None or row['max_assumed_available_at']<row['prediction_at'])

    def test_result_at_cutoff_is_not_available(self):
        first=game(1,date(2021,10,1))
        second=game(2,date(2021,10,2))
        second['start_time_utc']=first['assumed_available_at']+timedelta(hours=1)
        rows,_=elo_predictions([first,second],timestamp('2022-07-01T00:00Z'))
        self.assertEqual(rows[1]['games_updated_before_prediction'],0)
        self.assertEqual(rows[0]['home_win_probability'],rows[1]['home_win_probability'])

    def test_offseason_regression_and_utah_continuity(self):
        first=game(1,date(2023,10,1),home=53)
        first['season_id']=20232024
        second=game(2,date(2024,10,1),home=59)
        second['season_id']=20242025
        third=game(3,date(2025,10,1),home=68)
        third['season_id']=20252026
        rows,ratings=elo_predictions([first,second,third],timestamp('2026-07-01T00:00Z'))
        self.assertGreater(rows[1]['home_rating'],1500)
        self.assertLess(rows[1]['home_rating']-1500,20*(1-rows[0]['home_win_probability']))
        self.assertEqual(rows[1]['baseline_training_games'],1)
        self.assertGreater(rows[2]['home_rating'],rows[1]['home_rating'])
        self.assertNotIn(59,ratings)
        self.assertNotIn(68,ratings)

    def test_future_labels_do_not_change_player_inputs_or_predictions(self):
        games=[game(i,date(2021,10,20)+timedelta(days=i)) for i in range(1,6)]
        games.append(game(6,date(2021,11,20)))
        skaters=[dict(player_id=9,game_id=g['game_id'],season_id=g['season_id'],game_date=g['game_date'],
            assumed_available_at=g['assumed_available_at'],home_road='H',goals=1,assists=1,points=2) for g in games]
        data=dict(games=games,skaters=skaters)
        inputs,labels,predictions=player_datasets(data,[20212022],timestamp('2026-11-10T12:00Z'))
        data=copy.deepcopy(data)
        data['skaters'][-1].update(goals=99,assists=99,points=198)
        inputs2,labels2,predictions2=player_datasets(data,[20212022],timestamp('2026-11-10T12:00Z'))
        self.assertTrue(inputs)
        self.assertEqual(inputs,inputs2)
        self.assertEqual(predictions,predictions2)
        self.assertNotEqual(labels,labels2)
        self.assertLess(inputs[0]['max_assumed_available_at'],inputs[0]['prediction_at'])
        self.assertFalse(any(k.startswith('final_') for k in inputs[0]))

    def test_metrics_known_values(self):
        self.assertEqual(regression_metrics([(1,2),(3,2)])['rmse'],1)
        self.assertEqual(probability_metrics([(0.5,1),(0.5,0)])['brier'],0.25)
        self.assertAlmostEqual(elo_probability(1500,1500,0),0.5)

    def test_missing_backfill_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'Missing schedules'):
            validate_history(dict(games=[],skaters=[],goalies=[]),[20212022])

    def test_strict_export_cannot_be_consumed_as_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'manifest.json').write_text('{"status":"complete","availability_policy":"strict"}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'not strict'):
                load_history(path)

    def test_modified_snapshot_is_rejected_before_model_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'games.parquet').write_bytes(b'changed content')
            (path/'manifest.json').write_text(json.dumps(dict(status='complete',
                data_mode='reconstructed_latest_corrected',artifacts={'games':dict(rows=1,sha256='wrong')})),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Changed reconstruction'):
                load_history(path)


if __name__=='__main__':
    unittest.main()
