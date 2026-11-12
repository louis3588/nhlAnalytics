import copy
import importlib.util
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nhl_analytics.baselines import elo_probability
from nhl_analytics.features import write_document
from nhl_analytics.pregame import FEATURES, HOLDOUT, build_dataset
from nhl_analytics.reconstructed import MODE, assumed_available, file_hash
from nhl_analytics.team_models import chronological_folds, create_dataset, evaluate, fit_logistic, matrix, paired_week_bootstrap, predict_saved, read_dataset

SKLEARN_AVAILABLE=importlib.util.find_spec('sklearn') is not None
AS_OF=datetime(2026,11,10,19,35,tzinfo=timezone.utc)


def small_games(seasons=(20212022,20222023,20232024,20242025,20252026,20262027),n=8):
    games=[];elo={};aggregates={}
    for season in seasons:
        year=season//10000
        for i in range(n):
            day=date(year,10,1)+timedelta(days=3*i)
            start=datetime(day.year,day.month,day.day,23,tzinfo=timezone.utc)
            gid=year*1000000+20000+i+1
            home,away=(1,2) if i%2==0 else (2,1)
            hs,aws=(3,1) if i%3==0 else (1,2)
            game=dict(game_id=gid,season_id=season,game_date=day,start_time_utc=start,
                home_team_id=home,away_team_id=away,home_team_abbrev='AAA' if home==1 else 'BBB',
                away_team_abbrev='AAA' if away==1 else 'BBB',home_score=hs,away_score=aws,
                winner_team_id=home if hs>aws else away,is_final=True,result_type='REG',
                assumed_available_at=assumed_available(day),retrieved_at=AS_OF,source_sha256='fixture')
            games.append(game)
            elo[gid]=dict(game_id=str(gid),season_id=str(season),prediction_at=(start-timedelta(hours=1)).isoformat(),
                data_mode=MODE,home_rating='1500',away_rating='1500',home_win_probability=str(elo_probability(1500,1500,25)),
                historical_home_win_probability='0.52',max_assumed_available_at='')
            for side,shots,ga in [('home',20+i,aws),('away',18+i,hs)]:
                aggregates[gid,side]=dict(shots=shots,goalie_shots=shots,saves=shots-ga)
    return games,elo,aggregates


class PregameFeatureTests(unittest.TestCase):
    def test_target_and_future_outcomes_cannot_enter_own_or_earlier_features(self):
        games,elo,aggregates=small_games(seasons=(20212022,),n=4)
        before,_=build_dataset(games,elo,aggregates,AS_OF)
        changed=copy.deepcopy(games)
        changed[2]['home_score']=10;changed[2]['away_score']=0;changed[2]['winner_team_id']=changed[2]['home_team_id']
        after,_=build_dataset(changed,elo,aggregates,AS_OF)
        self.assertEqual(before[:3],after[:3])
        self.assertNotEqual(before[3]['season_goal_diff_per_game_diff'],after[3]['season_goal_diff_per_game_diff'])
        self.assertNotIn('home_win',before[0]);self.assertNotIn('home_score',before[0])

    def test_strict_equality_exclusion_and_rest_before_result_publication(self):
        games,elo,aggregates=small_games(seasons=(20212022,),n=2)
        first,second=games
        second['game_date']=first['game_date']+timedelta(days=1)
        second['start_time_utc']=first['start_time_utc']+timedelta(days=1)
        second['assumed_available_at']=assumed_available(second['game_date'])
        cutoff=second['start_time_utc']-timedelta(hours=1)
        elo[second['game_id']]['prediction_at']=cutoff.isoformat()
        first['assumed_available_at']=cutoff
        rows,_=build_dataset(games,elo,aggregates,AS_OF)
        self.assertEqual(rows[1]['home_season_games'],0)
        self.assertEqual(rows[1]['home_back_to_back'],1)
        self.assertEqual(rows[1]['away_back_to_back'],1)
        self.assertIsNone(rows[1]['last10_goal_diff_per_game_diff'])

    def test_shootout_deciding_goal_does_not_inflate_scoring_rate(self):
        games,elo,aggregates=small_games(seasons=(20212022,),n=2)
        first=games[0];first.update(home_score=3,away_score=2,winner_team_id=first['home_team_id'],result_type='SO')
        rows,_=build_dataset(games,elo,aggregates,AS_OF)
        self.assertEqual(rows[1]['season_goal_diff_per_game_diff'],0)

    def test_arizona_utah_continuity_preserves_raw_team_ids(self):
        games,elo,aggregates=small_games(seasons=(20232024,20242025),n=1)
        games[0]['home_team_id']=53;games[0]['winner_team_id']=53
        games[1]['home_team_id']=59;games[1]['winner_team_id']=59
        rows,_=build_dataset(games,elo,aggregates,AS_OF)
        self.assertEqual(rows[1]['home_team_id'],59)
        self.assertEqual(rows[1]['home_prior_games'],1)
        self.assertEqual(rows[1]['home_season_games'],0)

    def test_label_cutoffs_and_holdout_exclusion_in_every_fold(self):
        games,elo,aggregates=small_games()
        rows,labels=build_dataset(games,elo,aggregates,AS_OF)
        labels[0]['label_available_at']='2024-12-01T00:00:00Z'
        for season,cutoff,train,test,truth in chronological_folds(rows,labels,AS_OF):
            self.assertTrue(all(r['season_id']<season and r['season_id']!=HOLDOUT for r in train))
            self.assertTrue(all(datetime.fromisoformat(truth[r['game_id']]['label_available_at'])<cutoff for r in train))
            if season==20222023:self.assertNotIn(rows[0]['game_id'],{r['game_id'] for r in train})


@unittest.skipUnless(SKLEARN_AVAILABLE,'Install models extra for regression tests')
class LogisticTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def test_training_only_preprocessing_and_portable_model_match(self):
        games,elo,aggregates=small_games(seasons=(20212022,),n=8)
        rows,labels=build_dataset(games,elo,aggregates,AS_OF)
        truth={r['game_id']:r['home_win'] for r in labels}
        pipeline,model=fit_logistic(rows,truth,FEATURES)
        untouched=copy.deepcopy(model)
        target=copy.deepcopy(rows[:1]);target[0]['rest_days_diff']=10000
        portable=predict_saved(model,target)
        reference=pipeline.predict_proba(matrix(target,FEATURES))[:,1]
        self.assertAlmostEqual(portable[0],reference[0],places=12)
        self.assertEqual(model,untouched)
        self.assertNotIn('home_win',model['feature_columns'])

    def test_holdout_labels_and_features_cannot_change_any_fitted_model(self):
        games,elo,aggregates=small_games()
        rows,labels=build_dataset(games,elo,aggregates,AS_OF)
        evaluate(rows,labels,self.root/'before',AS_OF,[20212022,20222023,20232024,20242025,HOLDOUT])
        altered=copy.deepcopy(labels)
        for row in altered:
            if row['season_id']==HOLDOUT:row['home_win']=1-row['home_win']
        altered_features=copy.deepcopy(rows)
        for row in altered_features:
            if row['season_id']==HOLDOUT:row['last10_goal_diff_per_game_diff']=10000
        evaluate(altered_features,altered,self.root/'after',AS_OF,[20212022,20222023,20232024,20242025,HOLDOUT])
        for path in (self.root/'before/models').glob('*.json'):
            self.assertEqual(path.read_bytes(),(self.root/'after/models'/path.name).read_bytes())

    def test_paired_bootstrap_identical_probabilities_and_sparse_weeks(self):
        rows=[dict(game_date=(date(2024,1,1)+timedelta(days=7*i)).isoformat(),home_win=i%2,model=0.6,frozen_elo=0.6) for i in range(12)]
        result=paired_week_bootstrap(rows,'model',replicates=50)
        self.assertEqual(result['brier_lower'],0);self.assertEqual(result['brier_upper'],0)
        self.assertIsNone(paired_week_bootstrap(rows[:2],'model',replicates=50)['brier_lower'])


class DatasetIntegrationTests(unittest.TestCase):
    def test_saved_dataset_is_reusable_separates_labels_and_verifies_sources(self):
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        root=Path(temporary.name).resolve();history=root/'history';baseline=root/'baseline';evaluation=root/'evaluation'
        for folder in (history,baseline,evaluation):folder.mkdir()
        games,elo,_=small_games(seasons=(20212022,20222023),n=4)
        skaters=[];goalies=[]
        for game in games:
            for side,flag in [('home','H'),('away','R')]:
                common=dict(game_id=game['game_id'],season_id=game['season_id'],home_road=flag,team_abbrev=game[side+'_team_abbrev'])
                for index in range(15):skaters.append(dict(common,player_id=index+(1 if side=='home' else 101),shots=2))
                goalies.append(dict(common,player_id=999 if side=='home' else 998,shots_against=30,saves=27))
        artifacts={}
        for name,values in [('games',games),('skaters',skaters),('goalies',goalies)]:
            path=history/f'{name}.parquet';pq.write_table(pa.Table.from_pylist(values),path)
            artifacts[name]=dict(rows=len(values),sha256=file_hash(path))
        write_document(history/'manifest.json',dict(status='complete',data_mode=MODE,game_type_id=2,artifacts=artifacts))
        hsha=file_hash(history/'manifest.json')
        write_document(baseline/'manifest.json',dict(status='complete',data_mode=MODE,history=str(history),history_manifest_sha256=hsha,
            as_of=AS_OF,completed_seasons=[20212022,20222023]))
        parameters=dict(elo_k=10,elo_home_advantage=25,elo_season_retention=0.75)
        write_document(evaluation/'elo_selection.json',dict(parameters=parameters,excluded_holdout_from_selection=True))
        write_document(evaluation/'selected_elo_model.json',dict(parameters=parameters,data_mode=MODE,as_of=AS_OF,selection_sha256=file_hash(evaluation/'elo_selection.json')))
        from nhl_analytics.evaluation_support import save_csv
        save_csv(evaluation/'selected_elo_predictions.csv',elo.values())
        write_document(evaluation/'manifest.json',dict(status='complete',data_mode=MODE,holdout=HOLDOUT,as_of=AS_OF,
            original_baseline=str(baseline),original_manifest_sha256=file_hash(baseline/'manifest.json'),history_manifest_sha256=hsha,
            files={name:dict(sha256=file_hash(evaluation/name)) for name in ('selected_elo_model.json','selected_elo_predictions.csv','elo_selection.json')}))
        rows,labels,manifest=create_dataset(evaluation,root/'dataset')
        self.assertEqual(len(rows),8);self.assertEqual(len(labels),8)
        loaded,loaded_labels,_=read_dataset(root/'dataset')
        self.assertEqual(loaded[0]['game_id'],rows[0]['game_id'])
        self.assertEqual(len(loaded_labels),8)
        self.assertNotIn('home_win',pq.read_schema(root/'dataset/pregame_features.parquet').names)
        self.assertIn('home_win',pq.read_schema(root/'dataset/game_labels.parquet').names)
        with self.assertRaisesRegex(ValueError,'empty'):create_dataset(evaluation,root/'dataset')
        (history/'games.parquet').write_bytes(b'changed')
        with self.assertRaises(ValueError):read_dataset(root/'dataset')


if __name__=='__main__':unittest.main()
