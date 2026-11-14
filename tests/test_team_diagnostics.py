import copy
import tempfile
import unittest
from pathlib import Path

from nhl_analytics.evaluation_support import save_csv
from nhl_analytics.team_diagnostics import correlation, proxy_label, load_predictions, cohort_metrics
from nhl_analytics.team_ablation import ADDITIONS


class DiagnosticTests(unittest.TestCase):
    def test_correlations_skip_missing_and_handle_constant_inputs(self):
        self.assertAlmostEqual(correlation([(1,2),(2,4),(None,8)])['pearson'],1.)
        self.assertIsNone(correlation([(1,2),(1,4)])['pearson'])
        self.assertEqual(correlation([]),{'n':0,'pearson':None})

    def test_postgame_proxy_labels_do_not_modify_features(self):
        row=dict(game_id=1,home_proxy_goalie_id='99',away_proxy_goalie_id='88')
        original=copy.deepcopy(row)
        self.assertEqual(proxy_label(row,{(1,'home'):{99},(1,'away'):{88}}),'both_correct')
        self.assertEqual(proxy_label(row,{(1,'home'):{98},(1,'away'):{88}}),'at_least_one_wrong')
        self.assertEqual(proxy_label(row,{(1,'home'):{98,99},(1,'away'):{88}}),'unknown')
        self.assertEqual(row,original)

    def test_holdout_predictions_are_rejected(self):
        with tempfile.TemporaryDirectory(dir='.work/tmp') as root:
            path=Path(root)/'predictions.csv'
            save_csv(path,[dict(game_id=2025020001,season_id=20252026,home_win=1)])
            with self.assertRaisesRegex(ValueError,'Nondevelopment'):load_predictions(path)

    def test_cohorts_compare_exact_same_games(self):
        predictions=[dict(game_id=1,home_win=1,baseline_context=.5,context_plus_xg=.6,context_plus_goalie=.4)]
        feature=dict(home_season_games=2,away_season_games=3)
        feature.update({k:None for values in ADDITIONS.values() for k in values})
        result=cohort_metrics(predictions,{1:feature},{1:'unknown'})
        row=next(r for r in result if r['candidate']=='context_plus_xg' and r['dimension']=='input_coverage')
        self.assertEqual(row['n'],1);self.assertEqual(row['group'],'missing_added_input')
        self.assertAlmostEqual(row['baseline_brier'],.25)
        self.assertAlmostEqual(row['candidate_brier'],.16)


if __name__=='__main__':unittest.main()
