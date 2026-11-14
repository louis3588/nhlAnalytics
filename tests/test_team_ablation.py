import json
import tempfile
import unittest
from pathlib import Path

from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.features import write_document
from nhl_analytics.pregame import FEATURES, HOLDOUT
from nhl_analytics.reconstructed import MODE, file_hash
from nhl_analytics.team_ablation import design, load_inputs, evaluate
from nhl_analytics.team_feature_audit import CANDIDATE


class AblationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='.work/tmp');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.dataset=self.root/'dataset';self.dataset.mkdir()
        self.model=self.root/'model';self.model.mkdir();self.candidate=self.root/'candidate';self.candidate.mkdir()
    def fixture(self,late_source=False):
        base=dict.fromkeys(FEATURES,0)
        base.update(game_id=2021020001,season_id=20212022,home_team_id=1,away_team_id=2,
            prediction_at='2021-10-01T17:00:00+00:00',data_mode=MODE,frozen_elo_probability=.5)
        held=dict(base,game_id=2025020001,season_id=HOLDOUT,elo_log_odds='DO_NOT_PARSE')
        feature_artifact=save_csv(self.dataset/'pregame_features.csv',[base,held])
        labels=[dict(game_id=base['game_id'],season_id=base['season_id'],home_win=1,label_available_at='2021-10-02T16:00:00+00:00',data_mode=MODE),
                dict(game_id=held['game_id'],season_id=HOLDOUT,home_win='DO_NOT_PARSE',label_available_at='DO_NOT_PARSE',data_mode=MODE)]
        label_artifact=save_csv(self.dataset/'game_labels.csv',labels)
        write_document(self.dataset/'manifest.json',dict(feature_columns=FEATURES,artifacts={'pregame_features':{'csv':feature_artifact},'game_labels':{'csv':label_artifact}}))
        write_document(self.model/'manifest.json',dict(dataset=str(self.dataset)))
        candidate=dict(base,**dict.fromkeys(CANDIDATE,.1))
        for side in ('home','away'):
            candidate[side+'_max_feature_available_at']=base['prediction_at'] if late_source else '2021-09-30T16:00:00+00:00'
            candidate[side+'_source_game_ids']='[]'
        held_candidate=dict(candidate,**held);held_candidate[CANDIDATE[0]]='DO_NOT_PARSE'
        save_csv(self.candidate/'candidate_features_reconstructed.csv',[candidate,held_candidate])
        write_document(self.candidate/'summary.json',dict(source_model_manifest_sha256=file_hash(self.model/'manifest.json'),
            source_dataset_manifest_sha256=file_hash(self.dataset/'manifest.json'),candidate_data_mode=MODE,models_fitted=False))
    def test_plan_is_two_separate_fixed_additions_without_holdout(self):
        plan=design()
        self.assertEqual(set(plan['models']),{'context_plus_xg','context_plus_goalie'})
        self.assertNotIn(HOLDOUT,plan['development_seasons'])
        self.assertFalse(plan['production_models_changed'])
    def test_holdout_values_and_labels_are_filtered_before_conversion(self):
        self.fixture()
        rows,labels,_,_=load_inputs(self.model,self.candidate)
        self.assertEqual(len(rows),1);self.assertEqual(len(labels),1)
        self.assertEqual(rows[0]['season_id'],20212022)
    def test_candidate_cutoff_boundary_is_rejected(self):
        self.fixture(late_source=True)
        with self.assertRaisesRegex(ValueError,'reaches cutoff'):load_inputs(self.model,self.candidate)
    def test_evaluator_refuses_holdout_before_fitting(self):
        with self.assertRaisesRegex(ValueError,'forbidden'):
            evaluate([dict(season_id=HOLDOUT)],[],{},self.model,{},self.root)
