"""Causal forward-only tests for the V33 swap-pressure research arm."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.v33_flow_continuation import observed_flow_signal
from tools.forward_shadow_agent import (
    FLOW_V33_RESEARCH_MODE, POLICIES, default_state, eligible, step,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def candidate(**changes):
    row = {
        "address": "0xab", "phase": "CONFIRMATION", "score": 79,
        "quality": 70, "impulse": 11, "trades_1h": 22, "buy_ratio": 2.5,
        "flow_1h": 450, "volume_1h": 1600, "m15": 2.7, "m1h": 5.5,
        "m4h": 12.0, "price_sda": 1.0, "data_age_sec": 0,
    }
    row.update(changes)
    return row


def observed_token(time=T0, price=1.0):
    return {
        "price_in_sda": price, "last_transaction": time.isoformat(),
        "flow": {
            "15m": {"transactions": 8, "buy_count": 6, "sell_count": 2,
                    "total_volume": 580, "buy_volume": 490,
                    "sell_volume": 90, "net_flow": 400},
            "1h": {"transactions": 23, "buy_count": 15, "sell_count": 8,
                   "total_volume": 1700, "buy_volume": 1100,
                   "sell_volume": 600, "net_flow": 500},
        },
    }


def market(time=T0, token=None):
    return {"updated_at": time.isoformat(),
            "tokens": {"0xab": {"analysis": token or observed_token(time)}}}


def funnel(time=T0, row=None):
    return {"at": time.isoformat(), "candidate_snapshots": [row or candidate()]}


class FlowConfirmationTests(unittest.TestCase):
    def test_only_prior_swaps_with_genuine_buy_pressure(self):
        signal = observed_flow_signal(candidate(), observed_token(), T0, T0)
        self.assertTrue(signal["eligible"])
        self.assertTrue(signal["available"])
        self.assertEqual(signal["metrics"]["recent_share_of_hour_volume"],
                         round(580 / 1700, 4))
        self.assertFalse(signal["pool_liquidity_verified"])
        self.assertFalse(signal["execution_parity"])
        self.assertFalse(signal["can_promote_champion"])

    def test_missing_buckets_or_features_fail_closed(self):
        bad = observed_token()
        del bad["flow"]["15m"]
        self.assertFalse(observed_flow_signal(candidate(), bad, T0, T0)["eligible"])
        self.assertFalse(observed_flow_signal(
            candidate(m4h=None), observed_token(), T0, T0)["eligible"])

    def test_stale_future_and_misaligned_observations_fail(self):
        stale = observed_token(T0 - timedelta(minutes=4))
        future = observed_token(T0 + timedelta(seconds=1))
        for token in (stale, future):
            self.assertFalse(observed_flow_signal(candidate(), token, T0, T0)["eligible"])
        self.assertFalse(observed_flow_signal(
            candidate(), observed_token(), T0, T0+timedelta(seconds=1))["eligible"])

    def test_no_chasing_four_hour_pump_or_weak_15m_flow(self):
        self.assertIn("late_four_hour_chase", observed_flow_signal(
            candidate(m4h=32), observed_token(), T0, T0)["rejection_reasons"])
        token = observed_token()
        token["flow"]["15m"]["buy_count"] = 1
        token["flow"]["15m"]["buy_volume"] = 140
        token["flow"]["15m"]["sell_volume"] = 440
        token["flow"]["15m"]["net_flow"] = -300
        got = observed_flow_signal(candidate(), token, T0, T0)
        self.assertFalse(got["eligible"])
        self.assertIn("weak_recent_buy_pressure", got["rejection_reasons"])

    def test_research_arm_only_and_forward_quote_exit(self):
        with patch("tools.forward_shadow_agent.load_evolution", return_value={}):
            state = default_state()
            result = step(state, market(), funnel())
            self.assertEqual(result["status"], "processed")
            self.assertEqual(len(state["strategies"][FLOW_V33_RESEARCH_MODE]["positions"]), 1)
            self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)
            self.assertTrue(state["last_diagnostic"]["v33_flow_research"]
                            ["eligible_after_common_shadow_gates"] >= 1)
            self.assertTrue(state["last_diagnostic"]["v33_flow_research"]
                            ["paper_champion_unchanged"])
            entry = state["strategies"][FLOW_V33_RESEARCH_MODE]["positions"]["0xab"]
            self.assertIn("v33_flow_at_entry", entry)
            # An identical quote cannot close its own entry.
            self.assertEqual(len(state["strategies"][FLOW_V33_RESEARCH_MODE]["trades"]), 0)

            t1 = T0 + timedelta(minutes=5)
            token = observed_token(t1, price=0.95)
            next_row = candidate(price_sda=0.95)
            result2 = step(state, market(t1, token), funnel(t1, next_row))
            self.assertGreater(result2["closed"], 0)
            rows = state["strategies"][FLOW_V33_RESEARCH_MODE]["trades"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "STOP")
            self.assertTrue(rows[0]["valid_for_learning"])
            self.assertLess(rows[0]["pnl_sda"], 0)
            self.assertEqual(rows[0]["v33_flow_at_entry"]["mode"], "RESEARCH_ONLY")
            self.assertEqual(state["strategies"][FLOW_V33_RESEARCH_MODE]["positions"], {})

    def test_no_signal_without_market_flow_while_baseline_unchanged(self):
        row = candidate()
        self.assertTrue(eligible(row, POLICIES["baseline"]))
        self.assertFalse(eligible(row, POLICIES[FLOW_V33_RESEARCH_MODE],
                                  macd_mode=FLOW_V33_RESEARCH_MODE))
        with patch("tools.forward_shadow_agent.load_evolution", return_value={}):
            no_flow = {"price_in_sda": 1.0, "last_transaction": T0.isoformat()}
            state = default_state()
            step(state, market(T0, no_flow), funnel())
            self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)
            self.assertEqual(len(state["strategies"][FLOW_V33_RESEARCH_MODE]["positions"]), 0)


if __name__ == "__main__":
    unittest.main()
