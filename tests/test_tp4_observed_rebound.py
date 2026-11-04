"""PAPER v6 causal admission and unchanged baseline coverage."""
import unittest
from tests.test_paper_confirmed_continuation import NOW, ADDRESS, prior, candidate
from tools.temporal_recovery_buy import evaluate_tp4, evaluate_paper

class ReboundAdmissionTests(unittest.TestCase):
 def test_forming_watch_rebound_can_enter_only_paper_tp4(self):
  p=prior(phase="WATCH",score=48,impulse=0)
  c=candidate(phase="WATCH",score=60,quality=58,impulse=4,
              trades_1h=3,volume_1h=700,flow_1h=200,flow_15m=60,
              buy_ratio=1.5,m15=.5,m1h=-5)
  r=evaluate_tp4(ADDRESS,c,at=NOW,previous=p)
  self.assertTrue(r["ready"],r)
  self.assertTrue(r["sparse_activity_verified"])
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
  for kw in [dict(score=59),dict(quality=57),dict(impulse=3),
             dict(trades_1h=2),dict(volume_1h=699),dict(flow_1h=199),
             dict(flow_15m=59),dict(buy_ratio=1.49),dict(m15=-.01),
             dict(m1h=-5.01),dict(m15=8.01),dict(price_sda=.98),
             dict(quote_at=p["quote_at"]),dict(data_age_sec=301),
             dict(phase="NO")]:
   with self.subTest(kw=kw):
    self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**kw),at=NOW,previous=p)["ready"])
 def test_eight_to_eleven_trades_propagate_admission(self):
  for count in [8,11]:
   c=candidate(score=60,quality=58,impulse=4,trades_1h=count,
               volume_1h=500,flow_1h=150,flow_15m=30,buy_ratio=1.3,m15=.5,m1h=0)
   r=evaluate_tp4(ADDRESS,c,at=NOW,previous=prior())
   self.assertTrue(r["ready"],r)
   self.assertTrue(r["sparse_activity_verified"])
   self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=prior())["ready"])

 def test_five_to_seven_trade_volume_floor_keeps_other_guards(self):
  p=prior(phase="WATCH",score=48,impulse=0)
  for count in [3,4,5,7]:
   c=candidate(phase="WATCH",score=60,quality=58,impulse=4,
               trades_1h=count,volume_1h=500,flow_1h=200,flow_15m=60,
               buy_ratio=1.5,m15=.5,m1h=0)
   with self.subTest(count=count):
    self.assertEqual(evaluate_tp4(ADDRESS,c,at=NOW,previous=p)["ready"],count>=5)
    self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
    if count>=5:
     for changes in [dict(volume_1h=499),dict(flow_1h=199),dict(flow_15m=59),
                     dict(buy_ratio=1.49),dict(impulse=3),dict(price_sda=.98),
                     dict(quote_at=p["quote_at"]),dict(data_age_sec=301)]:
      self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**changes),at=NOW,previous=p)["ready"])
