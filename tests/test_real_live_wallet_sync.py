"""Regression tests for incremental, read-only REAL wallet reconciliation."""
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import real_live_wallet_sync as live
import real_trading_config as cfg
import real_trader

WALLET = "0x" + "a" * 40
POOL = "0x" + "c" * 40
TOKEN = "0x" + "b" * 40
TX_BUY = "0x" + "1" * 64
TX_SELL = "0x" + "2" * 64
TRANSFER = live.TRANSFER_TOPIC


def topic(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def word(value):
    return str(value).lower().replace("0x", "").rjust(64, "0")


def calldata(token_in, token_out, amount):
    return (cfg.BUY_SELECTOR + word(token_in) + word(token_out) + word(3000)
            + word(WALLET) + word(1_800_000_000) + word(amount) + word(0) + word(0))


def transfer_log(token, sender, recipient, amount, index):
    return {
        "address": token,
        "topics": [TRANSFER, topic(sender), topic(recipient)],
        "data": hex(amount), "logIndex": hex(index),
    }


class FakeTrader:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {"positions": {}, "trades": []})
        self.fail_native = False
        self.fail_chain = False
        self.tx_by_hash = {}
        self.receipt_by_hash = {}

    def load_state(self):
        return copy.deepcopy(self.state)

    def save_state(self, value):
        self.state = copy.deepcopy(value)

    def wallet_address(self):
        return WALLET

    def chain_id(self):
        if self.fail_chain:
            raise TimeoutError("RPC chain ID timeout")
        return cfg.CHAIN_ID

    def native_balance(self, _wallet):
        if self.fail_native:
            raise TimeoutError("RPC eth_getBalance timeout")
        return 100 * 10**18

    def token_decimals(self, _token):
        return 18

    def rpc(self, method, params):
        if method == "eth_getTransactionByHash":
            return self.tx_by_hash.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipt_by_hash.get(params[0])
        raise AssertionError(f"unexpected RPC method {method}")


class LiveWalletSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "sync.lock"
        self.lock_patch = patch.object(live, "LOCK_FILE", self.lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.now = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
        self.tx_row = {
            "hash": TX_BUY, "block_number": 101, "transaction_index": 0,
            "timestamp": "2026-11-15T07:59:30Z",
        }
        self.transfer_row = {
            "transaction_hash": TX_BUY, "block_number": 101, "index": 2,
            "timestamp": "2026-11-15T07:59:30Z",
        }
        self.token_payload = [{
            "token": {"address_hash": TOKEN, "decimals": "18", "symbol": "TST"},
            "value": str(10 * 10**18),
        }]
        self.payload_patch = patch.object(live, "_get_json", side_effect=self.explorer)
        self.payload_patch.start()
        self.addCleanup(self.payload_patch.stop)

    def explorer(self, path, params=None):
        params = params or {}
        if path.endswith("/transactions"):
            if params.get("filter") == "from":
                return {"items": [self.tx_row], "next_page_params": None}
            return {"items": [], "next_page_params": None}
        if path.endswith("/token-transfers"):
            return {"items": [self.transfer_row], "next_page_params": None}
        if path.endswith("/token-balances"):
            return self.token_payload
        raise AssertionError(path)

    def trader_with_buy(self, state=None):
        trader = FakeTrader(state or {"positions": {}, "trades": [], "lastProcessedBlock": 100})
        trader.tx_by_hash[TX_BUY] = {
            "hash": TX_BUY, "from": WALLET, "to": cfg.ROUTER_ADDRESS,
            "value": hex(2 * 10**18), "gasPrice": hex(10**9),
            "input": calldata(cfg.WSDA_ADDRESS, TOKEN, 2 * 10**18),
        }
        trader.receipt_by_hash[TX_BUY] = {
            "status": "0x1", "gasUsed": hex(21000), "effectiveGasPrice": hex(10**9),
            "logs": [transfer_log(TOKEN, POOL, WALLET, 10 * 10**18, 2)],
        }
        return trader

    def test_manual_buy_imports_exactly_once_and_commits_cursor(self):
        trader = self.trader_with_buy()
        result = live.sync_once(trader, now=self.now)
        self.assertTrue(result["ok"])
        position = trader.state["positions"][TOKEN]
        self.assertEqual(position["source"], "MANUAL")
        self.assertEqual(position["status"], "OPEN")
        self.assertTrue(position["sellManagementEnabled"])
        self.assertEqual(position["token_amount_raw"], 10 * 10**18)
        self.assertEqual(Decimal(position["initial_cost_basis"]), Decimal("2"))
        self.assertEqual(position["buy_tx"], TX_BUY)
        self.assertEqual(trader.state["lastProcessedBlock"], 101)
        self.assertEqual(trader.state["lastProcessedSignature"], TX_BUY)
        self.assertIn(TX_BUY, trader.state["processedBlockchainSignatures"])
        self.assertEqual(len(trader.state["trades"]), 1)
        self.assertEqual(result["imported"][0]["fees_sda"], "0.000021")

        # A restarted process sees the same indexed event but cannot import it again.
        restarted = FakeTrader(trader.state)
        restarted.tx_by_hash = trader.tx_by_hash
        restarted.receipt_by_hash = trader.receipt_by_hash
        again = live.sync_once(restarted, now=self.now)
        self.assertTrue(again["ok"])
        self.assertEqual(len(restarted.state["positions"]), 1)
        self.assertEqual(len(restarted.state["trades"]), 1)

    def test_manual_partial_sell_updates_quantity_cost_basis_and_realized_pnl(self):
        position = {
            "token": TOKEN, "symbol": "TST", "source": "MANUAL", "status": "OPEN",
            "sellManagementEnabled": True, "token_amount_raw": 10 * 10**18,
            "initial_quantity": 10 * 10**18, "remaining_quantity": 10 * 10**18,
            "initial_cost_basis": "2", "remaining_cost_basis": "2",
            "entry_gas_remaining_sda": "0.000021", "buy_tx": TX_BUY,
        }
        trader = FakeTrader({
            "positions": {TOKEN: position}, "trades": [], "lastProcessedBlock": 100,
            "walletBalances": {"tokens": {TOKEN: {"decimals": 18}}},
        })
        self.tx_row = {
            "hash": TX_SELL, "block_number": 101, "transaction_index": 1,
            "timestamp": "2026-11-15T07:59:45Z",
        }
        self.transfer_row = {
            "transaction_hash": TX_SELL, "block_number": 101, "index": 4,
            "timestamp": "2026-11-15T07:59:45Z",
        }
        self.token_payload = [{
            "token": {"address_hash": TOKEN, "decimals": "18", "symbol": "TST"},
            "value": str(7 * 10**18),
        }]
        trader.tx_by_hash[TX_SELL] = {
            "hash": TX_SELL, "from": WALLET, "to": cfg.ROUTER_ADDRESS,
            "value": "0x0", "gasPrice": hex(10**9),
            "input": calldata(TOKEN, cfg.WSDA_ADDRESS, 3 * 10**18),
        }
        trader.receipt_by_hash[TX_SELL] = {
            "status": "0x1", "gasUsed": hex(21000), "effectiveGasPrice": hex(10**9),
            "logs": [
                transfer_log(TOKEN, WALLET, POOL, 3 * 10**18, 3),
                transfer_log(cfg.WSDA_ADDRESS, POOL, WALLET, 12 * 10**17, 4),
            ],
        }
        result = live.sync_once(trader, now=self.now)
        self.assertTrue(result["ok"])
        updated = trader.state["positions"][TOKEN]
        self.assertEqual(updated["remaining_quantity"], 7 * 10**18)
        self.assertAlmostEqual(float(updated["remaining_cost_basis"]), 1.4, places=8)
        sell = next(x for x in trader.state["trades"] if x["action"] == "MANUAL_SELL")
        self.assertEqual(sell["buy_tx"], TX_BUY)
        self.assertAlmostEqual(float(sell["proceeds_sda"]), 1.2, places=8)
        self.assertAlmostEqual(float(sell["cost_basis_sda"]), 0.6, places=8)
        self.assertGreater(float(sell["net_pnl_sda"]), 0.59)
        self.assertEqual(trader.state["daily_realized_pnl_sda"], float(Decimal(sell["net_pnl_sda"])))

    def test_rpc_failure_never_advances_cursor_and_recovery_resets_backoff(self):
        trader = self.trader_with_buy({"positions": {}, "trades": [], "lastProcessedBlock": 100})
        trader.fail_native = True
        failed = live.sync_once(trader, now=self.now)
        self.assertFalse(failed["ok"])
        self.assertEqual(trader.state["lastProcessedBlock"], 100)
        self.assertEqual(trader.state["syncStatus"], "FAILED")
        self.assertGreater(trader.state["walletSyncRetryAfterSeconds"], 0)
        trader.fail_native = False
        recovered = live.sync_once(trader, now=self.now)
        self.assertTrue(recovered["ok"])
        self.assertEqual(trader.state["lastProcessedBlock"], 101)
        self.assertEqual(trader.state["walletSyncFailures"], 0)
        self.assertEqual(trader.state["walletSyncRetryAfterSeconds"], 0)

    def test_rpc_chain_timeout_keeps_cursor(self):
        trader = self.trader_with_buy({"positions": {}, "trades": [], "lastProcessedBlock": 100})
        trader.fail_chain = True
        result = live.sync_once(trader, now=self.now)
        self.assertFalse(result["ok"])
        self.assertEqual(trader.state["lastProcessedBlock"], 100)

    def test_real_buy_gate_blocks_stale_and_allows_fresh_reconciliation(self):
        base = {
            "positions": {}, "pending_buys": {}, "daily_realized_pnl_sda": 0,
            "syncStatus": "STALE", "walletSyncHealthy": False,
            "reconciliationHealthy": False,
            "lastSuccessfulWalletSyncAt": "2026-11-15T07:00:00+00:00",
            "lastSyncError": "RPC timeout",
        }
        with patch.object(real_trader.cfg, "REAL_TRADING_ENABLED", True), \
             patch.object(real_trader.cfg, "REAL_TRADING_DRY_RUN", False):
            with self.assertRaisesRegex(RuntimeError, "REAL BUY blocked"):
                real_trader.safety_check(10, copy.deepcopy(base))
            fresh = copy.deepcopy(base)
            fresh.update({
                "syncStatus": "FRESH", "walletSyncHealthy": True,
                "reconciliationHealthy": True,
                "lastSuccessfulWalletSyncAt": self.now.isoformat(),
                "lastSyncError": None,
            })
            real_trader.safety_check(10, fresh)

    def test_fresh_degraded_stale_thresholds(self):
        state = {"syncStatus": "FRESH", "lastSuccessfulWalletSyncAt": self.now.isoformat()}
        self.assertEqual(live.freshness(state, self.now)[0], "FRESH")
        from datetime import timedelta
        state["lastSuccessfulWalletSyncAt"] = (self.now - timedelta(seconds=180)).isoformat()
        self.assertEqual(live.freshness(state, self.now)[0], "DEGRADED")
        state["lastSuccessfulWalletSyncAt"] = (self.now - timedelta(seconds=301)).isoformat()
        self.assertEqual(live.freshness(state, self.now)[0], "STALE")

    def test_failure_does_not_advance_signature_or_processed_event_cursor(self):
        trader = self.trader_with_buy({"positions": {}, "trades": [], "lastProcessedBlock": 100,
                                       "lastProcessedSignature": "0xold"})
        trader.fail_native = True
        live.sync_once(trader, now=self.now)
        self.assertEqual(trader.state["lastProcessedSignature"], "0xold")
        self.assertEqual(trader.state.get("processedBlockchainSignatures", []), [])


if __name__ == "__main__":
    unittest.main()
