import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import real_live_wallet_sync as live
import real_pnl_stats
import real_bot_views
import telegram_home


WALLET = "0x" + "a" * 40
TOKEN = "0x" + "b" * 40
SENDER = "0x" + "c" * 40
TRANSFER = live.TRANSFER_TOPIC
TOPIC_WALLET = live._address_topic(WALLET)
TOPIC_SENDER = live._address_topic(SENDER)


class FakeTrader:
    def __init__(self, state=None, fail_balance=False):
        self.state = copy.deepcopy(state or {"positions": {}, "trades": []})
        self.fail_balance = fail_balance
        self.saves = 0

    def load_state(self):
        return copy.deepcopy(self.state)

    def save_state(self, value):
        self.state = copy.deepcopy(value)
        self.saves += 1

    def wallet_address(self):
        return WALLET

    def native_balance(self, _wallet):
        return 100 * 10**18

    def token_balance(self, _token, _wallet):
        if self.fail_balance:
            raise TimeoutError("RPC timeout during balanceOf")
        return 10 * 10**18

    def token_decimals(self, _token):
        return 18

    def rpc(self, method, params):
        if method == "eth_chainId":
            return hex(live.cfg.CHAIN_ID)
        if method == "eth_blockNumber":
            return "0xa"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            txs = []
            if number == 10:
                txs = [{
                    "hash": "0x" + "d" * 64, "from": WALLET,
                    "to": live.cfg.ROUTER_ADDRESS, "value": hex(2 * 10**18),
                    "gasPrice": hex(1),
                }]
            return {"timestamp": hex(1_800_000_000 + number), "transactions": txs}
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1", "gasUsed": "0x5208", "effectiveGasPrice": "0x1",
                "logs": [{
                    "address": TOKEN,
                    "topics": [TRANSFER, TOPIC_SENDER, TOPIC_WALLET],
                    "data": hex(10 * 10**18), "logIndex": "0x2",
                }],
            }
        raise AssertionError(method)


class LiveWalletSyncTests(unittest.TestCase):
    def test_manual_buy_import_and_cursor_commit(self):
        trader = FakeTrader()
        result = live.sync_once(trader, now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertTrue(result["ok"])
        position = trader.state["positions"][TOKEN]
        self.assertEqual(position["source"], "MANUAL")
        self.assertTrue(position["sellManagementEnabled"])
        self.assertEqual(position["token_amount_raw"], 10 * 10**18)
        self.assertEqual(position["buy_tx"], "0x" + "d" * 64)
        self.assertEqual(position["amount_sda"], 2.0)
        self.assertEqual(trader.state["walletBalances"]["tokens_raw"][TOKEN], str(10 * 10**18))
        self.assertTrue(position["opened_at"])
        self.assertGreater(float(position["network_fee_sda"]), 0)
        self.assertAlmostEqual(float(position["execution_price_sda"]), 0.2, places=8)
        self.assertEqual(trader.state["lastProcessedBlock"], 10)
        self.assertEqual(len(trader.state["trades"]), 1)

    def test_duplicate_event_is_idempotent_after_restart(self):
        trader = FakeTrader()
        live.sync_once(trader)
        first = copy.deepcopy(trader.state)
        restarted = FakeTrader(first)
        result = live.sync_once(restarted)
        self.assertTrue(result["ok"])
        self.assertEqual(len(restarted.state["positions"]), 1)
        self.assertEqual(len(restarted.state["trades"]), 1)

    def test_balance_failure_does_not_advance_cursor(self):
        trader = FakeTrader({"positions": {}, "trades": {}, "lastProcessedBlock": 9},
                            fail_balance=True)
        original_cursor = trader.state["lastProcessedBlock"]
        result = live.sync_once(trader)
        self.assertFalse(result["ok"])
        self.assertEqual(trader.state["lastProcessedBlock"], original_cursor)
        self.assertFalse(trader.state["buyDataHealthy"])

    def test_successful_rpc_recovery_resets_retry_backoff(self):
        trader = FakeTrader({"positions": {}, "trades": [], "walletSyncFailures": 3,
                             "walletSyncRetryAfterSeconds": 60})
        result = live.sync_once(trader)
        self.assertTrue(result["ok"])
        self.assertEqual(trader.state["walletSyncFailures"], 0)
        self.assertNotIn("walletSyncRetryAfterSeconds", trader.state)

    def test_rpc_timeout_records_retry_without_cursor_advance(self):
        class TimeoutTrader(FakeTrader):
            def rpc(self, method, params):
                if method == "eth_chainId":
                    raise TimeoutError("RPC timeout")
                return super().rpc(method, params)
        trader = TimeoutTrader({"positions": {}, "trades": [], "lastProcessedBlock": 5})
        result = live.sync_once(trader)
        self.assertFalse(result["ok"])
        self.assertEqual(trader.state["lastProcessedBlock"], 5)
        self.assertGreaterEqual(trader.state["walletSyncRetryAfterSeconds"], 1)


    def test_manual_partial_sell_reconciles_proceeds_and_remaining_cost(self):
        state = {
            "positions": {TOKEN: {
                "token": TOKEN, "symbol": "TEST", "source": "MANUAL",
                "sellManagementEnabled": True, "buy_tx": "0xbuy",
                "remaining_quantity": 10 * 10**18, "token_amount_raw": 10 * 10**18,
                "remaining_cost_basis": "20", "amount_sda": 20,
            }},
            "trades": [], "lastProcessedBlock": 9,
        }
        class SellTrader(FakeTrader):
            def __init__(self):
                super().__init__(state)
            def rpc(self, method, params):
                if method == "eth_chainId":
                    return hex(live.cfg.CHAIN_ID)
                if method == "eth_blockNumber":
                    return "0xa"
                if method == "eth_getBlockByNumber":
                    return {"timestamp": "0x6a000000", "transactions": [{
                        "hash": "0x" + "f" * 64, "from": WALLET,
                        "to": live.cfg.ROUTER_ADDRESS, "value": "0x0", "gasPrice": "0x1",
                    }]}
                if method == "eth_getTransactionReceipt":
                    return {"status": "0x1", "gasUsed": "0x5208", "effectiveGasPrice": "0x1",
                        "logs": [
                            {"address": TOKEN, "topics": [TRANSFER, TOPIC_WALLET, TOPIC_SENDER],
                             "data": hex(5 * 10**18), "logIndex": "0x1"},
                            {"address": live.cfg.WSDA_ADDRESS, "topics": [TRANSFER, TOPIC_SENDER, TOPIC_WALLET],
                             "data": hex(12 * 10**18), "logIndex": "0x2"},
                        ]}
                raise AssertionError(method)
        trader = SellTrader()
        result = live.sync_once(trader)
        self.assertTrue(result["ok"])
        position = trader.state["positions"][TOKEN]
        self.assertEqual(position["remaining_quantity"], 5 * 10**18)
        self.assertEqual(position["remaining_cost_basis"], "10")
        sell = [x for x in trader.state["trades"] if x.get("action") == "MANUAL_SELL"]
        self.assertEqual(len(sell), 1)
        self.assertEqual(sell[0]["proceeds_sda"], "12")
        self.assertGreater(float(sell[0]["net_pnl_sda"]), 1.99)


    def test_receipt_verified_manual_sell_flows_into_realized_pnl(self):
        accounting = {"sell_transactions": [], "buy_transactions": [],
                      "excluded_external_activity": {}, "bot_unresolved_transactions": []}
        runtime = {"trades": [
            {"action": "MANUAL_BUY", "source": "MANUAL", "tx": "0xbuy", "at": "2026-11-14T10:00:00+00:00"},
            {"action": "MANUAL_SELL", "source": "MANUAL", "tx": "0xsell", "buy_tx": "0xbuy",
             "contract": TOKEN, "token": TOKEN, "at": "2026-11-14T11:00:00+00:00",
             "cost_basis_sda": "10", "proceeds_sda": "12", "net_pnl_sda": "1.99"},
        ]}
        model = real_pnl_stats.build_pnl_model(accounting, runtime, 0, True)
        self.assertEqual(len(model["events"]), 1)
        self.assertAlmostEqual(float(model["realized"]), 1.99, places=8)


    def test_telegram_capacity_uses_live_runtime_not_accounting_snapshot(self):
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "syncStatus": "FRESH", "walletSyncHealthy": True, "reconciliationHealthy": True,
            "lastSuccessfulWalletSyncAt": now,
            "walletBalances": {"native_sda_wei": str(50 * 10**18)},
            "positions": {TOKEN: {"remaining_quantity": 10, "remaining_cost_basis": "20"}},
            "pending_buys": {},
        }
        class Dashboard:
            def load(self, path, default):
                if path == "real_trade_state.json":
                    return state
                if path == "positions.json":
                    return {"positions": {}}
                if path == "real_accounting_state.json":
                    raise AssertionError("historical accounting snapshot must not be required")
                return default
        worker = {"syncStatus": "FRESH", "walletSyncHealthy": True,
                  "reconciliationHealthy": True, "lastSuccessfulWalletSyncAt": now,
                  "real_capital_cap_sda": 300, "real_max_open_positions": 10,
                  "real_buy_size_sda": 30}
        with patch.object(telegram_home.rb, "_load_worker_status", return_value=worker):
            capacity = telegram_home.real_capacity(Dashboard())
        self.assertIsNotNone(capacity)
        self.assertEqual(capacity["positions"], 1)
        self.assertEqual(capacity["deployed"], 20)
        self.assertEqual(capacity["available"], 50)

    def test_telegram_cache_invalidates_when_live_state_version_changes(self):
        state = {"updated_at": "v1"}
        dashboard = type("Dashboard", (), {"load": lambda _self, _path, _default: dict(state)})()
        calls = []
        with patch.object(real_bot_views, "_build_dashboard_data",
                          side_effect=lambda _dashboard: calls.append(len(calls)) or {"generation": len(calls)}):
            real_bot_views.clear_cache()
            first = real_bot_views._get_data(dashboard)
            state["updated_at"] = "v2"
            second = real_bot_views._get_data(dashboard)
        self.assertEqual(first["generation"], 1)
        self.assertEqual(second["generation"], 2)
        real_bot_views.clear_cache()

    def test_freshness_thresholds(self):
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        stamp = now.isoformat()
        state = {"lastSuccessfulWalletSyncAt": stamp}
        self.assertEqual(live.freshness(state, now)[0], "FRESH")
        from datetime import timedelta
        state["lastSuccessfulWalletSyncAt"] = (now - timedelta(seconds=180)).isoformat()
        self.assertEqual(live.freshness(state, now)[0], "DEGRADED")
        state["lastSuccessfulWalletSyncAt"] = (now - timedelta(seconds=301)).isoformat()
        self.assertEqual(live.freshness(state, now)[0], "STALE")


if __name__ == "__main__":
    unittest.main()
