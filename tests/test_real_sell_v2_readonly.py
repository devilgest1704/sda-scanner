"""Read-only Swing SELL V2 projection tests; no wallet, RPC, or transaction calls."""
import unittest
from datetime import datetime, timedelta, timezone

import real_bot_menu


Q = 100 * 10**18
POSITION = {
    "initial_quantity": Q,
    "remaining_quantity": Q,
    "initial_cost_basis": "20",
    "remaining_cost_basis": "20",
    "amount_sda": 20,
    "profit_partial_done": False,
    "trailing_armed": False,
}


class SellV2ReadOnlyTests(unittest.TestCase):
    def preview(self, price, **overrides):
        position = {**POSITION, **overrides}
        return real_bot_menu._sell_v2_preview(position, price, 18)

    def test_unavailable_price_is_read_only_action(self):
        result = self.preview(None)
        self.assertEqual(result["action"], "PRICE_UNAVAILABLE")
        self.assertIsNone(result["pnl_pct"])

    def test_emergency_partial_and_take_profit_actions(self):
        self.assertEqual(self.preview(0.10)["action"], "FULL_SELL_EMERGENCY")
        self.assertEqual(self.preview(0.25)["action"], "PARTIAL_SELL_20")
        self.assertEqual(self.preview(0.30)["action"], "FULL_SELL_TP")

    def test_trailing_hold_and_exit(self):
        trailing = {
            "profit_partial_done": True,
            "trailing_armed": True,
            "highest_price_since_trailing_activation": "0.30",
        }
        self.assertEqual(self.preview(0.27, **trailing)["action"], "TRAILING_HOLD")
        result = self.preview(0.25, **trailing)
        self.assertEqual(result["action"], "FULL_SELL_TRAILING")
        self.assertAlmostEqual(result["trailing_price"], 0.255)

    def test_wallet_reconciliation_quarantines(self):
        result = self.preview(0.30, wallet_reconciliation_required=True)
        self.assertEqual(result["action"], "SELL_QUARANTINED")

    def test_scanner_price_requires_fresh_snapshot_and_trade(self):
        now = datetime.now(timezone.utc)
        token = "0x" + "a" * 40
        fresh = {
            "updated_at": now.isoformat(),
            "tokens": {token: {
                "price_in_sda": 0.25,
                "last_transaction": (now - timedelta(seconds=90)).isoformat(),
            }},
        }
        result = real_bot_menu._scanner_snapshot_price(token, fresh, now=now)
        self.assertEqual(result["price_sda"], 0.25)
        self.assertLessEqual(result["age_seconds"], 300)

        stale_trade = {
            "updated_at": now.isoformat(),
            "tokens": {token: {
                "price_in_sda": 0.25,
                "last_transaction": (now - timedelta(minutes=8)).isoformat(),
            }},
        }
        self.assertIsNone(real_bot_menu._scanner_snapshot_price(token, stale_trade, now=now))

        stale_snapshot = {
            "updated_at": (now - timedelta(minutes=8)).isoformat(),
            "tokens": fresh["tokens"],
        }
        self.assertIsNone(real_bot_menu._scanner_snapshot_price(token, stale_snapshot, now=now))


if __name__ == "__main__":
    unittest.main()
