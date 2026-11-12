"""Unit tests for the PAPER -> REAL transition bridge (no RPC or wallet needed)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import real_paper_bridge as bridge


TOKEN = "0x" + "a" * 40


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paper = root / "positions.json"
        self.state = root / "bridge_state.json"
        self.events = root / "bridge_events.json"
        self.paper.write_text(json.dumps({"positions": {}}), encoding="utf-8")
        for name, value in (
            ("PAPER_FILE", self.paper),
            ("STATE_FILE", self.state),
            ("EVENTS_FILE", self.events),
        ):
            p = patch.object(bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "REAL_TRADING_ENABLED": "true",
            "REAL_TRADING_DRY_RUN": "false",
            "SDA_PAPER_BUY_MODE": "temporal_recovery",
            "SDA_PAPER_TP4": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = SimpleNamespace(position_sda=lambda value=None: min(20.0, max(10.0, 20.0 if value is None else float(value))), POSITION_DEFAULT_SDA=20.0)
        self.trader = SimpleNamespace(
            execute_buy=unittest.mock.Mock(return_value={"status": "CONFIRMED", "tx": "buytx"}),
            execute_sell=unittest.mock.Mock(return_value={"status": "CONFIRMED", "tx": "selltx"}),
            load_state=unittest.mock.Mock(return_value={"positions": {
                TOKEN: {"symbol": "TEST", "token_amount_raw": 123}
            }}),
        )
        self.swing = SimpleNamespace(
            migrate_if_needed=unittest.mock.Mock(return_value=True),
            reconcile_position_wallet=unittest.mock.Mock(return_value={"status": "UNCHANGED"}),
            startup_recovery=unittest.mock.Mock(return_value=[]),
            evaluate_position=unittest.mock.Mock(return_value={"action": "HOLD", "status": "NO_THRESHOLD"}),
        )
        self.modules = patch.dict("sys.modules", {
            "real_trading_config": self.cfg,
            "real_trader": self.trader,
            "real_swing_sell_executor": self.swing,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def write_paper(self, positions):
        self.paper.write_text(json.dumps({"positions": positions}), encoding="utf-8")

    def test_disabled_is_inert(self):
        with patch.dict(os.environ, {"REAL_TRADING_ENABLED": "false"}):
            result = bridge.reconcile()
        self.assertEqual(result["status"], "disabled")
        self.trader.execute_buy.assert_not_called()

    def test_dry_run_bridge_does_not_reconcile_or_initialize(self):
        self.write_paper({TOKEN: {"symbol": "TEST"}})
        with patch.dict(os.environ, {"REAL_TRADING_DRY_RUN": "true"}):
            result = bridge.reconcile()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(self.state.exists())
        self.trader.execute_buy.assert_not_called()
        self.trader.execute_sell.assert_not_called()

    def test_real_bridge_refuses_strategy_drift(self):
        with patch.dict(os.environ, {"SDA_PAPER_TP4": "0"}):
            with self.assertRaisesRegex(RuntimeError, "requires the active PAPER profile"):
                bridge.reconcile()

    def test_real_buy_uses_configured_real_size_not_paper_size(self):
        self.cfg.position_sda = lambda value=None: 30.0 if value is None else float(value)
        bridge.reconcile()
        self.write_paper({TOKEN: {"symbol": "TEST", "investment_sda": 18.0, "opened_at": bridge._now()}})
        bridge.reconcile()
        self.trader.execute_buy.assert_called_once_with(TOKEN, "TEST", 30.0)

    def test_first_run_only_sets_baseline(self):
        self.write_paper({TOKEN: {"symbol": "TEST"}})
        result = bridge.reconcile()
        self.assertEqual(result["status"], "baseline_initialized")
        self.trader.execute_buy.assert_not_called()

    def test_new_paper_position_triggers_one_buy(self):
        bridge.reconcile()  # baseline empty
        self.write_paper({TOKEN: {"symbol": "TEST", "opened_at": bridge._now()}})
        result = bridge.reconcile()
        self.assertEqual(result["opened"], 1)
        self.trader.execute_buy.assert_called_once_with(TOKEN, "TEST", 20.0)
        self.assertEqual(result["actions"][0]["status"], "CONFIRMED")

    def test_pending_buy_is_recovered_from_real_ledger_after_bridge_checkpoint_loss(self):
        self.state.write_text(json.dumps({
            "initialized": True,
            "paper_positions": [TOKEN],
            "pending_buy": [],
            "pending_sell": [],
        }), encoding="utf-8")
        self.write_paper({})
        self.trader.load_state.return_value = {
            "positions": {TOKEN: {"symbol": "TEST", "token_amount_raw": 123}},
            "pending_buys": {TOKEN: {"symbol": "TEST"}},
            "pending_sells": {},
        }
        result = bridge.reconcile()
        self.trader.execute_buy.assert_called_once_with(TOKEN, "TEST", 20.0)
        self.trader.execute_sell.assert_not_called()
        self.assertEqual([a["action"] for a in result["actions"]], ["BUY"])

    def test_pending_buy_is_reconciled_and_closed_if_paper_exits_first(self):
        self.state.write_text(json.dumps({
            "initialized": True,
            "paper_positions": [TOKEN],
            "pending_buy": [TOKEN],
            "pending_sell": [],
        }), encoding="utf-8")
        self.write_paper({})
        self.trader.load_state.return_value = {
            "positions": {TOKEN: {"symbol": "TEST", "token_amount_raw": 123}},
            "pending_buys": {TOKEN: {"symbol": "TEST"}},
        }
        result = bridge.reconcile()
        self.trader.execute_buy.assert_called_once_with(TOKEN, "TEST", 20.0)
        self.trader.execute_sell.assert_not_called()
        self.assertEqual([a["action"] for a in result["actions"]], ["BUY"])

    def test_paper_close_does_not_trigger_real_sell(self):
        self.write_paper({TOKEN: {"symbol": "TEST"}})
        bridge.reconcile()  # baseline includes token
        self.write_paper({})
        result = bridge.reconcile()
        self.trader.execute_sell.assert_not_called()
        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["actions"], [])

    def test_legacy_bridge_pending_sell_is_archived_not_executed(self):
        self.state.write_text(json.dumps({
            "initialized": True, "paper_positions": [], "pending_buy": [],
            "pending_sell": [TOKEN],
        }), encoding="utf-8")
        result = bridge.reconcile()
        self.trader.execute_sell.assert_not_called()
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["pending_sell"], [])
        self.assertEqual(saved["legacy_pending_sell"][0]["token"], TOKEN)
        self.assertEqual(result["status"], "reconciled")


if __name__ == "__main__":
    unittest.main()
