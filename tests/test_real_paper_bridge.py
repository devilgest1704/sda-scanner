"""Unit tests for the PAPER -> REAL transition bridge (no RPC or wallet needed)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import real_paper_bridge as bridge
import real_wallet_reconciliation


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
            save_state=unittest.mock.Mock(),
        )
        self.swing = SimpleNamespace(
            migrate_if_needed=unittest.mock.Mock(return_value=True),
            reconcile_position_wallet=unittest.mock.Mock(return_value={"status": "UNCHANGED"}),
            startup_recovery=unittest.mock.Mock(return_value=[]),
            _request_accounting_refresh=unittest.mock.Mock(return_value="DISPATCHED"),
            evaluate_position=unittest.mock.Mock(return_value={"action": "HOLD", "status": "NO_THRESHOLD"}),
        )
        self.modules = patch.dict("sys.modules", {
            "real_trading_config": self.cfg,
            "real_trader": self.trader,
            "real_swing_sell_executor": self.swing,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.wallet_sync_patch = patch.object(
            real_wallet_reconciliation, "reconcile_manual_buys",
            return_value={"ok": True, "syncStatus": "FRESH", "imported": [],
                          "lastSuccessfulWalletSync": "2026-11-14T08:00:00+00:00",
                          "lastSuccessfulReconciliation": "2026-11-14T08:00:00+00:00"})
        self.wallet_sync_patch.start()
        self.addCleanup(self.wallet_sync_patch.stop)

    def write_paper(self, positions):
        self.paper.write_text(json.dumps({"positions": positions}), encoding="utf-8")

    def test_stale_snapshot_dispatches_readonly_refresh(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": stale,
        }
        bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.swing._request_accounting_refresh.assert_called_once()
        dispatched_state = self.swing._request_accounting_refresh.call_args.args[0]
        self.assertTrue(dispatched_state["accounting_refresh_pending"])

    def test_fresh_snapshot_does_not_dispatch_refresh(self):
        fresh = datetime.now(timezone.utc).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": fresh,
            "lastSuccessfulWalletSync": fresh,
            "lastSuccessfulReconciliation": fresh,
            "syncStatus": "FRESH",
        }
        result = bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.assertEqual(result, "FRESH")
        self.swing._request_accounting_refresh.assert_not_called()

    def test_refresh_is_requested_after_five_minutes(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": stale,
        }
        result = bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.assertEqual(result, "DISPATCHED")
        dispatched_state = self.swing._request_accounting_refresh.call_args.args[0]
        self.assertEqual(dispatched_state["accounting_refresh_requested_from_snapshot_at"], stale)

    def test_refresh_retries_after_two_minutes_until_snapshot_is_reconciled(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        requested = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        attempted = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": stale,
            "accounting_refresh_requested_from_snapshot_at": requested,
            "accounting_refresh_requested_at": requested,
            "accounting_refresh_last_attempt_at": attempted,
        }
        result = bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.assertEqual(result, "DISPATCHED")
        self.swing._request_accounting_refresh.assert_called_once()

    def test_refresh_waits_until_two_minute_retry_window(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        attempted = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": stale,
            "accounting_refresh_requested_from_snapshot_at": stale,
            "accounting_refresh_last_attempt_at": attempted,
        }
        result = bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.assertEqual(result, "RETRY_WAIT")
        self.swing._request_accounting_refresh.assert_not_called()

    def test_refresh_only_clears_after_new_snapshot_and_reconciliation(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=8)).isoformat()
        generated = (now - timedelta(minutes=1)).isoformat()
        self.trader.load_state.return_value = {
            "positions": {}, "lastBlockchainSnapshotGeneratedAt": generated,
            "accounting_refresh_requested_from_snapshot_at": old,
            "lastSuccessfulWalletSync": now.isoformat(),
            "lastSuccessfulReconciliation": now.isoformat(),
            "syncStatus": "FRESH",
        }
        result = bridge._request_snapshot_refresh_if_stale(self.trader, self.swing)
        self.assertEqual(result, "REFRESH_CONFIRMED")
        saved = self.trader.save_state.call_args.args[0]
        self.assertNotIn("accounting_refresh_requested_from_snapshot_at", saved)
        self.swing._request_accounting_refresh.assert_not_called()

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

    def test_bridge_never_reads_paper_or_executes_buys(self):
        self.paper.unlink()
        result = bridge.reconcile()
        self.trader.execute_buy.assert_not_called()
        self.assertEqual(result["paper_state"], "NOT_USED")

    def test_real_pending_buy_is_not_replayed_by_sell_bridge(self):
        self.trader.load_state.return_value = {
            "positions": {TOKEN: {"symbol": "TEST", "token_amount_raw": 123}},
            "pending_buys": {TOKEN: {"symbol": "TEST"}},
            "pending_sells": {},
        }
        result = bridge.reconcile()
        self.trader.execute_buy.assert_not_called()
        self.assertEqual(result["paper_state"], "NOT_USED")

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
