import unittest
from datetime import timedelta
from tests.test_paper_confirmed_continuation import NOW, ADDRESS, prior, candidate
from tools.temporal_recovery_buy import evaluate_tp4, evaluate_paper

class FifteenMinutePaperTests(unittest.TestCase):
 def test_zero_m15_and_fifteen_minute_quote_interval_only_tp4(self):
  p=prior(decision_at=(NOW-timedelta(seconds=850)).isoformat(),
          quote_at=(NOW-timedelta(seconds=920)).isoformat(),data_age_sec=70)
  c=candidate(quote_at=(NOW-timedelta(seconds=20)).isoformat(),data_age_sec=20,m15=0)
  self.assertTrue(evaluate_tp4(ADDRESS,c,at=NOW,previous=p)["ready"])
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
  for changes in [dict(m15=-.01),dict(price_sda=.98),dict(flow_15m=-1),
                  dict(data_age_sec=301),dict(quote_at=p["quote_at"])]:
   self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**changes),at=NOW,previous=p)["ready"])
  too_far=dict(p,quote_at=(NOW-timedelta(seconds=921)).isoformat(),data_age_sec=71)
  self.assertFalse(evaluate_tp4(ADDRESS,c,at=NOW,previous=too_far)["ready"])
 def test_current_quote_after_prior_decision_remains_required(self):
  p=prior(decision_at=(NOW-timedelta(seconds=100)).isoformat(),
          quote_at=(NOW-timedelta(seconds=150)).isoformat(),data_age_sec=50)
  c=candidate(quote_at=(NOW-timedelta(seconds=120)).isoformat(),data_age_sec=120,m15=0)
  self.assertFalse(evaluate_tp4(ADDRESS,c,at=NOW,previous=p)["ready"])

 def test_quote_continuity_accepts_actual_fifteen_minute_pair(self):
  from tools.paper_quote_quality import evaluate
  def tx(age):
   return dict(timestamp=(NOW-timedelta(seconds=age)).isoformat(),price=1)
  rows=[tx(920),tx(20)]
  self.assertTrue(evaluate(rows,NOW,rows[-1]["timestamp"],confirmed_continuation=True)["ready"])
  self.assertFalse(evaluate(rows,NOW,rows[-1]["timestamp"])["ready"])
  for rows in [[tx(921),tx(20)],[tx(30),tx(20)],[tx(1190),tx(301)],[tx(20)]*3]:
   self.assertFalse(evaluate(rows,NOW,rows[-1]["timestamp"],confirmed_continuation=True)["ready"])
