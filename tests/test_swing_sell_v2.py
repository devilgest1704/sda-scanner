import unittest
from decimal import Decimal

from swing_sell_v2 import (
    apply_confirmed_partial, decide, migrate_state,
    reconcile_wallet_quantity, update_trailing_peak,
)


class SwingSellV2PolicyTests(unittest.TestCase):
    def test_loss_boundaries(self):
        for pnl in ("-0.10", "-0.25", "-0.40", "-0.49", "-0.499"):
            self.assertEqual(decide({}, pnl, "1")["action"], "HOLD")
        self.assertEqual(decide({}, "-0.50", "1")["action"], "FULL_SELL")

    def test_profit_boundaries_and_one_time_partial(self):
        for pnl in ("0.10", "0.24", "0.249"):
            self.assertEqual(decide({}, pnl, "1")["action"], "HOLD")
        self.assertEqual(decide({}, "0.25", "1")["action"], "PARTIAL_SELL")
        already_partial = {"profit_partial_done": True, "trailing_armed": True,
                           "highest_price_since_trailing_activation": "1.30"}
        self.assertEqual(decide(already_partial, "0.25", "1.25")["action"], "HOLD")

    def test_hard_tp_and_direct_jump_are_full_sell(self):
        self.assertEqual(decide({}, "0.50", "1.5")["action"], "FULL_SELL")
        self.assertEqual(decide({}, "0.75", "1.75")["action"], "FULL_SELL")

    def test_trailing_peak_and_fifteen_percent_fall(self):
        pos = {"profit_partial_done": True, "trailing_armed": True,
               "highest_price_since_trailing_activation": "1.50"}
        self.assertEqual(decide(pos, "0.30", "1.275")["action"], "FULL_SELL")
        updated = update_trailing_peak(pos, "1.60")
        self.assertEqual(Decimal(updated["current_trailing_price"]), Decimal("1.3600"))

    def test_no_time_exit_after_48_hours_or_seven_days(self):
        for age in ("48 hours", "7 days"):
            self.assertEqual(decide({"age": age}, "0.10", "1")["action"], "HOLD")

    def test_partial_quantity_cost_basis_and_open_state(self):
        pos = {"token_amount_raw": 100, "amount_sda": 20,
               "initial_quantity": 100, "remaining_quantity": 100,
               "remaining_cost_basis": "20", "last_market_price": "1.25"}
        out = apply_confirmed_partial(pos, 20, "6", "2026-11-09T00:00:00Z")
        self.assertEqual(out["remaining_quantity"], 80)
        self.assertEqual(Decimal(out["remaining_cost_basis"]), Decimal("16"))
        self.assertEqual(Decimal(out["sold_cost_basis"]), Decimal("4"))
        self.assertEqual(out["status"], "OPEN")
        self.assertTrue(out["profit_partial_done"])
        self.assertTrue(out["trailing_armed"])

    def test_manual_full_and_partial_reconciliation(self):
        pos = {"remaining_quantity": 100, "remaining_cost_basis": "20"}
        full = reconcile_wallet_quantity(pos, 0)
        self.assertEqual(full["status"], "CLOSED_MANUAL_RECONCILED")
        partial = reconcile_wallet_quantity(pos, 60)
        self.assertEqual(partial["remaining_quantity"], 60)
        self.assertEqual(Decimal(partial["remaining_cost_basis"]), Decimal("12"))

    def test_legacy_pending_sells_are_archived_not_replayed(self):
        migrated = migrate_state({"pending_sells": {"0xabc": {"tx_hash": "old"}}},
                                 "2026-11-09T00:00:00Z")
        self.assertEqual(migrated["pending_sells"], {})
        self.assertEqual(len(migrated["legacy_pending_sells"]), 1)
        self.assertEqual(migrated["sell_migration_historical_transactions"], 0)
        self.assertEqual(migrated["sell_strategy_version"], "swing_v2")

    def test_unresolved_intent_prevents_duplicate(self):
        pos = {"sell_intent_id": "intent-1", "sell_intent_status": "BROADCAST_UNKNOWN"}
        self.assertEqual(decide(pos, "0.25", "1.25")["reason"], "SELL_INTENT_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
