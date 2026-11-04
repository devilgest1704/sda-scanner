import unittest
from unittest.mock import patch
from tests.test_paper_confirmed_continuation import NOW, ADDRESS, prior, candidate, ActivePaperEntryTests
from tools.temporal_recovery_buy import evaluate_tp4_legacy as evaluate_tp4, evaluate_paper
from tools.paper_quote_quality import evaluate
from tests.test_paper_quote_quality import tx, T
import v30_pump_hunter as v

class SparsePaperTests(unittest.TestCase):
 def test_conditional_sparse_lane_keeps_baseline_strict(self):
  c=candidate(trades_1h=4,score=65,m1h=0)
  self.assertTrue(evaluate_tp4(ADDRESS,c,at=NOW,previous=prior())["ready"])
  self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=prior())["ready"])
  for changes in [dict(trades_1h=1),dict(volume_1h=699),dict(flow_1h=199),dict(flow_15m=59),dict(impulse=0),dict(score=59),dict(m1h=-6),dict(price_sda=.98)]:
   with self.subTest(changes=changes):
    self.assertFalse(evaluate_tp4(ADDRESS,dict(c,**changes),at=NOW,previous=prior())["ready"])
 def test_two_quotes_only_confirmed_lane_still_requires_continuity(self):
  rows=[tx(700),tx(150)]
  self.assertFalse(evaluate(rows,T,rows[-1]["timestamp"])["ready"])
  self.assertTrue(evaluate(rows,T,rows[-1]["timestamp"],confirmed_continuation=True)["ready"])
  for rows in [[tx(20)]*3,[tx(30),tx(20)],[tx(700),tx(301)]]:
   self.assertFalse(evaluate(rows,T,rows[-1]["timestamp"],confirmed_continuation=True)["ready"])
 def test_outer_activity_allows_verified_sparse_only(self):
  for verified in [False,True]:
   result=dict(ready=True,lane="confirmed_continuation",reason="test",sparse_activity_verified=verified)
   with patch.object(v,"PAPER_TP4",True),patch.object(v,"strict_temporal_signal",return_value=result),patch.object(v,"quote_quality_gate",return_value=dict(ready=True,reason="test")):
    d=ActivePaperEntryTests().decide(trades=4)
    self.assertEqual(d["eligible_for_buy"],verified)
