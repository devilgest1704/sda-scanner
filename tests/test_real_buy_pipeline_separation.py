import tempfile, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from tools import real_buy_pipeline as p
import real_trading_config as cfg

class RealPaperIsolation(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"real.json"
  self.at=datetime.now(timezone.utc); self.state=p.initial_state()
  self.real={"positions":{f"0x{i:040x}":{"cost_sda":30} for i in range(7)},
             "pending_buys":{},"daily_realized_pnl_sda":0}
  self.analysis={"price_in_sda":1,"last_transaction":self.at.isoformat(),
   "momentum":{"1h_pct":3,"15m_pct":2,"4h_pct":1},
   "flow":{"1h":{"buy_volume":900,"sell_volume":100,"buy_count":8,"sell_count":4,"net_flow":800},
           "15m":{"net_flow":200}}}
  self.token="0x"+"a"*40
 def tearDown(self): self.tmp.cleanup()
 def ready(self):
  temporal={"ready":True,"reason":"TEST","lane":"confirmed_continuation","sparse_activity_verified":True}
  score=({"last_transaction":self.at.isoformat(),"data_age_sec":10,"price":1,"m1":3,"m15":2,"m4":1,
   "flow":800,"flow15":200,"vol":1000,"trades":12,"buy_ratio":9,"trade_ratio":2,"accel":1},80,70,10,"CONFIRMATION")
  with patch.object(p.strategy,"score",return_value=score),patch.object(p,"evaluate_tp4",return_value=temporal),       patch.object(p,"_capacity",return_value=(True,"REAL_CAPACITY_AVAILABLE",{"positions":7})),       patch.object(p,"_market_gates",return_value={"blocked":False,"reasons":[]}):
   return p.evaluate_candidate(self.token,self.analysis,self.state,self.real,self.at)
 def test_paper_full_and_cooldown_are_irrelevant(self):
  self.assertTrue(self.ready()["ready"])
 def test_paper_position_and_loss_circuit_are_irrelevant(self):
  self.assertTrue(self.ready()["ready"])
 def test_real_cooldown_blocks(self):
  self.state["cooldowns"][self.token]=(self.at+timedelta(hours=1)).isoformat()
  self.assertFalse(self.ready()["ready"])
 def test_real_ten_positions_blocks(self):
  self.state["cooldowns"].clear()
  with patch.object(p,"_capacity",return_value=(False,"REAL_POSITION_LIMIT",None)):
   self.assertFalse(self.ready()["ready"])
 def test_real_capital_blocks(self):
  with patch.object(p,"_capacity",return_value=(False,"REAL_CAPITAL_LIMIT",None)):
   self.assertFalse(self.ready()["ready"])
 def test_real_loss_circuit_blocks(self):
  self.real["daily_realized_pnl_sda"]=-cfg.MAX_DAILY_LOSS_SDA
  self.assertFalse(self.ready()["ready"])
 def test_restart_preserves_temporal_history(self):
  self.state["temporal_history"][self.token]={"score":70}
  p.save_state(self.state,self.path); loaded,error=p.load_state(self.path)
  self.assertIsNone(error); self.assertEqual(loaded["temporal_history"],self.state["temporal_history"])
 def test_corrupt_state_fails_closed(self):
  self.path.write_text("{",encoding="utf-8")
  loaded,error=p.load_state(self.path); self.assertIsNone(loaded); self.assertEqual(error,"REAL_SIGNAL_STATE_UNREADABLE")

 def test_full_dry_run_path_returns_would_execute(self):
  order=[]
  market={"tokens":{self.token:self.analysis}}
  market_path=Path(self.tmp.name)/"market.json"
  market_path.write_text(__import__("json").dumps(market),encoding="utf-8")
  with patch.dict("os.environ",{"REAL_TRADING_ENABLED":"true","REAL_TRADING_DRY_RUN":"true"}),\
       patch.object(p,"load_state",return_value=(self.state,None)),\
       patch.object(p.real_trader,"load_state",return_value=self.real),\
       patch.object(p,"evaluate_candidate",return_value={"ready":True,"signal_id":"sid"}),\
       patch.object(p.real_trader,"safety_check",side_effect=lambda *a:order.append("SAFETY_PASS")),\
       patch.object(p.real_trader,"execute_buy",side_effect=lambda *a: (order.append("EXECUTE"),{"status":"DRY_RUN"})[1]),\
       patch.object(p,"save_state"):
   result=p.run_scan(market_path=str(market_path),execute=True,state_path=self.path)
  self.assertEqual(order,["SAFETY_PASS","EXECUTE"])
  self.assertEqual(result["actions"][0]["status"],"WOULD EXECUTE")

 def test_executor_dry_run_contract(self):
  with patch.object(p.real_trader,"safety_check") as safety,patch.object(p.real_trader,"execute_buy",return_value={"status":"DRY_RUN"}):
   p.real_trader.safety_check(30,self.real); out=p.real_trader.execute_buy(self.token,"T",30)
   safety.assert_called_once(); self.assertEqual(out["status"],"DRY_RUN")

if __name__=="__main__": unittest.main()
