import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.forward_shadow_agent import (
    POLICIES, TEMPORAL_TP4_MODE, default_state, eligible, exit_profit, load_state, step,
)
from tools.strategy_evidence import evidence_report

T = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)
PARENT = "research_temporal_recovery"


def inputs(at, price=1.0, age=0):
    quote = at-timedelta(seconds=age)
    candidate = dict(address="0xab", at=at.isoformat(), quote_at=quote.isoformat(),
                     data_age_sec=age, phase="CONFIRMATION", score=90, quality=65,
                     impulse=12, trades_1h=20, buy_ratio=2, flow_1h=500,
                     volume_1h=2000, m15=2, m1h=3, price_sda=price)
    market = {"updated_at": at.isoformat(), "tokens": {"0xab": {"analysis": {
        "price_in_sda": price, "last_transaction": quote.isoformat()}}}}
    return market, {"at": at.isoformat(), "candidate_snapshots": [candidate]}


class TemporalTP4Tests(unittest.TestCase):
    def setUp(self):
        self.patch = patch("tools.forward_shadow_agent.load_evolution", return_value={})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def opened(self):
        state = default_state()
        step(state, *inputs(T, age=400))
        self.assertFalse(state["strategies"][TEMPORAL_TP4_MODE]["positions"])
        now = T+timedelta(seconds=60)
        step(state, *inputs(now))
        for name in (PARENT, TEMPORAL_TP4_MODE):
            self.assertIn("0xab", state["strategies"][name]["positions"])
        return state, now

    def test_no_prior_signal_no_entry_and_no_promotion_lane(self):
        state = default_state()
        step(state, *inputs(T))
        self.assertFalse(state["strategies"][TEMPORAL_TP4_MODE]["positions"])
        self.assertTrue(TEMPORAL_TP4_MODE.startswith("research_"))
        self.assertEqual(POLICIES[TEMPORAL_TP4_MODE], POLICIES[PARENT])

    def test_same_entry_and_tp4_on_observed_price_after_costs(self):
        state, now = self.opened()
        step(state, *inputs(now+timedelta(minutes=5), price=1.045))
        arm = state["strategies"][TEMPORAL_TP4_MODE]
        self.assertFalse(arm["positions"])
        trade = arm["trades"][0]
        self.assertEqual(trade["reason"], "TAKE")
        self.assertAlmostEqual(trade["pnl_sda"], exit_profit(1, 1.045), places=5)
        self.assertTrue(trade["valid_for_learning"])
        self.assertTrue(state["strategies"][PARENT]["positions"])
        step(state, *inputs(now+timedelta(minutes=10), price=1.085))
        pairs = evidence_report(state)["paired_tp4_exits"]
        self.assertEqual(pairs["pairs"], 1)
        self.assertLess(pairs["difference_sda"], 0)

    def test_stale_quote_never_produces_same_entry_exit(self):
        state, now = self.opened()
        step(state, *inputs(now+timedelta(minutes=5), price=1.2, age=300))
        self.assertFalse(state["strategies"][TEMPORAL_TP4_MODE]["trades"])

    def test_advancing_but_stale_exit_is_not_a_fill_and_times_out(self):
        state, now = self.opened()
        step(state, *inputs(now+timedelta(minutes=20), price=1.2, age=400))
        arm = state["strategies"][TEMPORAL_TP4_MODE]
        self.assertFalse(arm["trades"])
        self.assertTrue(arm["positions"])
        step(state, *inputs(now+timedelta(hours=8), price=1.2, age=400))
        self.assertEqual(arm["trades"][0]["reason"], "STALE_TIMEOUT")
        self.assertFalse(arm["trades"][0]["valid_for_learning"])

    def test_stop_and_gap_keep_risk_in_evidence(self):
        for delay, valid in ((5, True), (40, False)):
            with self.subTest(delay=delay):
                state, now = self.opened()
                step(state, *inputs(now+timedelta(minutes=delay), price=.97))
                trade = state["strategies"][TEMPORAL_TP4_MODE]["trades"][0]
                self.assertEqual(trade["reason"], "STOP")
                self.assertEqual(trade["valid_for_learning"], valid)
                result = next(r for r in evidence_report(state)["strategies"]
                              if r["strategy"] == TEMPORAL_TP4_MODE)
                if not valid:
                    self.assertEqual(result["conservative_closed_risk_sda"], -50)

    def test_gap_during_hold_cannot_be_erased_by_later_fresh_exit(self):
        state, now = self.opened()
        step(state, *inputs(now+timedelta(minutes=40), price=1.01))
        arm = state["strategies"][TEMPORAL_TP4_MODE]
        self.assertFalse(arm["trades"])
        step(state, *inputs(now+timedelta(minutes=45), price=1.05))
        trade = arm["trades"][0]
        self.assertFalse(trade["valid_for_learning"])
        self.assertEqual(trade["max_quote_gap_minutes"], 40)
        self.assertIn("quote_gap_over_30m", trade["learning_exclusion_reasons"])

    def test_trial_cannot_copy_parent_results_on_upgrade(self):
        import json
        import tempfile
        from pathlib import Path
        state = default_state()
        state["strategies"].pop(TEMPORAL_TP4_MODE)
        state["strategies"][PARENT]["trades"] = [{"pnl_sda": 100}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            path.write_text(json.dumps(state))
            upgraded = load_state(path)
        self.assertEqual(upgraded["strategies"][TEMPORAL_TP4_MODE]["trades"], [])
        self.assertEqual(upgraded["strategies"][PARENT]["trades"], [{"pnl_sda": 100}])

    def test_report_accounts_for_invalid_and_open_stakes_without_mutation(self):
        state = {"strategies": {PARENT: {"trades": [
            {"valid_for_learning": True, "pnl_sda": 10},
            {"valid_for_learning": False, "pnl_sda": 1000}],
            "positions": {"0xab": {}}}}}
        before = copy.deepcopy(state)
        result = evidence_report(state)
        row = result["strategies"][0]
        self.assertEqual(row["observed_quote_net_sda"], 10)
        self.assertEqual(row["conservative_closed_risk_sda"], -40)
        self.assertEqual(row["conservative_including_open_risk_sda"], -90)
        self.assertFalse(result["can_promote"])
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
