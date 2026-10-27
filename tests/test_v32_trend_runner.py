"""V32 Trend Runner: causal partial profits and one net SDA outcome per entry."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

from tools.forward_shadow_agent import POLICIES, default_state, step, exit_profit
from tools.v32_trend_runner import (apply_observed_quote, close_record,
                                    stale_writeoff, net_exit)

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
RUNNER = "research_trend_runner"


def market(t, price, quote=None):
    return {"updated_at": t.isoformat(), "tokens": {
        "0xab": {"analysis": {"price_in_sda": price,
                              "last_transaction": (quote or t).isoformat()}}
    }}


def funnel(t, price=1.0, **features):
    candidate = {
        "address": "0xab", "phase": "CONFIRMATION", "score": 90,
        "quality": 80, "impulse": 16, "trades_1h": 25,
        "buy_ratio": 2.0, "flow_1h": 1000, "volume_1h": 2500,
        "m15": 3.0, "m1h": 4.0, "price_sda": price, "data_age_sec": 0
    }
    candidate.update(features)
    return {"at": t.isoformat(), "candidate_snapshots": [candidate]}


def sample(state, minutes, price, quote=None):
    t = T0 + timedelta(minutes=minutes)
    return step(state, market(t, price, quote), funnel(t, price))


class TrendRunnerForwardTests(unittest.TestCase):
    def test_runner_opens_only_forward_with_same_baseline_gates(self):
        state = default_state()
        self.assertEqual(POLICIES[RUNNER], POLICIES["baseline"])
        result = sample(state, 0, 1.0)
        self.assertEqual(len(state["strategies"][RUNNER]["positions"]), 1)
        self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)
        self.assertEqual(result["closed"], 0)
        self.assertEqual(state["strategies"][RUNNER]["trades"], [])

    def test_partial_take_keeps_remainder_and_runner_trails_on_later_quote(self):
        state = default_state()
        sample(state, 0, 1.0)
        result = sample(state, 5, 1.10)
        runner = state["strategies"][RUNNER]
        self.assertEqual(result["closed"], 10)  # six original arms + four cooldown research arms
        self.assertEqual(len(runner["trades"]), 0)
        self.assertEqual(len(runner["positions"]), 1)
        p = runner["positions"]["0xab"]
        self.assertEqual(len(p["runner_legs"]), 1)
        self.assertEqual(p["runner_legs"][0]["fraction"], .5)
        self.assertAlmostEqual(p["runner_legs"][0]["net_sda"],
                               net_exit(1., 1.10, 25., .011), places=5)
        self.assertEqual(state["strategies"]["baseline"]["trades"][0]["reason"], "TAKE")

        sample(state, 10, 1.20)
        sample(state, 15, 1.16)
        self.assertEqual(len(runner["trades"]), 0)  # 1.16 above 1.20*.965
        sample(state, 20, 1.15)
        self.assertEqual(len(runner["positions"]), 0)
        self.assertEqual(len(runner["trades"]), 1)
        rec = runner["trades"][0]
        self.assertEqual(rec["reason"], "RUNNER_TRAIL")
        self.assertTrue(rec["valid_for_learning"])
        self.assertEqual(rec["runner_leg_count"], 2)
        self.assertEqual([x["fraction"] for x in rec["runner_legs"]], [.5,.5])
        expected = net_exit(1., 1.10, 25., .011) + net_exit(1., 1.15, 25., .011)
        self.assertAlmostEqual(rec["pnl_sda"], expected, places=5)
        baseline = state["strategies"]["baseline"]["trades"][0]["pnl_sda"]
        self.assertGreater(rec["pnl_sda"], baseline)
        self.assertTrue(state["last_diagnostic"]["trend_runner_research"]
                        ["partial_legs_are_not_closed_trades"])

    def test_hard_stop_before_partial_is_single_complete_trade(self):
        state = default_state()
        sample(state, 0, 1.0)
        sample(state, 5, .965)
        runner = state["strategies"][RUNNER]
        self.assertEqual(len(runner["trades"]), 1)
        rec = runner["trades"][0]
        self.assertFalse(rec["runner_first_take"])
        self.assertEqual(rec["runner_leg_count"], 1)
        self.assertEqual(rec["reason"], "STOP")
        self.assertAlmostEqual(rec["pnl_sda"], exit_profit(1.,.965), places=5)

    def test_older_quote_cannot_execute_partial_or_exit(self):
        state = default_state()
        sample(state, 0, 1.0)
        t = T0+timedelta(minutes=10)
        step(state, market(t, 1.50, T0), funnel(t,1.50))
        runner = state["strategies"][RUNNER]
        self.assertEqual(len(runner["positions"]["0xab"]["runner_legs"]), 0)
        self.assertEqual(len(runner["trades"]), 0)
        self.assertEqual(runner["positions"]["0xab"]["last_quote_at"], T0.isoformat())

    def test_delayed_new_quote_cannot_fake_observed_earlier_partial_fill(self):
        state = default_state()
        sample(state, 0, 1.)
        current = T0 + timedelta(minutes=20)
        # Data arrived 15m after the transaction. A new price timestamp
        # exists, but it was not actionable at this scanner snapshot.
        delayed = T0 + timedelta(minutes=5)
        step(state, market(current, 1.2, delayed), funnel(current, 1.2))
        pos = state["strategies"][RUNNER]["positions"]["0xab"]
        self.assertEqual(pos["runner_legs"], [])
        self.assertEqual(pos["last_quote_at"], T0.isoformat())

    def test_long_quote_gap_excludes_final_quote_trade(self):
        state = default_state()
        sample(state, 0, 1.)
        sample(state, 5, 1.1)
        sample(state, 65, 1.2)
        sample(state, 70, 1.1)
        trade = state["strategies"][RUNNER]["trades"][0]
        self.assertFalse(trade["valid_for_learning"])
        self.assertIn("runner_observation_gap_over_30m",
                      trade["learning_exclusion_reasons"])

    def test_stale_partial_counts_only_unquoted_remainder_and_excludes(self):
        state = default_state()
        sample(state, 0, 1.0)
        sample(state, 5, 1.09)
        at = T0+timedelta(hours=17)
        step(state, market(at, 1.09, T0+timedelta(minutes=5)), funnel(at,1.09))
        trade = state["strategies"][RUNNER]["trades"][0]
        self.assertFalse(trade["valid_for_learning"])
        self.assertEqual(trade["reason"], "STALE_TIMEOUT")
        self.assertEqual(trade["runner_leg_count"], 1)
        self.assertAlmostEqual(trade["pnl_sda"],
                               net_exit(1.,1.09,25.,.011)-25.,places=5)
        self.assertFalse(state["strategies"][RUNNER]["positions"])

    def test_first_take_at_observed_quote_not_intrabar_limit(self):
        p = {"opened_at": T0.isoformat(), "entry_price_sda": 1.,
             "peak_price_sda": 1., "runner_legs": []}
        stamp = T0+timedelta(minutes=5)
        action, why = apply_observed_quote(p,stamp,1.18,5,50,.011)
        self.assertEqual((action,why),("partial","PARTIAL_TAKE"))
        self.assertEqual(p["runner_legs"][0]["price_sda"],1.18)
        # Record is not finalized until a *later* observed quote.
        self.assertEqual(len(p["runner_legs"]),1)
        self.assertTrue(p["runner_legs"][0]["net_sda"] > 0)

    def test_stale_quote_never_becomes_positive_valid_trade(self):
        p = {"opened_at": T0.isoformat(), "entry_price_sda":1.,
             "peak_price_sda":1., "runner_legs":[]}
        take_time=T0+timedelta(minutes=5)
        apply_observed_quote(p,take_time,1.1,5)
        writeoff=stale_writeoff(p,T0+timedelta(hours=18))
        self.assertFalse(writeoff["valid_for_learning"])
        self.assertIsNone(writeoff["exit_price_sda"])

    def test_partial_and_final_net_charged_friction_once_per_leg(self):
        p = {"opened_at":T0.isoformat(),"entry_price_sda":1.,
             "peak_price_sda":1.,"runner_legs":[]}
        take_time=T0+timedelta(minutes=5)
        apply_observed_quote(p,take_time,1.1,5)
        end=T0+timedelta(minutes=10)
        apply_observed_quote(p,end,1.05,5)
        result=close_record(p,end,1.05,"RUNNER_TRAIL",5)
        expected=(net_exit(1.,1.1,25.,.011)+net_exit(1.,1.05,25.,.011))
        self.assertAlmostEqual(result["pnl_sda"],expected,places=5)
        self.assertEqual(sum(x["fraction"] for x in result["runner_legs"]),1.)


if __name__=="__main__":
    unittest.main()
