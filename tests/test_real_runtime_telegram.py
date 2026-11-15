import unittest
from unittest.mock import patch

import real_bot_views as views


class RuntimeTelegramTests(unittest.TestCase):
    def setUp(self):
        views.clear_cache()

    def tearDown(self):
        views.clear_cache()

    def test_cache_invalidates_when_reconciled_runtime_state_changes(self):
        runtime = {
            "updated_at": "2026-11-15T08:00:00+00:00",
            "lastSuccessfulWalletSyncAt": "2026-11-15T08:00:00+00:00",
            "lastSuccessfulReconciliationAt": "2026-11-15T08:00:00+00:00",
        }

        class Dashboard:
            def load(self, path, default=None):
                return runtime if path == "real_trade_state.json" else default

        with patch.object(views, "_build_dashboard_data",
                          side_effect=[{"version": 1}, {"version": 2}]) as build:
            first = views._get_data(Dashboard())
            cached = views._get_data(Dashboard())
            self.assertIs(first, cached)
            runtime["updated_at"] = "2026-11-15T08:01:00+00:00"
            runtime["lastSuccessfulWalletSyncAt"] = "2026-11-15T08:01:00+00:00"
            refreshed = views._get_data(Dashboard())
        self.assertEqual(first["version"], 1)
        self.assertEqual(refreshed["version"], 2)
        self.assertEqual(build.call_count, 2)

    def test_runtime_accounting_includes_manual_buy_and_partial_sell(self):
        state = {
            "syncStatus": "FRESH", "walletSyncHealthy": True, "reconciliationHealthy": True,
            "lastSuccessfulWalletSyncAt": "2026-11-15T07:45:00+00:00",
            "lastSuccessfulReconciliationAt": "2026-11-15T07:45:00+00:00",
            "walletBalances": {"native_sda_wei": str(100 * 10**18), "wallet": "0xabc"},
            "positions": {
                "0x" + "a" * 40: {
                    "symbol": "TEST", "decimals": 18, "remaining_quantity": 7 * 10**18,
                    "remaining_cost_basis": "1.4", "buy_tx": "0xbuy", "source": "MANUAL",
                }
            },
            "trades": [
                {"action": "MANUAL_BUY", "source": "MANUAL", "tx": "0xbuy",
                 "token": "0x" + "a" * 40, "symbol": "TEST", "at": "2026-11-15T07:00:00+00:00",
                 "invested_sda": "2", "cost_basis_sda": "2"},
                {"action": "MANUAL_SELL", "source": "MANUAL", "tx": "0xsell",
                 "token": "0x" + "a" * 40, "symbol": "TEST", "at": "2026-11-15T07:30:00+00:00",
                 "buy_tx": "0xbuy", "proceeds_sda": "1.2", "cost_basis_sda": "0.6",
                 "net_pnl_sda": "0.599"},
            ],
        }
        accounting = views._runtime_accounting(state)
        self.assertEqual(accounting["bot_reconciliation_status"], "COMPLETE")
        self.assertEqual(accounting["open_positions"][0]["token"], "0x" + "a" * 40)
        self.assertEqual(accounting["buy_transactions"][0]["tx"], "0xbuy")
        self.assertEqual(accounting["sell_transactions"][0]["tx"], "0xsell")
        self.assertEqual(accounting["realized_pnl_sda"], "0.599")


if __name__ == "__main__":
    unittest.main()
