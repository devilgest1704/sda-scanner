import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from tools.paper_quote_quality import evaluate,entry_gate
from tools.paper_strategy_stats import cohort
T=datetime(2026,10,30,12,tzinfo=timezone.utc)
def tx(seconds,price=1):return dict(timestamp=(T-timedelta(seconds=seconds)).isoformat(),price=price)
class QualityTests(unittest.TestCase):
 def test_regular_quotes_pass(self):self.assertTrue(evaluate([tx(800),tx(400),tx(20)],T,tx(20)['timestamp'])['ready'])
 def test_single_burst_does_not_hide_prior_gap(self):self.assertEqual(evaluate([tx(30),tx(20),tx(10)],T,tx(10)['timestamp'])['reason'],'quote_history_gap')
 def test_duplicates_are_not_independent_quotes(self):self.assertFalse(evaluate([tx(20)]*5,T,tx(20)['timestamp'])['ready'])
 def test_future_quote_cannot_pass(self):self.assertFalse(evaluate([tx(800),tx(400),tx(-1)],T,tx(-1)['timestamp'])['ready'])
 def test_stale_quote_and_mismatched_quote_fail(self):
  self.assertFalse(evaluate([tx(800),tx(500),tx(400)],T,tx(400)['timestamp'])['ready'])
  self.assertEqual(evaluate([tx(800),tx(400),tx(20)],T,tx(10)['timestamp'])['reason'],'current_quote_history_mismatch')
 def test_snapshot_age_and_token_binding(self):
  with patch('tools.paper_quote_quality.market_snapshot',return_value={'updated_at':T.isoformat(),'tokens':{'a':{'transactions':[tx(800),tx(400),tx(20)]}}}):
   self.assertTrue(entry_gate('a',tx(20)['timestamp'],T)['ready']);self.assertFalse(entry_gate('b',tx(20)['timestamp'],T)['ready'])
  with patch('tools.paper_quote_quality.market_snapshot',return_value={'updated_at':tx(301)['timestamp']}):self.assertFalse(entry_gate('a',tx(20)['timestamp'],T)['ready'])
 def test_invalid_and_unknown_results_remain_in_total(self):
  rows=[dict(paper_strategy='temporal_recovery_tp4',address='a',closed_at=str(i),closed_profit_sda=v,**q) for i,(v,q) in enumerate([(2,{'paper_quote_quality_valid':True}),(-5,{'paper_quote_quality_valid':False}),(-3,{})])]
  s=cohort({'closed_trades':rows,'positions':{}});self.assertEqual(s['net_sda'],-6);self.assertEqual(s['drawdown_sda'],8);self.assertEqual(s['quality_unknown'],1);self.assertEqual(s['quality_invalid'],1)

class RuntimeGateTests(unittest.TestCase):
 def test_ready_temporal_entry_cannot_bypass_quote_quality(self):
  import v30_pump_hunter as v
  from test_temporal_recovery_paper_buy import ActualPaperGateTests
  for ready in (False,True):
   with patch.object(v,'PAPER_TP4',True),patch.object(v,'strict_temporal_signal',return_value={'ready':True,'reason':'NEW_FRESH_QUOTE_CONFIRMED'}),patch.object(v,'quote_quality_gate',return_value={'ready':ready,'reason':'test'}):
    d=ActualPaperGateTests().decision(True)
    self.assertEqual(d['eligible_for_buy'],ready)
    self.assertEqual('quote_continuity' in d['paper_buy_gate_failures'],not ready)
    self.assertEqual(d['paper_quote_quality']['ready'],ready)
