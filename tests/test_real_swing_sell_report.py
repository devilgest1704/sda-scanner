import unittest
from datetime import datetime, timezone
import real_swing_sell_report as report
TOKEN="0x"+"1"*40
NOW=datetime(2026,11,9,19,9,0,tzinfo=timezone.utc)
STATE={"sell_migration_observation_started_at":"2026-11-09T18:09:10Z","positions":{TOKEN:{
 "amount_sda":20,"remaining_cost_basis":"20","remaining_quantity":100*10**18,
 "token_amount_raw":100*10**18,"profit_partial_done":False,"trailing_armed":False}}}
META={TOKEN:{"symbol":"TEST","decimals":"18"}}
def analysis(price,stamp): return {"tokens":{TOKEN:{"price_in_sda":price,"last_transaction":stamp}}}
class ReadOnlySellReportTests(unittest.TestCase):
 def test_fresh_scanner_price(self):
  row=report.build_decision_rows(STATE,analysis(.205,"2026-11-09T19:08:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"HOLD"); self.assertAlmostEqual(row["pnl_pct"],2.5)
  self.assertEqual(row["price_source"],"scanner_market_analysis")
 def test_stale_scanner_price_unavailable(self):
  row=report.build_decision_rows(STATE,analysis(.30,"2026-11-09T18:50:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"PRICE_UNAVAILABLE"); self.assertIsNone(row["decision_price"])
 def test_partial_trigger(self):
  row=report.build_decision_rows(STATE,analysis(.25,"2026-11-09T19:08:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"PARTIAL_SELL_20")
 def test_emergency_exit(self):
  row=report.build_decision_rows(STATE,analysis(.10,"2026-11-09T19:08:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"FULL_SELL_EMERGENCY")
 def test_unresolved_intent_quarantines(self):
  state=dict(STATE); state["pending_sells"]={TOKEN:{"stage":"SWAP_SUBMITTED","status":"SUBMITTED"}}
  row=report.build_decision_rows(state,analysis(.25,"2026-11-09T19:08:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"SELL_QUARANTINED")
 def test_trailing_preview_does_not_mutate_state(self):
  state={"positions":{TOKEN:{"remaining_cost_basis":"20","remaining_quantity":100*10**18,
   "profit_partial_done":True,"trailing_armed":True,"highest_price_since_trailing_activation":"0.25"}}}
  before=dict(state["positions"][TOKEN])
  row=report.build_decision_rows(state,analysis(.27,"2026-11-09T19:08:00Z"),META,now=NOW)[0]
  self.assertEqual(row["action"],"TRAILING_HOLD"); self.assertEqual(row["current_peak"],.27)
  self.assertEqual(state["positions"][TOKEN],before)
if __name__=="__main__": unittest.main()
