import unittest
from datetime import datetime, timedelta, timezone
from nhl_analytics.team_feature_audit import context, audit_rows
from nhl_analytics.pregame import FEATURES


class TeamFeatureAuditTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=datetime(2026,11,12,tzinfo=timezone.utc)
        self.records=[dict(game_id=i,start=self.cutoff-timedelta(days=12-i),
            available_at=self.cutoff-timedelta(days=11-i),xgf=2.,xga=1.) for i in range(1,7)]
        self.goalies=[dict(r,player_id=99,started=1,toi_seconds=3600,saves=27,shots_against=30) for r in self.records]

    def test_target_and_exact_cutoff_sources_do_not_change_candidate(self):
        expected=context(self.records,self.goalies,100,self.cutoff)
        poisoned=dict(self.records[-1],game_id=100,xgf=1000)
        future=dict(self.records[-1],game_id=101,available_at=self.cutoff,xgf=1000)
        self.assertEqual(context(self.records+[poisoned,future],self.goalies,100,self.cutoff),expected)
        self.assertAlmostEqual(expected['last10_xg_share'],2/3)
        self.assertAlmostEqual(expected['proxy_goalie_last10_save_pct'],.9)
        self.assertEqual(expected['proxy_goalie_id'],99)

    def test_missing_game_is_not_silently_replaced_by_older_valid_game(self):
        records=[dict(r) for r in self.records];records[-1]['xgf']=None
        result=context(records,self.goalies,100,self.cutoff)
        self.assertEqual(result['xg_games'],6)
        self.assertEqual(result['xg_complete_games'],5)
        self.assertIsNone(result['last10_xg_share'])

    def test_unknown_goalie_and_short_history_remain_missing(self):
        result=context(self.records[:4],[],100,self.cutoff)
        self.assertIsNone(result['last10_xg_share'])
        self.assertIsNone(result['proxy_goalie_id'])
        self.assertIsNone(result['proxy_goalie_last7_minutes'])

    def test_audit_flags_future_sources_and_target_leakage(self):
        row=dict.fromkeys(FEATURES,0)
        row.update(game_id=5,season_id=20262027,prediction_at=self.cutoff.isoformat(),
            home_last_stats_available_at=self.cutoff.isoformat(),home_last_stats_game_id='5')
        missing,freshness,violations=audit_rows([row])
        self.assertEqual(len(violations),2)
        self.assertEqual(len(missing),len(FEATURES))


if __name__=='__main__':unittest.main()
