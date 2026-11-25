import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import real_trader
import tools.real_buy_pipeline as buy_pipeline


WALLET = "0x" + "1" * 40
TOKEN = "0x" + "a" * 40


class PendingBuyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "real_trade_state.json"
        self.cfg_path = patch.object(real_trader.cfg, "STATE_FILE", str(self.state_path))
        self.cfg_path.start()
        self.addCleanup(self.cfg_path.stop)
        self.state_path.write_text(json.dumps({
            "positions": {},
            "pending_buys": {
                TOKEN: {
                    "tx_hash": "0xalready-broadcast",
                    "wallet": WALLET,
                    "symbol": "TEST",
                    "position_sda": 30,
                    "plan": {"position_sda": 30, "quoted_out_raw": 100, "minimum_out_raw": 99},
                    "token_balance_before": 0,
                    "gas": 100000,
                    "gas_price": 1,
                }
            },
            "pending_sells": {},
            "trades": [],
        }), encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_unconfirmed_buy_stays_pending_and_is_never_rebroadcast(self):
        with patch.object(real_trader, "rpc", return_value=None), \
             patch.object(real_trader, "send_transaction") as send:
            results = real_trader.recover_pending_buys()
        self.assertEqual(results[0]["status"], "PENDING_CONFIRMATION")
        self.assertIn(TOKEN, real_trader.load_state()["pending_buys"])
        send.assert_not_called()

    def test_confirmed_buy_is_finalized_without_new_transaction(self):
        account = SimpleNamespace(address=WALLET)
        finalized = {"action": "BUY", "status": "CONFIRMED", "tx": "0xalready-broadcast"}
        with patch.object(real_trader, "rpc", return_value={"status": "0x1"}), \
             patch.object(real_trader, "chain_id", return_value=real_trader.cfg.CHAIN_ID), \
             patch.object(real_trader, "private_account", return_value=account), \
             patch.object(real_trader, "_finalize_confirmed_buy", return_value=finalized) as finish, \
             patch.object(real_trader, "send_transaction") as send:
            results = real_trader.recover_pending_buys()
        self.assertEqual(results[0]["status"], "CONFIRMED")
        finish.assert_called_once()
        send.assert_not_called()

    def test_missing_hash_requires_manual_reconciliation(self):
        state = real_trader.load_state()
        state["pending_buys"][TOKEN].pop("tx_hash")
        real_trader.save_state(state)
        with patch.object(real_trader, "rpc") as rpc, \
             patch.object(real_trader, "send_transaction") as send:
            results = real_trader.recover_pending_buys()
        self.assertEqual(results[0]["status"], "MANUAL_RECONCILIATION_REQUIRED")
        rpc.assert_not_called()
        send.assert_not_called()

    def test_startup_recovery_runs_even_when_market_data_is_degraded(self):
        recovery = [{"action": "BUY_RECOVERY", "token": TOKEN, "status": "PENDING_CONFIRMATION"}]
        with patch.dict("os.environ", {
            "REAL_TRADING_ENABLED": "true",
            "SDA_SCANNER_BUY_DATA_AVAILABLE": "false",
        }), patch.object(buy_pipeline.real_trader, "recover_pending_buys", return_value=recovery) as recover, \
             patch.object(buy_pipeline, "_mark_unavailable"):
            result = buy_pipeline.run_scan(market_path=str(Path(self.tmp.name) / "missing.json"))
        recover.assert_called_once()
        self.assertEqual(result["status"], "market_data_degraded")
        self.assertEqual(result["actions"], recovery)

    def test_reverted_claim_is_released_but_hash_is_retained(self):
        claim = {
            "token": TOKEN,
            "status": "BROADCAST_PENDING",
            "tx_hash": "0xreverted",
            "position_sda": 30,
            "expires_at": "2026-11-25T00:00:00+00:00",
        }
        state = {
            "claims": {"signal-1": claim},
            "consumed_signal_ids": [],
        }
        with patch.object(buy_pipeline.real_trader, "rpc", return_value={"status": "0x0"}):
            buy_pipeline._reconcile_claims(state, {"positions": {}, "pending_buys": {}, "trades": []})
        self.assertEqual(claim["status"], "FAILED_RETRYABLE")
        self.assertEqual(claim["tx_hash"], "0xreverted")
        self.assertEqual(claim["reconciliation"], "REVERTED_REQUIRES_RECONCILIATION")


if __name__ == "__main__":
    unittest.main()
