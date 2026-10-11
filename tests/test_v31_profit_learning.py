import copy
import unittest
from datetime import datetime, timedelta, timezone

from tools.v31_profit_learning import (
    analyze, earlier_exit, net_exit, observed_trade, quote_path, EXIT_POLICIES,
)


def trade(index, prices=(1.0, 1.05, 0.99), with_quote_age=True):
    started = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    closed = started + timedelta(minutes=2 * (len(prices) - 1), seconds=10)
    inv, entry, close = 50.0, 1.0, prices[-1]
    samples = [
        {"at": (started + timedelta(minutes=2 * j)).isoformat(),
         "price_sda": p,
         **({"quote_age_seconds": 40.0} if with_quote_age else {})}
        for j, p in enumerate(prices)
    ]
    return {
        "status": "CLOSED", "v30_mode": "CLEAN-PUMP-HUNTER-V30.3",
        "label": f"TOKEN{index}/SDA", "address": f"0x{index:040x}",
        "opened_at": started.isoformat(), "closed_at": closed.isoformat(),
        "entry_price": entry, "close_price": close, "investment_sda": inv,
        "closed_profit_sda": net_exit(entry, close, inv),
        "entry_confidence": 90 if index % 2 else 75,
        "pump_mfe_pct": (max(prices) / entry - 1) * 100,
        "pump_exit_observation_gap_seconds": 120,
        "close_reason": "V30.3 TRAILING/STOP",
        "pump_price_samples": samples,
    }


class V31ProfitLearningTests(unittest.TestCase):
    def test_engine_fee_alignment_and_normalized_pnl(self):
        row = trade(1)
        r = observed_trade(row)
        self.assertAlmostEqual(r["pnl_sda"], net_exit(1, .99, 50))
        path, error = quote_path(r)
        self.assertIsNone(error)
        self.assertEqual(len(path), 3)

    def test_profit_take_earlier_only_and_identical_cohort(self):
        sample = trade(1, (1.0, 1.05, 0.99))
        row = observed_trade(sample)
        path, _ = quote_path(row)
        price, at, changed = earlier_exit("take_profit_4", 1, path, .99)
        self.assertTrue(changed)
        self.assertEqual(price, 1.05)
        self.assertIsNotNone(at)
        price, at, changed = earlier_exit("take_profit_4", 1, path[:1], .99)
        self.assertFalse(changed)
        self.assertEqual(price, .99)

    def test_missing_quote_age_fails_closed(self):
        r = observed_trade(trade(1, with_quote_age=False))
        path, error = quote_path(r)
        self.assertIsNone(path)
        self.assertEqual(error, "missing_quote_age")
        result = analyze([r["source"]])
        self.assertEqual(result["data_quality"]["replayable_paths"], 0)
        self.assertEqual(result["exit_research"]["status"], "COLLECTING_VALIDATED_EXIT_PATHS")
        self.assertFalse(result["promotion_eligible"])

    def test_gap_and_bad_fee_and_future_samples_fail_closed(self):
        t = trade(3)
        t["pump_price_samples"][1]["at"] = (
            datetime.fromisoformat(t["opened_at"]) + timedelta(minutes=25)).isoformat()
        path, reason = quote_path(observed_trade(t))
        self.assertIsNone(path)
        self.assertIn(reason, ("sample_gap_over_20m", "sample_outside_actual_trade",
                               "sample_times_not_increasing"))
        t = trade(4)
        t["closed_profit_sda"] += 10
        self.assertEqual(quote_path(observed_trade(t))[1], "paper_fee_alignment_failed")

    def test_cohorts_include_score_exit_and_giveback(self):
        src = [trade(0), trade(1, (1, 1.04, 1.01)),
               trade(2, (1, .98, .96))]
        report = analyze(src)
        self.assertEqual(report["historical_observed"]["overall"]["closed"], 3)
        self.assertEqual(report["historical_observed"]["gave_back_profit"]
                         ["mfe_at_least_2_5_and_closed_loss"], 1)
        self.assertIn("85_plus", report["historical_observed"]["by_entry_score"])
        self.assertEqual(report["data_quality"]["replayable_paths"], 3)
        self.assertFalse(report["production_mutated"])
        self.assertFalse(report["automatically_applied"])

    def test_train_only_candidate_selection_holdout_independence(self):
        source = [trade(i, (1, 1.05, 0.99)) for i in range(60)]
        a = analyze(source)
        self.assertEqual(a["exit_research"]["train_count"], 42)
        self.assertEqual(a["exit_research"]["holdout_count"], 18)
        self.assertEqual(a["exit_research"]["status"], "OBSERVATIONAL_EARLIER_EXIT_ONLY")
        self.assertTrue(a["exit_research"]["selection_uses_train_only"])
        b_source = copy.deepcopy(source)
        for row in b_source[42:]:
            row["pump_price_samples"][1]["price_sda"] = 0.97
        b = analyze(b_source)
        self.assertEqual(a["exit_research"]["selected_candidate"],
                         b["exit_research"]["selected_candidate"])
        self.assertNotEqual(a["exit_research"]["holdout"], b["exit_research"]["holdout"])
        self.assertFalse(a["promotion_eligible"])
        self.assertFalse(b["promotion_eligible"])

    def test_actual_stats_mismatch_is_reported_not_hidden(self):
        source = [trade(0)]
        result = analyze(source, {"closed_trades": 999, "realized_pnl_sda": 999})
        self.assertFalse(result["statistics_reconciled"])
        self.assertTrue(any("do not match" in x for x in result["limitations"]))

    def test_no_lookahead_or_hold_after_original_close(self):
        src = trade(1)
        src["pump_price_samples"][-1]["at"] = (
            datetime.fromisoformat(src["closed_at"]) + timedelta(minutes=1)).isoformat()
        self.assertEqual(quote_path(observed_trade(src))[1],
                         "sample_outside_actual_trade")
        self.assertEqual(len(EXIT_POLICIES), 4)


if __name__ == "__main__":
    unittest.main()
