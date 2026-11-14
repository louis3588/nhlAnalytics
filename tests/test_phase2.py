import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from nhl_analytics.phase2_studies import OpponentRatings,simple_features,SIMPLE,specs
from nhl_analytics.research_registry import register,finish,verify_registry,amend,effective_result
from nhl_analytics.reconstructed import file_hash
from nhl_analytics.prospective_reports import forward_details


NOW=datetime(2023,10,20,17,tzinfo=timezone.utc)


class RegistryTests(unittest.TestCase):
    def test_corrections_preserve_original_and_verify_supplementary_hashes(self):
        with tempfile.TemporaryDirectory(dir='.work/tmp') as tmp:
            root=Path(tmp);register('study',{'hypothesis':'fixed'},root)
            finish('study',{'decision':'rejected'},root)
            original=file_hash(root/'study/result.json')
            source=root/'supplement';source.write_text('stability')
            amend('study',{'decision':'inconclusive'},'Correct derived label',{str(source):file_hash(source)},root)
            self.assertEqual(effective_result('study',root)['decision'],'inconclusive')
            self.assertEqual(file_hash(root/'study/result.json'),original)
            source.write_text('tampered')
            with self.assertRaises(ValueError):verify_registry('study',root)

    def test_specs_and_results_are_immutable_and_hash_checked(self):
        with tempfile.TemporaryDirectory(dir='.work/tmp') as tmp:
            root=Path(tmp);source=root/'source';source.write_text('original')
            spec={'hypothesis':'fixed','input_sha256':{str(source):file_hash(source)}}
            first=register('study',spec,root);self.assertEqual(register('study',spec,root),first)
            with self.assertRaises(ValueError):register('study',dict(spec,hypothesis='changed'),root)
            finish('study',{'decision':'inconclusive'},root);verify_registry('study',root)
            with self.assertRaises(FileExistsError):finish('study',{'decision':'promising'},root)
            source.write_text('changed')
            with self.assertRaises(ValueError):verify_registry('study',root)


class OpponentTests(unittest.TestCase):
    def record(self,gid=1,net=1.,available=None):
        return dict(game_id=gid,season_id=20232024,home=1,away=2,start=NOW-timedelta(days=2),available=available or NOW-timedelta(days=1),net=net)
    def test_hand_calculation_and_future_invariance(self):
        record=self.record()
        model=OpponentRatings([record],[1,2],penalty=1.,carry=.5)
        ratings,n=model.at(20232024,NOW)
        self.assertEqual(n,1);self.assertAlmostEqual(ratings[0],.25);self.assertAlmostEqual(ratings[1],-.25)
        future=self.record(2,1000.,NOW)
        altered=OpponentRatings([record,future],[1,2],penalty=1.)
        self.assertEqual(altered.at(20232024,NOW)[1],1)
        self.assertAlmostEqual(altered.at(20232024,NOW)[0][0],ratings[0])
    def test_prior_refresh_changes_cached_empty_current_season(self):
        prior=self.record(1,2.,NOW+timedelta(days=1));prior['season_id']=20222023
        prior['start']=NOW-timedelta(days=2)
        model=OpponentRatings([prior],[1,2],penalty=1.,carry=.5)
        before,_=model.at(20232024,NOW)
        after,_=model.at(20232024,NOW+timedelta(days=2))
        self.assertEqual(before[0],0);self.assertGreater(after[0],0)
    def test_no_holdout_sources(self):
        row=self.record();row['season_id']=20252026
        with self.assertRaises(ValueError):OpponentRatings([row],[1,2])


class RollingTests(unittest.TestCase):
    def test_rolling_uses_latest_matched_games_and_keeps_small_n_explicit(self):
        rows=[]
        for i in range(25):
            rows.append(dict(game_id=i,valid_for_scoring=True,scheduled_start=NOW+timedelta(minutes=i),
                game_date=NOW.date(),home_team_id=1,winner_team_id=1,elo_home_probability=.6,
                logistic_home_probability=.7 if i%2 else .4,status='sealed',timing='standard'))
        rows.append(dict(rows[0],game_id=99,valid_for_scoring=False))
        report=forward_details(rows,NOW)
        latest=[r for r in report['rolling_metrics'] if r['timing']=='all' and r['window_games']==20]
        self.assertEqual([r['n'] for r in latest],[20,20])
        self.assertFalse(latest[0]['paired_interval_available'])
        totals=next(r for r in report['model_disagreement'] if r['timing']=='all' and r['window_games']==0)
        self.assertEqual(totals['n'],25);self.assertEqual(totals['disagreements'],13)


if __name__=='__main__':unittest.main()
