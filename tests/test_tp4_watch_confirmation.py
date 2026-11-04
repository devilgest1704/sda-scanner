import unittest
from datetime import timedelta
from tests.test_paper_confirmed_continuation import NOW, ADDRESS, prior, candidate
from tools.temporal_recovery_buy import evaluate_tp4, evaluate_paper

class RelaxedConfirmationTests(unittest.TestCase):
 def test_three_strong_trades_and_watch_prior_can_confirm(self):
  p=prior(phase="WATCH",score=55,impulse=0)
  c=candidate(trades_1h=3)
  r=evaluate_tp4(ADDRESS,c,at=NOW,previous=p)
  self.assertTrue(r["ready"],r)
  self.assertTrue(r["sparse_activity_verified"])
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
  for change in [dict(trades_1h=2),dict(volume_1h=699),dict(flow_1h=199),dict(flow_15m=59),dict(price_sda=.98),dict(impulse=0)]:
   self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**change),at=NOW,previous=p)["ready"])
 def test_older_current_quote_must_still_follow_prior_decision(self):
  p=prior(decision_at=(NOW-timedelta(seconds=400)).isoformat(),
          quote_at=(NOW-timedelta(seconds=450)).isoformat(),data_age_sec=50)
  c=candidate(data_age_sec=250,quote_at=(NOW-timedelta(seconds=250)).isoformat())
  self.assertTrue(evaluate_tp4(ADDRESS,c,at=NOW,previous=p)["ready"])
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
  for age in [301,-1]:
   c["data_age_sec"]=age;c["quote_at"]=(NOW-timedelta(seconds=age)).isoformat()
   self.assertFalse(evaluate_tp4(ADDRESS,c,at=NOW,previous=p)["ready"])
