"""PAPER state safety for the PAPER -> REAL BUY bridge; trader and executor are mocks."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import real_paper_bridge as bridge

TOKEN_A = "0x" + "a" * 40
TOKEN_B = "0x" + "b" * 40
HELD = "0x" + "c" * 40


def _stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class PaperStateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paper = root / "positions.json"
        self.state_file = root / "bridge_state.json"
        self.real = {"positions": {HELD: {"symbol": "HELD", "token_amount_raw": 100}},
                     "pending_buys": {}, "pending_sells": {}}
        self.limit_reached = False
        self.trader = SimpleNamespace(
            execute_buy=Mock(side_effect=self._buy),
            execute_sell=Mock(side_effect=AssertionError("REAL SELL from PAPER")),
            load_state=lambda: self.real,
            wallet_address=lambda: "0x" + "9" * 40,
        )
        self.swing = SimpleNamespace(
            migrate_if_needed=Mock(return_value=False),
            startup_recovery=Mock(return_value=[]),
            reconcile_position_wallet=Mock(return_value={"status": "UNCHANGED"}),
            reconcile_position=Mock(return_value={"intent": None, "wallet": {"status": "UNCHANGED"}}),
            evaluate_position=Mock(return_value={"action": "HOLD", "status": "NO_THRESHOLD"}),
        )
        cfg = SimpleNamespace(position_sda=lambda v=None: max(10.0, min(20.0, 20.0 if v is None else float(v))),
                              POSITION_DEFAULT_SDA=20.0, WSDA_ADDRESS="0x" + "e" * 40)
        market = SimpleNamespace(
            load_market_snapshot=lambda: {}, load_token_metadata=lambda: {}, newest_supabase_price=None,
            resolve_market_price=lambda *a, **k: {"price_sda": 1.0, "timestamp": _stamp(), "age_seconds": 0,
                                                  "source": "mock"})
        for item in (
            patch.object(bridge, "PAPER_FILE", self.paper),
            patch.object(bridge, "STATE_FILE", self.state_file),
            patch.object(bridge, "EVENTS_FILE", root / "events.json"),
            patch.dict(os.environ, {"REAL_TRADING_ENABLED": "true", "REAL_TRADING_DRY_RUN": "false",
                                    "SDA_PAPER_BUY_MODE": "temporal_recovery", "SDA_PAPER_TP4": "1"}),
            patch.dict("sys.modules", {"real_trader": self.trader, "real_swing_sell_executor": self.swing,
                                       "real_trading_config": cfg, "real_market_price": market}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.write({})
        self.assertEqual(bridge.reconcile()["status"], "baseline_initialized")

    def _buy(self, token, symbol, amount):
        if token in self.real["positions"]:
            return {"status": "ALREADY_OPEN"}
        if self.limit_reached:
            raise RuntimeError("Maximum real open positions reached")
        self.real["positions"][token] = {"symbol": symbol, "token_amount_raw": 100}
        return {"status": "CONFIRMED", "tx": "0xtx" + token[2:6]}

    def write(self, positions):
        self.paper.write_text(json.dumps({"positions": positions}), encoding="utf-8")

    def baseline(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))["paper_positions"]

    def buys(self):
        return [c.args[0] for c in self.trader.execute_buy.call_args_list]

    def run_bridge(self):
        return bridge.reconcile()

    def _open_a(self):
        self.write({TOKEN_A: {"symbol": "A", "investment_sda": 20, "opened_at": _stamp(1)}})
        return self.run_bridge()

    def test_01_valid_paper_new_token_creates_one_real_buy(self):
        result = self._open_a()
        self.assertEqual(self.buys(), [TOKEN_A])
        self.assertEqual(result["paper_state"], "VALID")
        self.assertEqual(self.baseline(), [TOKEN_A])

    def _assert_unavailable_pass(self, reason_prefix):
        result = self.run_bridge()
        self.assertEqual(result["status"], "paper_state_unavailable")
        self.assertTrue(result["paper_state_error"].startswith(reason_prefix))
        self.assertEqual(self.buys(), [TOKEN_A])
        self.assertEqual(self.baseline(), [TOKEN_A])
        self.assertEqual([a["action"] for a in result["actions"] if a["action"] == "BUY"], [])
        self.assertIn("PAPER_STATE_UNAVAILABLE", [a["action"] for a in result["actions"]])
        self.trader.execute_sell.assert_not_called()
        return result

    def test_02_unreadable_paper_preserves_baseline_and_buys_nothing(self):
        self._open_a()
        self.paper.write_bytes(b"\xff\xfe\x00broken")
        self._assert_unavailable_pass("PAPER_")

    def test_03_next_valid_scan_with_known_token_does_not_rebuy(self):
        self._open_a()
        self.paper.write_text("{", encoding="utf-8")
        self._assert_unavailable_pass("PAPER_JSON_MALFORMED")
        self._open_a()
        self.assertEqual(self.buys(), [TOKEN_A])

    def test_04_after_invalid_state_only_genuinely_new_token_is_new(self):
        self._open_a()
        self.paper.unlink()
        self._assert_unavailable_pass("PAPER_FILE_MISSING")
        self.write({TOKEN_A: {"symbol": "A", "opened_at": _stamp(30)},
                    TOKEN_B: {"symbol": "B", "opened_at": _stamp(1)}})
        result = self.run_bridge()
        self.assertEqual(self.buys(), [TOKEN_A, TOKEN_B])
        self.assertEqual([a["token"] for a in result["actions"] if a["action"] == "BUY"], [TOKEN_B])
        self.assertEqual(sorted(self.baseline()), [TOKEN_A, TOKEN_B])

    def test_05_missing_paper_buys_nothing_and_sell_v2_still_runs(self):
        self._open_a()
        self.swing.evaluate_position.reset_mock()
        self.paper.unlink()
        result = self._assert_unavailable_pass("PAPER_FILE_MISSING")
        evaluated = sorted(c.args[0] for c in self.swing.evaluate_position.call_args_list)
        self.assertEqual(evaluated, sorted([HELD, TOKEN_A]))
        self.assertEqual(result["sell_health"], "SELL_HOLD")

    def test_06_malformed_json_keeps_baseline(self):
        self._open_a()
        self.paper.write_text('{"positions": {"0xabc": ', encoding="utf-8")
        self._assert_unavailable_pass("PAPER_JSON_MALFORMED")

    def test_07_positions_wrong_type_or_missing_keeps_baseline(self):
        self._open_a()
        self.paper.write_text(json.dumps({"positions": []}), encoding="utf-8")
        self._assert_unavailable_pass("PAPER_POSITIONS_WRONG_TYPE")
        self.paper.write_text(json.dumps({"closed_trades": []}), encoding="utf-8")
        self._assert_unavailable_pass("PAPER_POSITIONS_MISSING")
        self.paper.write_text(json.dumps([]), encoding="utf-8")
        self._assert_unavailable_pass("PAPER_LEDGER_NOT_A_DICT")

    def test_08_fresh_paper_position_is_bought(self):
        self.write({TOKEN_A: {"symbol": "A", "opened_at": _stamp(14)}})
        self.run_bridge()
        self.assertEqual(self.buys(), [TOKEN_A])

    def test_09_stale_newly_discovered_paper_position_is_skipped(self):
        opened = _stamp(16)
        self.write({TOKEN_A: {"symbol": "A", "opened_at": opened}})
        result = self.run_bridge()
        self.assertEqual(self.buys(), [])
        skipped = [a for a in result["actions"] if a.get("status") == "STALE_PAPER_BUY_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["token"], TOKEN_A)
        self.assertEqual(skipped[0]["paper_opened_at"], opened)
        self.assertGreater(skipped[0]["age_seconds"], 15 * 60)
        self.assertIn("detected_at", skipped[0])
        self.assertEqual(json.loads(self.paper.read_text())["positions"][TOKEN_A]["opened_at"], opened)
        self.run_bridge()
        self.assertEqual(self.buys(), [])

    def test_09b_missing_opened_at_is_not_bought(self):
        self.write({TOKEN_A: {"symbol": "A"}})
        result = self.run_bridge()
        self.assertEqual(self.buys(), [])
        self.assertEqual(result["actions"][0]["status"], "STALE_PAPER_BUY_SKIPPED")

    def test_10_and_11_real_limit_skip_is_never_replayed(self):
        self.limit_reached = True
        self.write({TOKEN_A: {"symbol": "A", "opened_at": _stamp(1)}})
        result = self.run_bridge()
        statuses = [a["status"] for a in result["actions"] if a["action"] == "BUY"]
        self.assertEqual(statuses, ["PAPER_BUY_SKIPPED_REAL_LIMIT"])
        self.limit_reached = False
        for _ in range(3):
            result = self.run_bridge()
            self.assertEqual([a for a in result["actions"] if a["action"] == "BUY"], [])
        self.assertEqual(self.buys(), [TOKEN_A])
        self.assertNotIn(TOKEN_A, self.real["positions"])

    def test_12_real_buy_without_new_paper_position_is_impossible(self):
        self._open_a()
        for _ in range(3):
            self.run_bridge()
        self.paper.write_text("{", encoding="utf-8")
        self.run_bridge()
        self.assertEqual(self.buys(), [TOKEN_A])

    def test_13_paper_close_does_not_sell_real(self):
        self._open_a()
        self.write({})
        result = self.run_bridge()
        self.assertEqual(result["closed"], 1)
        self.trader.execute_sell.assert_not_called()
        self.assertIn(TOKEN_A, self.real["positions"])
        self.assertIn(TOKEN_A, [c.args[0] for c in self.swing.evaluate_position.call_args_list])

    def test_14_sell_v2_evaluates_real_positions_while_paper_unavailable(self):
        self.paper.write_text("not json", encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["paper_state"], "UNAVAILABLE")
        self.assertEqual([c.args[0] for c in self.swing.evaluate_position.call_args_list], [HELD])
        self.swing.startup_recovery.assert_called_once()
        self.assertEqual(self.buys(), [])

    def test_scanner_wrapper_runs_real_bridge_when_paper_engine_raises(self):
        import main
        import tools.paper_telemetry_report as telemetry

        reconcile = Mock(return_value={"status": "reconciled", "actions": []})
        previous_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as working_directory:
            try:
                os.chdir(working_directory)
                with patch.object(main.engine, "main", side_effect=RuntimeError("paper failed")), \
                        patch.object(main._v30, "write_scan_funnel"), \
                        patch.object(telemetry, "report", return_value={"status": "ok"}), \
                        patch.dict("sys.modules", {"real_paper_bridge": SimpleNamespace(reconcile=reconcile)}):
                    with self.assertRaisesRegex(RuntimeError, "paper failed"):
                        main._run_with_paper_telemetry()
            finally:
                os.chdir(previous_cwd)
        reconcile.assert_called_once_with()

    def test_invalid_paper_never_initializes_a_baseline(self):
        self.state_file.unlink()
        self.paper.write_text("{", encoding="utf-8")
        result = self.run_bridge()
        self.assertEqual(result["status"], "paper_state_unavailable")
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.buys(), [])

    def test_pending_real_buy_is_preserved_during_paper_failure(self):
        state = json.loads(self.state_file.read_text())
        state["pending_buy"] = [TOKEN_B]
        self.state_file.write_text(json.dumps(state))
        self.paper.write_text("{", encoding="utf-8")
        self.run_bridge()
        self.assertEqual(json.loads(self.state_file.read_text())["pending_buy"], [TOKEN_B])
        self.assertEqual(self.buys(), [])


if __name__ == "__main__":
    unittest.main()
