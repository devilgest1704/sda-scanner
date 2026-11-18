import contextlib, tempfile, unittest
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
             "pending_buys":{},"daily_realized_pnl_sda":0,"syncStatus":"FRESH",
             "lastSuccessfulWalletSync":datetime.now(timezone.utc).isoformat()}
  self.analysis={"price_in_sda":1,"last_transaction":self.at.isoformat(),
   "momentum":{"1h_pct":3,"15m_pct":2,"4h_pct":1},
   "flow":{"1h":{"buy_volume":900,"sell_volume":100,"buy_count":8,"sell_count":4,"net_flow":800},
           "15m":{"net_flow":200}}}
  self.token="0x"+"a"*40
 def tearDown(self): self.tmp.cleanup()
 def ready(self, capacity=(True,"REAL_CAPACITY_AVAILABLE",{"positions":7})):
  temporal={"ready":True,"reason":"TEST","lane":"confirmed_continuation","sparse_activity_verified":True}
  score=({"last_transaction":self.at.isoformat(),"data_age_sec":10,"price":1,"m1":3,"m15":2,"m4":1,
   "flow":800,"flow15":200,"vol":1000,"trades":12,"buy_ratio":9,"trade_ratio":2,"accel":1},80,70,10,"CONFIRMATION")
  with patch.object(p.strategy,"score",return_value=score),patch.object(p,"evaluate_tp4",return_value=temporal),       patch.object(p,"_capacity",return_value=capacity),       patch.object(p,"_market_gates",return_value={"blocked":False,"reasons":[]}):
   return p.evaluate_candidate(self.token,self.analysis,self.state,self.real,self.at)

 def test_confirmation_uses_snapshot_impulse_but_ignition_requires_delta(self):
  c={"data_age_sec":60,"last_transaction":self.at.isoformat(),"price":1,
     "m1":5,"m15":3,"flow15":100,"flow":500,"vol":1000,"trades":10,"buy_ratio":2}
  confirmation=p._current_signal({},(c,70,65,0,"CONFIRMATION"),self.at)
  ignition=p._current_signal({},(c,70,65,0,"IGNITION"),self.at)
  self.assertGreater(confirmation["impulse"],0)
  self.assertEqual(confirmation["impulse"],confirmation["snapshot_impulse"])
  self.assertEqual(ignition["impulse"],0)
 def test_paper_full_and_cooldown_are_irrelevant(self):
  self.assertTrue(self.ready()["ready"])
 def test_paper_position_and_loss_circuit_are_irrelevant(self):
  self.assertTrue(self.ready()["ready"])
 def test_real_cooldown_blocks(self):
  self.state["cooldowns"][self.token]=(self.at+timedelta(hours=1)).isoformat()
  self.assertFalse(self.ready()["ready"])
 def test_real_ten_positions_blocks(self):
  self.state["cooldowns"].clear()
  self.assertFalse(self.ready((False,"REAL_POSITION_LIMIT",None))["ready"])
 def test_real_capital_blocks(self):
  self.assertFalse(self.ready((False,"REAL_CAPITAL_LIMIT",None))["ready"])

 def test_real_held_token_blocks(self):
  self.real["positions"][self.token]={"cost_sda":30}
  self.assertFalse(self.ready()["ready"])
 def test_real_pending_buy_blocks(self):
  self.real["pending_buys"][self.token]={"position_sda":30}
  self.assertFalse(self.ready()["ready"])

 def test_real_capacity_7_of_10_has_room_under_300_sda(self):
  with patch.object(p.real_trader,"wallet_address",return_value="0x"+"c"*40),\
       patch.object(p.real_trader,"native_balance",return_value=1000*10**18):
   ok,reason,capacity=p._capacity(self.real)
  self.assertTrue(ok); self.assertEqual(reason,"REAL_CAPACITY_AVAILABLE")
  self.assertEqual(capacity["positions"],7); self.assertEqual(capacity["deployed_sda"],210)
 def test_real_capacity_blocks_at_ten_positions(self):
  real=dict(self.real); real["positions"]={f"0x{i:040x}":{"cost_sda":20} for i in range(10)}
  ok,reason,_=p._capacity(real)
  self.assertFalse(ok); self.assertEqual(reason,"REAL_POSITION_LIMIT")
 def test_real_capacity_blocks_over_300_sda(self):
  real=dict(self.real); real["positions"]={f"0x{i:040x}":{"cost_sda":34} for i in range(8)}
  with patch.object(p.real_trader,"wallet_address",return_value="0x"+"c"*40),\
       patch.object(p.real_trader,"native_balance",return_value=1000*10**18):
   ok,reason,_=p._capacity(real)
  self.assertFalse(ok); self.assertEqual(reason,"REAL_CAPITAL_LIMIT")
 def test_real_capacity_fails_closed_on_stale_wallet_sync(self):
  real=dict(self.real); real["lastSuccessfulWalletSync"]="2020-01-01T00:00:00+00:00"
  ok,reason,_=p._capacity(real)
  self.assertFalse(ok); self.assertEqual(reason,"REAL_WALLET_SYNC_STALE")
 def test_real_loss_circuit_blocks(self):
  self.real["daily_realized_pnl_sda"]=-cfg.MAX_DAILY_LOSS_SDA
  self.assertFalse(self.ready()["ready"])

 def test_consumed_real_signal_is_not_replayed_after_restart(self):
  quote=self.at.isoformat()
  signal_id=__import__("hashlib").sha256(
   f"{self.token}|{quote}|{p.strategy.VERSION}|REAL".encode()).hexdigest()
  self.state["consumed_signal_ids"]=[signal_id]
  p.save_state(self.state,self.path)
  self.state,error=p.load_state(self.path)
  self.assertIsNone(error)
  self.assertFalse(self.ready()["ready"])

 def test_first_real_observation_passes_empty_history_not_shadow_fallback(self):
  score=({"last_transaction":self.at.isoformat(),"data_age_sec":10,"price":1,"m1":3,"m15":2,"m4":1,
   "flow":800,"flow15":200,"vol":1000,"trades":12,"buy_ratio":9},80,70,10,"CONFIRMATION")
  seen=[]
  with patch.object(p.strategy,"score",return_value=score),\
       patch.object(p,"evaluate_tp4",side_effect=lambda *a,**kw:(seen.append(kw.get("previous")),{"ready":False,"reason":"no_prior"})[1]),\
       patch.object(p,"_market_gates",return_value={"blocked":True,"reasons":["no temporal"]}),\
       patch.object(p,"_capacity",return_value=(True,"REAL_CAPACITY_AVAILABLE",{"positions":7})):
   p.evaluate_candidate(self.token,self.analysis,self.state,self.real,self.at)
  self.assertEqual(seen,[{}])
 def test_restart_preserves_temporal_history(self):
  self.state["temporal_history"][self.token]={"score":70}
  p.save_state(self.state,self.path); loaded,error=p.load_state(self.path)
  self.assertIsNone(error); self.assertEqual(loaded["temporal_history"],self.state["temporal_history"])
 def test_missing_real_signal_state_fails_closed(self):
  loaded,error=p.load_state(self.path)
  self.assertIsNone(loaded)
  self.assertEqual(error,"REAL_SIGNAL_STATE_MISSING")
 def test_corrupt_state_fails_closed(self):
  self.path.write_text("{",encoding="utf-8")
  loaded,error=p.load_state(self.path); self.assertIsNone(loaded); self.assertEqual(error,"REAL_SIGNAL_STATE_UNREADABLE")

 def test_full_dry_run_path_returns_would_execute(self):
  order=[]
  market={"tokens":{self.token:self.analysis}}
  market_path=Path(self.tmp.name)/"market.json"
  market_path.write_text(__import__("json").dumps(market),encoding="utf-8")
  p.save_state(self.state,self.path)
  temporal={"ready":True,"reason":"TEST_VALID","lane":"confirmed_continuation","sparse_activity_verified":True}
  score=({"last_transaction":self.at.isoformat(),"data_age_sec":10,"price":1,"m1":3,"m15":2,"m4":1,
   "flow":800,"flow15":200,"vol":1000,"trades":12,"buy_ratio":9,"trade_ratio":2,"accel":1},80,70,10,"CONFIRMATION")
  # PAPER is deliberately full and has an active cooldown/loss circuit. REAL's
  # own 7/10 capacity and empty cooldown are the only admission state consulted.
  paper_file=Path(self.tmp.name)/"positions.json"
  paper_file.write_text(__import__("json").dumps({"positions":{**{f"0x{i+100:040x}":{} for i in range(9)},self.token:{}}}),encoding="utf-8")
  shadow_file=Path(self.tmp.name)/"strategy_lab"/"forward_shadow_state.json"
  shadow_file.parent.mkdir(parents=True,exist_ok=True)
  shadow_file.write_text(__import__("json").dumps({"cooldowns":{self.token:"2099-01-01T00:00:00+00:00"},"loss_circuit":{"blocked":True}}),encoding="utf-8")
  banned_reads=[]
  original_read=Path.read_text
  def guarded_read(path,*args,**kwargs):
   if path.name in ("positions.json","forward_shadow_state.json"):
    banned_reads.append(path.name)
    raise AssertionError("REAL pipeline attempted to read PAPER state")
   return original_read(path,*args,**kwargs)
  actual_safety=p.real_trader.safety_check
  def safety(*args):
   actual_safety(*args)
   order.append("SAFETY_PASS")
  with contextlib.chdir(self.tmp.name),patch.dict("os.environ",{"REAL_TRADING_ENABLED":"true","REAL_TRADING_DRY_RUN":"true"}),\
       patch.object(p.strategy,"score",return_value=score),\
       patch.object(p,"evaluate_tp4",return_value=temporal),\
       patch.object(p,"_market_gates",return_value={"blocked":False,"reasons":[]}),\
       patch.object(p,"_capacity",return_value=(True,"REAL_CAPACITY_AVAILABLE",{"positions":7})),\
       patch.object(p.real_trader,"load_state",return_value=self.real),\
       patch.object(p.real_trader,"safety_check",side_effect=safety),\
       patch.object(p.real_trader,"execute_buy",side_effect=lambda *a:(order.append("EXECUTE"),{"status":"DRY_RUN"})[1]),\
       patch.object(Path,"read_text",guarded_read):
   result=p.run_scan(market_path=str(market_path),execute=True,state_path=self.path)
  self.assertEqual(order,["SAFETY_PASS","EXECUTE"])
  self.assertEqual(result["actions"][0]["status"],"WOULD EXECUTE")
  self.assertEqual(banned_reads,[])

 def test_safety_failure_removes_buy_ready_diagnostic(self):
  market_path=Path(self.tmp.name)/"market.json"
  market_path.write_text(__import__("json").dumps({"tokens":{self.token:self.analysis}}),encoding="utf-8")
  p.save_state(self.state,self.path)
  with patch.dict("os.environ",{"REAL_TRADING_ENABLED":"true","REAL_TRADING_DRY_RUN":"true"}),\
       patch.object(p,"load_state",return_value=(self.state,None)),\
       patch.object(p,"evaluate_candidate",return_value={"ready":True,"signal_id":"sid"}),\
       patch.object(p.real_trader,"load_state",return_value=self.real),\
       patch.object(p.real_trader,"safety_check",side_effect=RuntimeError("daily loss limit")),\
       patch.object(p.real_trader,"execute_buy") as execute:
   result=p.run_scan(market_path=str(market_path),execute=True,state_path=self.path)
  execute.assert_not_called()
  self.assertEqual(result["buy_ready"],0)
  self.assertFalse(self.state["buy_ready"][self.token]["ready"])
 def test_executor_dry_run_contract(self):
  with patch.object(p.real_trader,"safety_check") as safety,patch.object(p.real_trader,"execute_buy",return_value={"status":"DRY_RUN"}):
   p.real_trader.safety_check(30,self.real); out=p.real_trader.execute_buy(self.token,"T",30)
   safety.assert_called_once(); self.assertEqual(out["status"],"DRY_RUN")

if __name__=="__main__": unittest.main()
