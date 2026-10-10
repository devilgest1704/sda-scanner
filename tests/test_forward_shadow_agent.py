import unittest
from datetime import datetime, timezone, timedelta
from tools.forward_shadow_agent import (
    POLICIES, default_state, eligible, step, exit_profit, MAX_HISTORY,
)

T0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def market(t, price=1.0, txn=None):
    return {"updated_at": t.isoformat(), "tokens": {
        "0xab": {"analysis": {"price_in_sda": price,
                              "last_transaction": (txn or t).isoformat()}}
    }}


def funnel(t, **overrides):
    item = {
        "address": "0xab", "phase": "CONFIRMATION", "score": 90,
        "quality": 90, "impulse": 18, "trades_1h": 20, "buy_ratio": 2,
        "flow_1h": 500, "volume_1h": 2000, "m15": 2.0,
        "m1h": 3, "price_sda": 1.0, "data_age_sec": 2,
    }
    item.update(overrides)
    return {"at": t.isoformat(), "candidate_snapshots": [item]}


class ForwardPaperTests(unittest.TestCase):
    def test_shadow_entries_do_not_execute_or_use_current_quote_as_exit(self):
        state = default_state()
        result = step(state, market(T0), funnel(T0))
        self.assertEqual(result["opened"], len(POLICIES))
        self.assertEqual(result["closed"], 0)
        for st in state["strategies"].values():
            self.assertEqual(len(st["positions"]), 1)
            self.assertEqual(len(st["trades"]), 0)
        self.assertEqual(step(state, market(T0), funnel(T0))["status"], "already_processed")

    def test_future_quote_exit_and_cost_deducted(self):
        state = default_state()
        step(state, market(T0), funnel(T0))
        t1 = T0 + timedelta(minutes=5)
        result = step(state, market(t1, 0.965), funnel(t1, price_sda=0.965))
        self.assertEqual(result["closed"], len(POLICIES))
        for st in state["strategies"].values():
            self.assertEqual(len(st["trades"]), 1)
            trade = st["trades"][0]
            self.assertEqual(trade["reason"], "STOP")
            self.assertTrue(trade["valid_for_learning"])
            self.assertLess(trade["pnl_sda"], 0)
            self.assertLess(trade["pnl_sda"], -1.75)

    def test_stale_or_missing_transaction_never_updates_trade(self):
        state = default_state()
        step(state, market(T0), funnel(T0))
        t1 = T0 + timedelta(minutes=10)
        step(state, market(t1, 0.70, txn=T0), funnel(t1, price_sda=0.70))
        for st in state["strategies"].values():
            self.assertEqual(len(st["trades"]), 0)

    def test_stale_entrant_rejected(self):
        state = default_state()
        stale = funnel(T0, data_age_sec=600)
        result = step(state, market(T0), stale)
        self.assertEqual(result["opened"], 0)

    def test_no_watch_entries_and_no_threshold_bypass(self):
        base = funnel(T0)["candidate_snapshots"][0]
        self.assertFalse(eligible({**base, "phase": "WATCH"}, POLICIES["baseline"]))
        self.assertFalse(eligible({**base, "trades_1h": 8}, POLICIES["baseline"]))
        self.assertFalse(eligible({**base, "quality": 50}, POLICIES["baseline"]))

    def test_future_market_timestamp_is_rejected(self):
        state = default_state()
        with self.assertRaises(ValueError):
            step(state, market(T0 + timedelta(hours=2)), funnel(T0))

    def test_gap_marks_result_unfit_for_learning(self):
        state = default_state()
        step(state, market(T0), funnel(T0))
        late = T0 + timedelta(hours=2)
        step(state, market(late, 0.90), funnel(late, price_sda=0.90))
        for st in state["strategies"].values():
            self.assertEqual(len(st["trades"]), 1)
            self.assertFalse(st["trades"][0]["valid_for_learning"])


if __name__ == "__main__":
    unittest.main()
