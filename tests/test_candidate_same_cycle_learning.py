"""Regression: collect original same-cycle rejected BUYs and verified later swaps.

No real order placement; never create a future feature, hindsight peak, or fake fill.
"""
import unittest
from datetime import datetime, timedelta, timezone

from tools.candidate_price_tracker import update
from tools.missed_pump_learning import normalize

START = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
ADDRESS = "0x" + "a" * 40


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def snapshot(decision_seconds=20):
    return {
        "at": stamp(decision_seconds),
        "address": ADDRESS,
        "quote_at": stamp(-60),
        "phase": "WATCH",
        "price_sda": 1.0,
        "score": 76,
        "quality": 70,
        "impulse": 0,
        "snapshot_impulse": 12,
        "m1h": 4,
        "m15": 2.5,
        "m4h": 12,
        "trades_1h": 8,
        "buy_ratio": 2.2,
        "volume_1h": 1500,
        "flow_1h": 800,
        "flow_15m": 500,
        "eligible": False,
        "gates": ["lifecycle", "score"],
    }


def market(now_seconds, price=1.0):
    return {
        "updated_at": stamp(now_seconds),
        "tokens": {ADDRESS: {"analysis": {
            "last_transaction": stamp(now_seconds),
            "price_in_sda": price,
        }}},
    }


def initial_funnel(market_at=None, decision_seconds=20, funnel_seconds=30):
    return {"at": stamp(funnel_seconds),
            "market_at": market_at,
            "candidate_snapshots": [snapshot(decision_seconds)]}


def swap(seconds, price):
    return {"timestamp": stamp(seconds), "price": price,
            "type": "buy", "volume": 100.0,
            "hash": f"real-swap-{seconds}"}


class SourceLinkedMissedBuyTest(unittest.TestCase):
    def test_current_cycle_captures_frozen_rejected_buy_immediately(self):
        original = update({"funnels": [initial_funnel(market_at=stamp(0))]},
                          market(0))
        self.assertEqual(len(original["observations"]), 1)
        row = original["observations"][0]
        self.assertEqual(row["at"], stamp(20))
        self.assertEqual(row["quote_at"], stamp(-60))
        self.assertEqual(row["entry_price_sda"], 1.0)
        self.assertEqual(row["features"]["m4h"], 12)
        self.assertNotIn("60m", row["outcomes"])

    def test_unlinked_future_and_oversized_delay_fail_closed(self):
        for funnel in (
            initial_funnel(market_at=None),
            initial_funnel(market_at=stamp(-1)),
            initial_funnel(market_at=stamp(0), decision_seconds=130,
                           funnel_seconds=140),
            initial_funnel(market_at=stamp(0), decision_seconds=40,
                           funnel_seconds=30),
        ):
            with self.subTest(funnel=funnel):
                self.assertEqual(
                    update({"funnels": [funnel]}, market(0))["observations"],
                    [],
                )

    def test_verified_later_transaction_matures_without_current_price(self):
        initial = update({"funnels": [initial_funnel(market_at=stamp(0))]},
                         market(0))
        later = market(90 * 60, price=None)
        history = {
            "updated_at": later["updated_at"],
            "tokens": {ADDRESS: {"transactions": [
                swap(60 * 60 + 25, 1.06),
                swap(60 * 60 + 65, 2.50),
                swap(90 * 60, 0.70),
            ]}},
        }
        result = update({"observations": initial["observations"]},
                        later, history_market=history)
        row = result["observations"][0]
        self.assertEqual(row["outcomes"]["60m"]["price_sda"], 1.06)
        self.assertEqual(row["outcomes"]["60m"]["tx_hash"],
                         "real-swap-3625")
        self.assertEqual(row["outcomes"]["60m"]["source"],
                         "observed_later_market_transaction_not_executable_fill")
        self.assertEqual(row["entry_price_sda"], 1.0)
        self.assertEqual(len(normalize(result["observations"])), 1)

    def test_observed_four_hour_pump_peak_is_not_a_model_exit_fill(self):
        initial = update({"funnels": [initial_funnel(market_at=stamp(0))]},
                         market(0))
        later = market(5 * 3600, price=None)
        history = {
            "updated_at": later["updated_at"],
            "tokens": {ADDRESS: {"transactions": [
                swap(60 * 60 + 25, 1.03),
                swap(95 * 60, 2.4),
                swap(130 * 60, 0.80),
                swap(250 * 60, 9.0),  # outside the 240m peak window
            ]}},
        }
        result = update({"observations": initial["observations"]},
                        later, history_market=history)
        row = result["observations"][0]
        self.assertEqual(row["outcomes"]["60m"]["price_sda"], 1.03)
        self.assertEqual(row["observed_peak_240m"]["price_return_pct"], 140.0)
        self.assertEqual(row["observed_peak_240m"]["tx_hash"],
                         "real-swap-5700")
        self.assertIn("not_executable_fill",
                      row["observed_peak_240m"]["source"])
        self.assertEqual(row["entry_price_sda"], 1.0)
        self.assertEqual(len(normalize(result["observations"])), 1)

    def test_wrong_history_cycle_never_labels_missing_current_price(self):
        initial = update({"funnels": [initial_funnel(market_at=stamp(0))]},
                         market(0))
        later = market(90 * 60, price=None)
        history = {"updated_at": stamp(90 * 60 + 1),
                   "tokens": {ADDRESS: {"transactions": [
                       swap(60 * 60 + 25, 99.0),
                   ]}}}
        result = update({"observations": initial["observations"]},
                        later, history_market=history)
        self.assertNotIn("60m", result["observations"][0]["outcomes"])


if __name__ == "__main__":
    unittest.main()
