import unittest
from datetime import datetime,timedelta,timezone

from nhl_analytics.phase2_goalies import GoalieHistory,normalise,shrunk_save_rate,mixture,OTHER

NOW=datetime(2023,10,20,17,tzinfo=timezone.utc)


class GoalieResearchTests(unittest.TestCase):
    def record(self,player=11,gid=1,available=None):
        return dict(player_id=player,game_id=gid,season_id=20232024,team=1,game_date=NOW.date()-timedelta(days=2),
            start=NOW-timedelta(days=2),available=available or NOW-timedelta(days=1),started=1,shots=30,saves=27,minutes=60)
    def test_probabilities_and_unknown_are_normalised_without_future(self):
        first=self.record();future=self.record(99,2,NOW)
        plain=GoalieHistory([first]);changed=GoalieHistory([first,future])
        a=plain.selection(20232024,1,NOW,3,NOW.date())[0]
        b=changed.selection(20232024,1,NOW,3,NOW.date())[0]
        self.assertEqual(a,b);self.assertAlmostEqual(sum(a.values()),1.)
        self.assertGreater(a[OTHER],0);self.assertNotIn(99,a)
        target=self.record(101,3)
        self.assertEqual(GoalieHistory([first,target]).selection(20232024,1,NOW,3,NOW.date())[0],a)
    def test_save_shrinkage_and_cold_start(self):
        self.assertAlmostEqual(shrunk_save_rate(0,0,0,0),.9)
        self.assertAlmostEqual(shrunk_save_rate(90,100,180,200),.9)
        value=shrunk_save_rate(1,1,0,0)
        self.assertGreater(value,.9);self.assertLess(value,1)
        with self.assertRaises(ValueError):shrunk_save_rate(5,2,0,0)
    def test_game_mixture_requires_explicit_assumptions_and_complete_pairs(self):
        home={1:.6,OTHER:.4};away={2:.7,OTHER:.3}
        condition={(h,a):.55 for h in home for a in away}
        with self.assertRaises(ValueError):mixture(home,away,condition)
        self.assertAlmostEqual(mixture(home,away,condition,assume_independence=True),.55)
        joint={(1,2):.5,(1,OTHER):.1,(OTHER,2):.2,(OTHER,OTHER):.2}
        self.assertAlmostEqual(mixture(home,away,condition,joint=joint),.55)
        del condition[OTHER,OTHER]
        with self.assertRaises(ValueError):mixture(home,away,condition,joint=joint)
    def test_bad_weights_and_future_season_rejected(self):
        with self.assertRaises(ValueError):normalise({1:-1})
        record=self.record();record['season_id']=20252026
        with self.assertRaises(ValueError):GoalieHistory([record])


if __name__=='__main__':unittest.main()
