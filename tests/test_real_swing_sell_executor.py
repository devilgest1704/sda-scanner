"""Mocked integration tests for live Swing SELL V2; no RPC or wallet transactions."""
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import real_swing_sell_executor as sell


TOKEN = "0x" + "a" * 40
WSDA = "0x" + "b" * 40
WALLET = "0x" + "c" * 40
ROUTER = "0x" + "d" * 40


class SwingSellExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "real_trade_state.json"
        self.state = {
            "sell_strategy_version": "swing_v2",
            "positions": {
                TOKEN: {
                    "token": TOKEN, "symbol": "TEST", "buy_tx": "buy-1",
                    "token_amount_raw": 1000, "amount_sda": 20.0,
                    "opened_at": "2026-11-01T00:00:00Z",
                }
            },
            "pending_sells": {}, "sell_intents": {}, "sell_intent_counters": {},
            "trades": [], "daily_realized_pnl_sda": 0.0,
        }
        self.account = SimpleNamespace(address=WALLET)
        self.load = Mock(side_effect=lambda: self.state)
        self.save = Mock(side_effect=lambda value: self.state.update(value))
        self.token_balances = []
        self.wsda_balances = []
        self.send_calls = []
        self.events = []
        self.send = Mock(side_effect=self.fake_send)
        self.receipt = Mock(return_value={"status": "0x1", "logs": self.transfer_logs(200, 5 * 10**18)})
        self.rpc = Mock(side_effect=self.rpc_result)
        self.patches = [
            patch.object(sell.trader, "load_state", self.load),
            patch.object(sell.trader, "save_state", self.save),
            patch.object(sell.trader, "now", lambda: "2026-11-09T18:00:00+00:00"),
            patch.object(sell.trader, "wallet_address", lambda: WALLET),
            patch.object(sell.trader, "private_account", lambda: self.account),
            patch.object(sell.trader, "chain_id", lambda: 999),
            patch.object(sell.trader, "token_decimals", lambda token: 0),
            patch.object(sell.trader, "token_balance", side_effect=self.balance),
            patch.object(sell.trader, "allowance", lambda *a: 10**30),
            patch.object(sell.trader, "quote_best_live_fee", lambda *a, **k: (3000, 10**19, "0x", 9999999999)),
            patch.object(sell.trader, "min_out", lambda value: int(value * 0.99)),
            patch.object(sell.trader, "build_exact_input_single", lambda *a: "0x1234"),
            patch.object(sell.trader, "send_transaction", self.send),
            patch.object(sell.trader, "rpc", self.rpc),
            patch.object(sell.trader, "persist_real_intent_before_broadcast", lambda: None),
            patch.object(sell.cfg, "REAL_TRADING_ENABLED", True),
            patch.object(sell.cfg, "REAL_TRADING_DRY_RUN", False),
            patch.object(sell.cfg, "STATE_FILE", str(self.state_path)),
            patch.object(sell.cfg, "WSDA_ADDRESS", WSDA),
            patch.object(sell.cfg, "ROUTER_ADDRESS", ROUTER),
            patch.object(sell.cfg, "APPROVE_SELECTOR", "0x095ea7b3"),
            patch.object(sell.cfg, "UNWRAP_SELECTOR", "0xdeadbeef"),
            patch.object(sell.cfg, "CHAIN_ID", 999),
            patch.object(sell.cfg, "GAS_LIMIT", 500000),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.token_balances = [1000, 1000, 1000, 800]
        self.wsda_balances = [0, 0, 5 * 10**18]

    def tearDown(self):
        self.tmp.cleanup()

    def transfer_logs(self, sold, proceeds):
        topic = sell.TRANSFER_TOPIC
        wallet_topic = "0x" + WALLET[2:].rjust(64, "0")
        router_topic = "0x" + ROUTER[2:].rjust(64, "0")
        return [
            {"address": TOKEN, "topics": [topic, wallet_topic, router_topic],
             "data": hex(int(sold))},
            {"address": WSDA, "topics": [topic, router_topic, wallet_topic],
             "data": hex(int(proceeds))},
        ]

    def rpc_result(self, method, params):
        if params[0] == "0xunwrap":
            return {"status": "0x1", "logs": []}
        return self.receipt(method, params)

    def balance(self, token, owner):
        if token == WSDA:
            return self.wsda_balances.pop(0) if self.wsda_balances else 5 * 10**18
        return self.token_balances.pop(0) if self.token_balances else 800

    def fake_send(self, account, to, data, value=0, gas_limit=None, before_broadcast=None):
        if str(data).startswith("0x095ea7b3"):
            tx = "0xapproval"
            self.events.append("approval")
        elif data == "0x1234":
            tx = "0xswap"
            self.events.append("swap")
        else:
            tx = "0xunwrap"
            self.events.append("unwrap")
        self.send_calls.append(tx)
        if before_broadcast:
            before_broadcast({"tx_hash": tx, "nonce": len(self.send_calls),
                              "gas": 100000, "gas_price": 1})
        return tx, 100000, 1

    def test_confirmed_partial_updates_quantity_basis_and_realized_pnl(self):
        result = sell.evaluate_position(TOKEN, "TEST", "0.025", "2026-11-09T18:00:00Z")
        pos = self.state["positions"][TOKEN]
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(pos["remaining_quantity"], 800)
        self.assertEqual(pos["token_amount_raw"], 800)
        self.assertAlmostEqual(pos["amount_sda"], 16.0)
        self.assertAlmostEqual(result["realized_pnl_sda"], 1.0)
        self.assertTrue(pos["profit_partial_done"])
        self.assertTrue(pos["trailing_armed"])
        self.assertEqual(len(self.state["trades"]), 1)
        self.assertEqual(self.state["trades"][0]["sell_reason"], "PROFIT_PARTIAL_25")
        open_value = Decimal(pos["remaining_quantity"]) * Decimal("0.025")
        open_pnl = open_value - Decimal(pos["remaining_cost_basis"])
        self.assertEqual(open_pnl, Decimal("4.0"))
        self.assertEqual(open_pnl + Decimal(str(result["realized_pnl_sda"])), Decimal("5.0"))

    def test_restart_after_partial_does_not_repeat_partial(self):
        sell.evaluate_position(TOKEN, "TEST", "0.025", "2026-11-09T18:00:00Z")
        self.token_balances = [800]
        self.wsda_balances = []
        result = sell.evaluate_position(TOKEN, "TEST", "0.025", "2026-11-09T18:01:00Z")
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(len([x for x in self.state["trades"] if x["action"] == "SELL"]), 1)

    def test_manual_full_wallet_sell_closes_without_automatic_transaction(self):
        self.token_balances = [0]
        result = sell.evaluate_position(TOKEN, "TEST", "0.02", "2026-11-09T18:00:00Z")
        self.assertEqual(result["status"], "MANUAL_CLOSED")
        self.assertNotIn(TOKEN, self.state["positions"])
        self.send.assert_not_called()
        self.assertIsNone(self.state["trades"][0]["proceeds_sda"])

    def test_manual_partial_wallet_sell_reduces_quantity_and_cost_basis(self):
        self.token_balances = [700]
        result = sell.evaluate_position(TOKEN, "TEST", "0.02", "2026-11-09T18:00:00Z")
        pos = self.state["positions"][TOKEN]
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(pos["remaining_quantity"], 700)
        self.assertAlmostEqual(float(pos["remaining_cost_basis"]), 14.0)
        self.assertIsNone(self.state["trades"][0]["proceeds_sda"])
        self.send.assert_not_called()

    def test_submitted_transaction_restart_does_not_resubmit(self):
        self.state["pending_sells"][TOKEN] = {
            "sell_intent_id": "id-1", "token": TOKEN, "stage": "SWAP_SUBMITTED",
            "status": "SUBMITTED", "tx_hash": "0xalready", "requested_quantity_raw": 200,
        }
        self.receipt.return_value = None
        result = sell.evaluate_position(TOKEN, "TEST", "0.025")
        self.assertEqual(result["status"], "PENDING_CONFIRMATION")
        self.send.assert_not_called()

    def test_rpc_timeout_after_broadcast_never_blindly_retries(self):
        self.rpc.side_effect = [TimeoutError("rpc timeout"), None]
        result = sell.evaluate_position(TOKEN, "TEST", "0.025")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.state["pending_sells"][TOKEN]["stage"], "SWAP_SUBMITTED")
        self.token_balances = []
        self.wsda_balances = []
        result2 = sell.evaluate_position(TOKEN, "TEST", "0.025")
        self.assertEqual(result2["status"], "PENDING_CONFIRMATION")
        self.assertEqual(self.send.call_count, 1)

    def test_wallet_quantity_caps_full_sell(self):
        self.token_balances = [600, 600, 600, 0]
        self.wsda_balances = [0, 0, 12 * 10**18]
        self.receipt.return_value = {"status": "0x1", "logs": self.transfer_logs(600, 12 * 10**18)}
        result = sell.evaluate_position(TOKEN, "TEST", "0.01")
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertNotIn(TOKEN, self.state["positions"])
        self.assertEqual(self.state["trades"][-1]["sold_quantity_raw"], 600)

    def test_pre_migration_price_observation_cannot_trigger_sell(self):
        self.state["sell_migration_observation_started_at"] = "2026-11-09T18:00:00+00:00"
        self.token_balances = [1000]
        result = sell.evaluate_position(TOKEN, "TEST", "0.025", "2026-11-09T17:59:59Z")
        self.assertEqual(result["status"], "PRE_MIGRATION_PRICE")
        self.send.assert_not_called()
        self.assertEqual(self.state["trades"], [])

    def test_legacy_pending_sell_is_archived_without_transaction(self):
        self.state.pop("sell_strategy_version")
        self.state["pending_sells"] = {TOKEN: {"tx_hash": "0xlegacy"}}
        migrated = sell.migrate_if_needed()
        self.assertTrue(migrated)
        self.assertEqual(self.state["pending_sells"], {})
        self.assertEqual(self.state["legacy_pending_sells"][0]["record"]["tx_hash"], "0xlegacy")
        self.assertEqual(self.state["sell_migration_historical_transactions"], 0)
        self.send.assert_not_called()

    def test_approval_is_confirmed_before_fresh_sell_quote(self):
        def quote(*args, **kwargs):
            self.events.append("quote")
            return (3000, 10**19, "0x", 9999999999)
        with patch.object(sell.trader, "allowance", side_effect=[0, 10**30]):
            with patch.object(sell.trader, "quote_best_live_fee", side_effect=quote):
                result = sell.evaluate_position(TOKEN, "TEST", "0.025")
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(self.events[:4], ["approval", "quote", "swap", "unwrap"])

    def test_legacy_full_position_executor_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy full-position SELL executor is disabled"):
            sell.trader.execute_sell(TOKEN, "TEST", 1000)

    def test_two_concurrent_evaluations_create_only_one_partial(self):
        results = []
        errors = []
        barrier = threading.Barrier(2)
        def run():
            try:
                barrier.wait()
                results.append(sell.evaluate_position(TOKEN, "TEST", "0.025"))
            except Exception as exc:
                errors.append(exc)
        a = threading.Thread(target=run)
        b = threading.Thread(target=run)
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.send.call_count, 2)  # one swap + one unwrap
        self.assertEqual(len(self.state["trades"]), 1)
        self.assertTrue(self.state["positions"][TOKEN]["profit_partial_done"])


if __name__ == "__main__":
    unittest.main()
