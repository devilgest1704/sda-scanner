import contextlib, tempfile, unittest
import copy
import json
import multiprocessing
import sys
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
  # PAPER is deliberately full and has an active cooldown/loss circuit. REAL's
  # own 7/10 capacity and empty cooldown are the only admission state consulted.
  paper_file=Path(self.tmp.name)/"positions.json"
  paper_file.write_text(__import__("json").dumps({
   "positions":{**{f"0x{i+100:040x}":{} for i in range(9)},self.token:{}},
   "closed_trades":[{"status":"CLOSED","closed_at":self.at.isoformat(),"closed_profit_sda":-3}
                    for _ in range(8)]}),encoding="utf-8")
  paper_state_file=Path(self.tmp.name)/"v30_pump_state.json"
  paper_state_file.write_text(__import__("json").dumps({
   "cooldowns":{self.token:"2099-01-01T00:00:00+00:00"}}),encoding="utf-8")
  shadow_file=Path(self.tmp.name)/"strategy_lab"/"forward_shadow_state.json"
  shadow_file.parent.mkdir(parents=True,exist_ok=True)
  shadow_file.write_text(__import__("json").dumps({"cooldowns":{self.token:"2099-01-01T00:00:00+00:00"},
   "loss_circuit":{"blocked":True}}),encoding="utf-8")
  (Path(self.tmp.name)/"v30_agent_config.json").write_text("{invalid",encoding="utf-8")
  (Path(self.tmp.name)/"paper_champion_config.json").write_text("{invalid",encoding="utf-8")
  banned={"positions.json","forward_shadow_state.json","v30_pump_state.json",
          "v30_agent_config.json","paper_champion_config.json","v30_execution_state.json"}
  banned_reads=[]
  original_read=Path.read_text
  original_open=open
  def guarded_read(path,*args,**kwargs):
   if path.name in banned:
    banned_reads.append(path.name)
    raise AssertionError("REAL pipeline attempted to read PAPER state")
   return original_read(path,*args,**kwargs)
  def guarded_open(path,*args,**kwargs):
   if Path(path).name in banned:
    banned_reads.append(Path(path).name)
    raise AssertionError("REAL pipeline attempted to open PAPER state")
   return original_open(path,*args,**kwargs)
  actual_safety=p.real_trader.safety_check
  def safety(*args):
   actual_safety(*args)
   order.append("SAFETY_PASS")
  forbidden=unittest.mock.Mock(side_effect=AssertionError("PAPER access or live transaction attempted"))
  with contextlib.chdir(self.tmp.name),patch.dict("os.environ",{"REAL_TRADING_ENABLED":"true","REAL_TRADING_DRY_RUN":"true"}),\
      patch.object(p.strategy,"load_state",forbidden),\
      patch("tools.temporal_recovery_buy.previous_signals",forbidden),\
      patch.object(p.real_trader,"wallet_address",return_value="0x"+"c"*40),\
      patch.object(p.real_trader,"native_balance",return_value=1000*10**18),\
      patch.object(p.real_trader,"rpc",forbidden),\
      patch.object(p.real_trader,"private_account",forbidden),\
      patch.object(p.real_trader,"send_transaction",forbidden),\
       patch.object(p.real_trader,"load_state",return_value=self.real),\
       patch.object(p.real_trader,"safety_check",side_effect=safety),\
       patch.object(p.real_trader,"execute_buy",side_effect=lambda *a:(order.append("EXECUTE"),{"status":"DRY_RUN"})[1]),\
       patch.object(Path,"read_text",guarded_read),patch("builtins.open",guarded_open):
   result=p.run_scan(market_path=str(market_path),execute=True,state_path=self.path)
  self.assertEqual(order,["SAFETY_PASS","EXECUTE"])
  self.assertEqual(result["actions"][0]["status"],"WOULD EXECUTE")
  self.assertEqual(result["buy_ready"],1)
  saved,error=p.load_state(self.path)
  self.assertIsNone(error)
  self.assertTrue(saved["buy_ready"][self.token]["ready"])
  self.assertEqual(saved["buy_ready"][self.token]["capacity"]["positions"],7)
  self.assertEqual(saved["buy_ready"][self.token]["capacity"]["deployed_sda"],210)
  self.assertIn(self.token,saved["history"])
  self.assertIn(self.token,saved["temporal_history"])
  self.assertFalse(saved["risk_circuit"]["blocked"])
  self.assertEqual(cfg.position_sda(),30)
  self.assertEqual(cfg.MAX_OPEN_POSITIONS,10)
  self.assertEqual(cfg.TRADING_WALLET_CAPITAL_SDA,300)
  forbidden.assert_not_called()
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

class RealBuyClaimTests(unittest.TestCase):
    tearDown = RealPaperIsolation.tearDown

    def setUp(self):
        RealPaperIsolation.setUp(self)
        self.market_path = Path(self.tmp.name) / "market.json"
        self.market_path.write_text(
            json.dumps({"tokens": {self.token: self.analysis}}), encoding="utf-8"
        )
        p.save_state(self.state, self.path)
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(
            patch.dict(
                "os.environ",
                {"REAL_TRADING_ENABLED": "true", "REAL_TRADING_DRY_RUN": "true"},
            )
        )
        self.contexts.enter_context(
            patch.object(p.real_trader, "load_state", return_value=self.real)
        )
        self.contexts.enter_context(
            patch.object(p.real_trader, "wallet_address", return_value="0x" + "c" * 40)
        )
        self.contexts.enter_context(
            patch.object(p.real_trader, "native_balance", return_value=1000 * 10**18)
        )
        self.forbidden = unittest.mock.Mock(
            side_effect=AssertionError(
                "Network, PAPER state, signing or broadcast forbidden"
            )
        )
        for name in ("rpc", "private_account", "send_transaction"):
            self.contexts.enter_context(
                patch.object(p.real_trader, name, self.forbidden)
            )
        self.contexts.enter_context(
            patch.object(p.strategy, "load_state", self.forbidden)
        )
        self.contexts.enter_context(
            patch("tools.temporal_recovery_buy.previous_signals", self.forbidden)
        )
        self.actual_execute = p.real_trader.execute_buy
        self.execute = self.contexts.enter_context(
            patch.object(
                p.real_trader, "execute_buy", return_value={"status": "DRY_RUN"}
            )
        )

    def scan(self):
        return p.run_scan(str(self.market_path), state_path=self.path)

    def reserve(self):
        ready, actions, reservations = p._prepare_scan(
            {self.token: self.analysis}, True, self.path
        )
        self.assertEqual(ready, 1)
        self.assertEqual(actions, [])
        self.assertEqual(len(reservations), 1)
        return reservations[0]

    def stored(self):
        state, error = p.load_state(self.path)
        self.assertIsNone(error)
        return state

    def expire(self, reservation):
        with p.state_lock(self.path):
            state = self.stored()
            state["claims"][reservation["signal_id"]]["expires_at"] = (
                p.now() - timedelta(seconds=1)
            ).isoformat()
            p.save_state(state, self.path)

    def test_dry_run_claim_blocks_replay_after_restart(self):
        first = self.scan()
        second = self.scan()
        self.assertEqual(first["actions"][0]["status"], "WOULD EXECUTE")
        self.assertEqual(self.execute.call_count, 1)
        self.assertIn(
            second["actions"][0]["status"], ("ALREADY_CLAIMED", "DUPLICATE_BLOCKED")
        )
        self.forbidden.assert_not_called()

    def test_identity_is_stable_and_includes_chain_token_quote_and_strategy(self):
        quote = self.at.isoformat()
        identity = p.signal_identity(self.token, quote)
        self.assertEqual(
            identity,
            p.signal_identity(self.token.upper(), quote.replace("+00:00", "Z")),
        )
        self.assertNotEqual(identity, p.signal_identity("0x" + "b" * 40, quote))
        self.assertNotEqual(
            identity,
            p.signal_identity(self.token, (self.at + timedelta(seconds=1)).isoformat()),
        )
        with patch.object(cfg, "CHAIN_ID", cfg.CHAIN_ID + 1):
            self.assertNotEqual(identity, p.signal_identity(self.token, quote))
        with patch.object(p.strategy, "VERSION", "different-version"):
            self.assertNotEqual(identity, p.signal_identity(self.token, quote))

    def test_restart_after_claim_is_blocked(self):
        reservation = self.reserve()
        claim = self.stored()["claims"][reservation["signal_id"]]
        for key in (
            "signal_id",
            "token",
            "claimed_at",
            "status",
            "worker_id",
            "expires_at",
            "claim_id",
        ):
            self.assertTrue(claim[key])
        self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        self.execute.assert_not_called()

    def test_expired_prebroadcast_claim_is_retryable(self):
        old = self.reserve()
        self.expire(old)
        self.assertEqual(self.scan()["actions"][0]["status"], "WOULD EXECUTE")
        self.execute.assert_called_once()
        current = self.stored()["claims"][old["signal_id"]]
        self.assertNotEqual(current["claim_id"], old["claim_id"])

    def test_expired_owner_is_fenced_after_claim_is_reassigned(self):
        old = self.reserve()
        self.expire(old)
        current = self.reserve()
        with self.assertRaisesRegex(RuntimeError, "OWNERSHIP_CHANGED"):
            p._execute_reserved(old, self.path)
        self.execute.assert_not_called()
        self.assertEqual(
            p._execute_reserved(current, self.path)["status"], "WOULD EXECUTE"
        )
        self.execute.assert_called_once()

    def test_expired_broadcast_pending_is_never_released(self):
        reservation = self.reserve()
        p._begin_execution(reservation, self.path)
        self.expire(reservation)
        self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        self.assertEqual(
            self.stored()["claims"][reservation["signal_id"]]["status"],
            "BROADCAST_PENDING",
        )
        self.execute.assert_not_called()

    def test_unknown_executor_result_is_not_retried_without_reconciliation(self):
        self.execute.side_effect = TimeoutError("response lost")
        self.scan()
        reservation = next(iter(self.stored()["claims"].values()))
        self.assertEqual(reservation["status"], "UNKNOWN")
        self.expire(reservation)
        self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        self.execute.assert_called_once()

    def test_pending_intent_hash_is_reconciled_without_reexecution(self):
        tx_hash = "0x" + "d" * 64

        def broadcast_attempt(*args):
            self.real["pending_buys"][self.token] = {
                "tx_hash": tx_hash,
                "position_sda": 30,
            }
            raise TimeoutError("receipt unavailable")

        self.execute.side_effect = broadcast_attempt
        self.scan()
        claim = next(iter(self.stored()["claims"].values()))
        self.assertEqual(claim["tx_hash"], tx_hash)
        self.expire(claim)
        with patch.object(p.real_trader, "rpc", return_value=None) as rpc:
            self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        rpc.assert_called_once_with("eth_getTransactionReceipt", [tx_hash])
        self.assertEqual(
            self.stored()["claims"][claim["signal_id"]]["status"], "BROADCAST_PENDING"
        )
        self.real["pending_buys"].clear()
        self.real["positions"][self.token] = {"buy_tx": tx_hash, "cost_sda": 30}
        self.assertEqual(self.scan()["actions"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(
            self.stored()["claims"][claim["signal_id"]]["status"], "EXECUTED"
        )
        self.execute.assert_called_once()

    def test_confirmed_receipt_without_accounting_keeps_reservation(self):
        self.execute.return_value = {
            "status": "PENDING_CONFIRMATION",
            "tx": "0x" + "e" * 64,
        }
        self.scan()
        claim = next(iter(self.stored()["claims"].values()))
        self.expire(claim)
        with patch.object(p.real_trader, "rpc", return_value={"status": "0x1"}):
            self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        claim = next(iter(self.stored()["claims"].values()))
        self.assertEqual(claim["reconciliation"], "CONFIRMED_AWAITING_ACCOUNTING")
        self.assertEqual(claim["status"], "BROADCAST_PENDING")
        self.execute.assert_called_once()

    def test_executed_signal_is_never_repeated(self):
        self.execute.return_value = {"status": "CONFIRMED", "tx": "0x" + "f" * 64}
        self.scan()
        claim = next(iter(self.stored()["claims"].values()))
        self.assertEqual(claim["status"], "EXECUTED")
        self.expire(claim)
        self.assertEqual(self.scan()["actions"][0]["status"], "DUPLICATE_BLOCKED")
        self.execute.assert_called_once()

    def test_different_signals_are_independent(self):
        self.scan()
        other = "0x" + "b" * 40
        self.market_path.write_text(
            json.dumps({"tokens": {other: self.analysis}}), encoding="utf-8"
        )
        self.assertEqual(self.scan()["actions"][0]["status"], "WOULD EXECUTE")
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(len(self.stored()["claims"]), 2)

    def test_prebroadcast_safety_failure_is_retryable(self):
        with patch.object(
            p.real_trader, "safety_check", side_effect=RuntimeError("blocked")
        ):
            self.scan()
        self.execute.assert_not_called()
        self.assertEqual(
            next(iter(self.stored()["claims"].values()))["status"], "FAILED_RETRYABLE"
        )
        self.assertEqual(self.scan()["actions"][0]["status"], "WOULD EXECUTE")
        self.execute.assert_called_once()

    def test_lock_failure_blocks_executor(self):
        with patch.object(p.fcntl, "flock", side_effect=OSError("lock unavailable")):
            self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()

    def test_executor_preparation_error_is_retryable_without_signing(self):
        with patch.object(
            p.real_trader, "prepare_buy", side_effect=RuntimeError("quote unavailable")
        ):
            with self.assertRaises(p.real_trader.BuyPreBroadcastError):
                self.actual_execute(self.token, "T", 30)
        self.execute.side_effect = p.real_trader.BuyPreBroadcastError(
            "quote unavailable"
        )
        self.scan()
        self.assertEqual(
            next(iter(self.stored()["claims"].values()))["status"], "FAILED_RETRYABLE"
        )
        self.execute.side_effect = None
        self.assertEqual(self.scan()["actions"][0]["status"], "WOULD EXECUTE")
        self.forbidden.assert_not_called()

    def test_unlock_failure_blocks_executor(self):
        actual = p.fcntl.flock

        def unlock_failure(descriptor, operation):
            if operation == p.fcntl.LOCK_UN:
                actual(descriptor, operation)
                raise OSError("unlock failed")
            return actual(descriptor, operation)

        with patch.object(p.fcntl, "flock", side_effect=unlock_failure):
            self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()

    def test_lock_open_failure_blocks_executor(self):
        with patch.object(p, "state_lock", side_effect=PermissionError("lock file")):
            self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()

    def test_read_missing_corrupt_and_schema_failures_block_executor(self):
        for content in (
            None,
            "{",
            "{}",
            json.dumps({**self.state, "claims": {"bad": {}}}),
        ):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content, encoding="utf-8")
                self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()

    def test_unreadable_authoritative_state_blocks_executor(self):
        with patch.object(
            p, "load_state", return_value=(None, "REAL_SIGNAL_STATE_UNREADABLE")
        ):
            self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()

    def test_claim_atomic_save_failure_blocks_executor(self):
        with patch.object(p.os, "replace", side_effect=OSError("atomic save failed")):
            self.assertEqual(self.scan()["status"], "state_error")
        self.execute.assert_not_called()
        self.assertEqual(self.stored()["claims"], {})

    def test_broadcast_transition_save_failure_blocks_executor(self):
        reservation = self.reserve()
        with patch.object(p.os, "replace", side_effect=OSError("atomic save failed")):
            with self.assertRaises(OSError):
                p._execute_reserved(reservation, self.path)
        self.execute.assert_not_called()
        self.assertEqual(
            self.stored()["claims"][reservation["signal_id"]]["status"], "CLAIMED"
        )

    def test_postexecutor_save_failure_keeps_persistent_pending_claim(self):
        original = p.save_state
        calls = []

        def fail_final_save(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise OSError("final save failed")
            return original(*args, **kwargs)

        with patch.object(p, "save_state", side_effect=fail_final_save):
            self.scan()
        self.assertEqual(
            next(iter(self.stored()["claims"].values()))["status"], "BROADCAST_PENDING"
        )
        self.assertEqual(self.scan()["actions"][0]["status"], "ALREADY_CLAIMED")
        self.execute.assert_called_once()

    def test_real_reservations_consume_real_capacity(self):
        self.real["positions"].update(
            {f"0x{i + 100:040x}": {"cost_sda": 30} for i in range(2)}
        )
        self.reserve()
        other = "0x" + "b" * 40
        self.market_path.write_text(
            json.dumps({"tokens": {other: self.analysis}}), encoding="utf-8"
        )
        self.assertEqual(self.scan()["buy_ready"], 0)
        self.execute.assert_not_called()

    def test_paper_decision_is_unaffected_by_real_claim(self):
        paper_state = {"history": {}, "cooldowns": {}, "paper_buy_signal_ids": []}
        before_state = copy.deepcopy(paper_state)
        with (
            patch.object(p.strategy, "load_state", return_value=paper_state),
            patch.object(p.strategy, "PAPER_BUY_MODE", "temporal_recovery"),
            patch.object(p.strategy, "PAPER_TP4", False),
            patch.object(p.strategy, "cooldown_active", return_value=False),
            patch.object(
                p.strategy,
                "temporal_recovery_signal",
                return_value={"ready": True, "reason": "VALID"},
            ),
            patch.object(
                p.strategy,
                "paper_entry_risk_gate",
                return_value={"blocked": False, "status": "ACTIVE"},
            ),
            patch.object(
                p.strategy, "paper_macd_veto", return_value={"blocked": False}
            ),
            patch.object(p.strategy.paper_champion_config, "buy_veto", return_value=[]),
        ):
            before = p.strategy.decision(self.token, self.analysis, persist=False)
            self.reserve()
            after = p.strategy.decision(self.token, self.analysis, persist=False)
        self.assertTrue(before["eligible_for_buy"])
        for key in (
            "eligible_for_buy",
            "paper_buy_blocked",
            "paper_buy_gate_failures",
            "pump_score",
        ):
            self.assertEqual(before[key], after[key])
        self.assertEqual(paper_state, before_state)

    @unittest.skipUnless(
        sys.platform == "linux", "Real fcntl concurrency requires Linux"
    )
    def test_linux_parallel_processes_execute_same_signal_exactly_once(self):
        context = multiprocessing.get_context("fork")
        messages = context.Queue()
        release = context.Event()

        def cycle(label):
            def executor(*args):
                messages.put((label, "EXECUTOR"))
                if label == "first" and not release.wait(15):
                    raise RuntimeError("Executor was not released")
                return {"status": "DRY_RUN"}

            self.execute.side_effect = executor
            result = self.scan()
            self.forbidden.assert_not_called()
            messages.put((label, result))

        first = context.Process(target=cycle, args=("first",))
        second = context.Process(target=cycle, args=("second",))
        first.start()
        events = []
        try:
            events.append(messages.get(timeout=15))
            self.assertEqual(events[0], ("first", "EXECUTOR"))
            second.start()
            events.append(messages.get(timeout=15))
        finally:
            release.set()
            first.join(20)
            if second.pid is not None:
                second.join(20)
            for process in (first, second):
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        events.append(messages.get(timeout=5))
        executor_calls = [event for event in events if event[1] == "EXECUTOR"]
        outcomes = {label: value for label, value in events if isinstance(value, dict)}
        self.assertEqual(len(executor_calls), 1)
        self.assertEqual(outcomes["first"]["actions"][0]["status"], "WOULD EXECUTE")
        self.assertIn(
            outcomes["second"]["actions"][0]["status"],
            ("ALREADY_CLAIMED", "DUPLICATE_BLOCKED"),
        )
        self.assertEqual(
            outcomes["first"]["actions"][0]["signal_id"],
            outcomes["second"]["actions"][0]["signal_id"],
        )


if __name__ == "__main__":
    unittest.main()
