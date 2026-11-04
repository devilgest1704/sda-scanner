"""User-requested experimental two-trade PAPER entry."""
import unittest
from unittest.mock import patch
from tests.test_paper_confirmed_continuation import NOW, ADDRESS, prior, candidate
import tests.test_paper_confirmed_continuation as helpers
from tools.temporal_recovery_buy import evaluate_tp4_legacy as evaluate_tp4, evaluate_paper
import v30_pump_hunter as v

class TwoTradeProbeTests(unittest.TestCase):
 def sample(self):
  return candidate(phase="WATCH",score=40,quality=40,impulse=2,
                   trades_1h=2,volume_1h=300,flow_1h=200,flow_15m=60,
                   buy_ratio=2,m15=.1,m1h=0)
 def test_two_independent_quotes_and_strict_profile_boundaries(self):
  p=prior(phase="WATCH",score=40,impulse=0)
  c=self.sample();r=evaluate_tp4(ADDRESS,c,at=NOW,previous=p)
  self.assertTrue(r["ready"],r);self.assertEqual(r["entry_variant"],"two_trade_probe")
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)["ready"])
  for kw in [dict(trades_1h=1),dict(trades_1h=3),dict(score=39),dict(quality=39),
             dict(impulse=1),dict(volume_1h=299),dict(flow_1h=199),
             dict(flow_15m=59),dict(buy_ratio=1.99),dict(price_sda=.98),
             dict(quote_at=p["quote_at"]),dict(data_age_sec=301),dict(m15=-.01),
             dict(phase="NO")]:
   with self.subTest(kw=kw):
    self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**kw),at=NOW,previous=p)["ready"])
  self.assertFalse(evaluate_tp4(ADDRESS,c,at=NOW,previous={})["ready"])
 def test_outer_entry_and_risk_controls_still_apply(self):
  r=evaluate_tp4(ADDRESS,self.sample(),at=NOW,previous=prior(phase="WATCH",score=40,impulse=0))
  for kw in [{},{"loss":True},{"macd":True},{"cooldown":True}]:
   with patch.object(v,"PAPER_TP4",True),patch.object(v,"strict_temporal_signal",return_value=r),patch.object(v,"quote_quality_gate",return_value=dict(ready=True,reason="test")):
    d=helpers.ActivePaperEntryTests().decide(trades=2,**kw)
    self.assertEqual(d["eligible_for_buy"],not bool(kw))
