import unittest
from datetime import datetime, timedelta, timezone

from nhl_analytics.xg_strength import FEATURE, PRIOR_GAMES, design, strength, enrich, evaluate


class StrengthTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=datetime(2023,10,20,17,tzinfo=timezone.utc)
    def record(self,gid,net,days=2):
        return dict(game_id=gid,net_xg=net,start=self.cutoff-timedelta(days=days),available_at=self.cutoff-timedelta(days=days,hours=-1))
    def test_fixed_blend_and_opening_season_prior(self):
        prior=[self.record(1,1.,100),self.record(2,3.,90)]
        current=[self.record(3,4.)]
        result=strength(current,prior,100,self.cutoff)
        self.assertEqual(PRIOR_GAMES,20)
        self.assertAlmostEqual(result['value'],(4+20*2)/21)
        self.assertEqual(strength([],prior,100,self.cutoff)['value'],2.)
    def test_neutral_prior_is_explicit_and_does_not_use_future(self):
        current=[self.record(1,2.)]
        result=strength(current,[],100,self.cutoff)
        self.assertAlmostEqual(result['value'],2/21)
        self.assertEqual(result['prior_origin'],'neutral_zero_net_xg')
        self.assertEqual(result['prior_games'],0)
        at_cutoff=dict(self.record(2,1000.),available_at=self.cutoff)
        target=self.record(100,1000.)
        self.assertEqual(strength(current+[target,at_cutoff],[],100,self.cutoff),result)
    def test_missing_known_game_is_not_silently_discarded(self):
        result=strength([self.record(1,None)],[self.record(2,1.,100)],100,self.cutoff)
        self.assertIsNone(result['value']);self.assertEqual(result['current_games'],1)
        self.assertFalse(result['complete'])
    def test_feature_is_home_minus_away_and_preserves_baseline(self):
        row=dict(game_id=100,season_id=20232024,home_team_id=1,away_team_id=2,
            prediction_at=self.cutoff.isoformat(),max_assumed_feature_available_at=None,elo_log_odds=.25)
        history={(20222023,1):[self.record(1,2.,100)],(20222023,2):[self.record(2,-1.,100)]}
        result=enrich([row],history)[0]
        self.assertEqual(result[FEATURE],3.)
        self.assertEqual(result['elo_log_odds'],row['elo_log_odds'])
        self.assertNotIn(FEATURE,row)
    def test_holdout_rejected_before_fitting(self):
        with self.assertRaisesRegex(ValueError,'forbidden'):enrich([dict(season_id=20252026)],{})
        with self.assertRaisesRegex(ValueError,'forbidden'):evaluate([dict(season_id=20252026)],[],{}, {},None,None)
        self.assertNotIn(20252026,design()['evaluation_seasons'])
        self.assertEqual(len(design()['feature_columns']),23)


if __name__=='__main__':unittest.main()
