import copy
import unittest
from datetime import datetime, timedelta, timezone

from technical_analysis import intraday_macd_analysis, analyze_token
from tools.forward_shadow_agent import (
    POLICIES, default_state, step, macd_research_decision, observed_macd
)

AS_OF = datetime(2026, 10, 12, 12, 1, tzinfo=timezone.utc)


def transactions(seconds=300, gaps=(), after_extra=False):
    floor = int(AS_OF.timestamp()) // seconds * seconds
    first = floor - 35 * seconds
    out = []
    for i in range(35):
        if i in gaps:
            continue
        at = datetime.fromtimestamp(first+i*seconds+30, timezone.utc)
        out.append({"timestamp": at.isoformat(),
                    "price": 1.0 + i*.003 + max(0, i-28)**2*.0004,
                    "volume": 10.0})
    if after_extra:
        # Unfinished and even future candle must not affect MACD.
        out += [
            {"timestamp": (AS_OF-timedelta(seconds=12)).isoformat(),
             "price": 9999.0, "volume": 1000.0},
            {"timestamp": (AS_OF+timedelta(minutes=3)).isoformat(),
             "price": 99999.0, "volume": 1000.0},
        ]
    return out


def technical_frame(ts, frame, positive=True, rising=True, fading=False):
    length = 300 if frame == "5m" else 900
    completed = ts - timedelta(seconds=length)
    return {
        "available": True, "completed_bars": 35, "traded_bars": 30,
        "last_completed_at": completed.isoformat(), "quote_age_seconds": 35,
        "macd": {"histogram_pct": .1 if positive else -.1,
                 "histogram_delta_pct": .02 if rising else -.02,
                 "histogram_rising": rising,
                 "histogram_fading_2": fading}
    }


def snapshot(t, slow, fast=None):
    technical = {"15m": slow}
    if fast is not None:
        technical["5m"] = fast
    return {
        "updated_at": t.isoformat(),
        "tokens": {"0xab": {"analysis": {
            "price_in_sda": 1.0, "last_transaction": t.isoformat(),
            "technical": technical,
        }}}
    }


def funnel(t):
    return {"at": t.isoformat(), "candidate_snapshots": [{
        "address": "0xab", "phase": "CONFIRMATION",
        "score": 90, "quality": 90, "impulse": 18,
        "trades_1h": 20, "buy_ratio": 2.0, "flow_1h": 500,
        "volume_1h": 2000, "m15": 2.0, "m1h": 3.0,
        "price_sda": 1.0, "data_age_sec": 2,
    }]}


class CompletedCandleMacdTests(unittest.TestCase):
    def test_complete_5m_and_15m_bars_are_available(self):
        for seconds in (300, 900):
            with self.subTest(seconds=seconds):
                result = intraday_macd_analysis(transactions(seconds), seconds, AS_OF)
                self.assertTrue(result["available"], result)
                self.assertEqual(result["completed_bars"], 35)
                self.assertEqual(result["traded_bars"], 35)
                self.assertEqual(result["carried_bars"], 0)
                self.assertIsInstance(result["macd"]["histogram_pct"], float)
                self.assertIsInstance(result["macd"]["histogram_fading_2"], bool)
                expected = datetime.fromtimestamp(
                    int(AS_OF.timestamp())//seconds*seconds, timezone.utc)
                self.assertEqual(result["last_completed_at"], expected.isoformat())

    def test_ignores_unfinished_and_future_transactions(self):
        for seconds in (300, 900):
            before = intraday_macd_analysis(transactions(seconds), seconds, AS_OF)
            after = intraday_macd_analysis(transactions(seconds, after_extra=True),
                                           seconds, AS_OF)
            self.assertEqual(before, after)

    def test_unsorted_input_is_identical(self):
        a = transactions()
        self.assertEqual(intraday_macd_analysis(a, 300, AS_OF),
                         intraday_macd_analysis(list(reversed(a)), 300, AS_OF))

    def test_small_missing_candle_is_explicitly_carried(self):
        result = intraday_macd_analysis(transactions(gaps={10}), 300, AS_OF)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["carried_bars"], 1)
        self.assertEqual(result["max_empty_streak"], 1)

    def test_sparse_history_is_unavailable_not_bullish(self):
        sparse = intraday_macd_analysis(transactions(gaps={8,9,10,11}), 300, AS_OF)
        self.assertFalse(sparse["available"])
        self.assertIsNone(sparse["macd"])
        self.assertEqual(sparse["reason"], "too_many_consecutive_missing_bars")
        tiny = intraday_macd_analysis(transactions(gaps=set(range(4,22))),
                                       300, AS_OF)
        self.assertFalse(tiny["available"])
        self.assertEqual(tiny["reason"], "insufficient_traded_bars")

    def test_stale_last_completed_trade_is_unavailable(self):
        result = intraday_macd_analysis(transactions(gaps={33,34}), 300, AS_OF)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "last_completed_price_too_old")

    def test_technical_token_exposes_both_short_timeframes(self):
        token = analyze_token({"transactions": transactions(300)}, AS_OF)
        self.assertIn("5m", token)
        self.assertIn("15m", token)
        self.assertTrue(token["5m"]["available"])


class MacdForwardShadowTests(unittest.TestCase):
    def test_anti_pump_blocks_fading_without_affecting_baseline(self):
        t = AS_OF
        slow = technical_frame(t, "15m", positive=True, rising=False, fading=True)
        fast = technical_frame(t, "5m", positive=True, rising=False)
        state = default_state()
        step(state, snapshot(t, slow, fast), funnel(t))
        self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)
        self.assertEqual(len(state["strategies"]["research_macd_antipump"]["positions"]), 0)
        self.assertEqual(len(state["strategies"]["research_macd_confirmation"]["positions"]), 0)
        self.assertEqual(state["last_diagnostic"]["macd_research_coverage"]["15m"], 1)

    def test_anti_pump_and_momentum_can_open_in_clean_rising_macd(self):
        t = AS_OF
        slow = technical_frame(t, "15m")
        fast = technical_frame(t, "5m")
        state = default_state()
        step(state, snapshot(t, slow, fast), funnel(t))
        for name in ("baseline", "research_macd_antipump", "research_macd_confirmation"):
            self.assertEqual(len(state["strategies"][name]["positions"]), 1, name)
        entry = state["strategies"]["research_macd_antipump"]["positions"]["0xab"]
        self.assertAlmostEqual(entry["v32_macd_at_entry"]["15m"]["histogram_pct"], .1)

    def test_unavailable_or_future_macd_never_opens_macd_arms(self):
        t = AS_OF
        for slow in ({"available":False},
                     technical_frame(t+timedelta(hours=1),"15m"),
                     {**technical_frame(t,"15m"),"traded_bars":10}):
            state = default_state()
            step(state, snapshot(t, slow, technical_frame(t,"5m")), funnel(t))
            self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)
            for name in ("research_macd_antipump", "research_macd_confirmation"):
                self.assertEqual(len(state["strategies"][name]["positions"]), 0)

    def test_existing_champion_research_has_no_macd_dependency(self):
        t = AS_OF
        state = default_state()
        old_market = snapshot(t, {"available":False})
        result = step(state, old_market, funnel(t))
        self.assertGreater(result["opened"], 0)
        self.assertEqual(state["last_diagnostic"]["macd_research_coverage"]["15m"], 0)
        self.assertEqual(len(state["strategies"]["baseline"]["positions"]), 1)

    def test_diagnostics_explain_missing_macd_without_new_trades(self):
        t = AS_OF
        slow = {"available":False,"reason":"insufficient_traded_bars",
                "completed_bars":35,"traded_bars":2}
        fast = {"available":False,"reason":"too_many_consecutive_missing_bars",
                "completed_bars":35,"traded_bars":17}
        state = default_state()
        step(state, snapshot(t, slow, fast), funnel(t))
        d = state["last_diagnostic"]["macd_research_coverage"]
        self.assertEqual(d["market_tokens_analyzed"],1)
        self.assertEqual(d["sampled_candidates"],1)
        self.assertEqual(d["market_unavailability_by_reason"]["15m"]
                         ["insufficient_traded_bars"],1)
        self.assertEqual(d["candidate_unavailability_by_reason"]["5m"]
                         ["too_many_consecutive_missing_bars"],1)
        self.assertFalse(d["thresholds_modified"])
        self.assertTrue(d["research_only"])
        self.assertEqual(len(state["strategies"]["baseline"]["positions"]),1)
        self.assertEqual(len(state["strategies"]["research_macd_antipump"]["positions"]),0)

    def test_diagnostics_distinguish_valid_calculation_from_join_rejection(self):
        t = AS_OF
        future = technical_frame(t+timedelta(minutes=45),"15m")
        state = default_state()
        step(state, snapshot(t, future, technical_frame(t,"5m")), funnel(t))
        d=state["last_diagnostic"]["macd_research_coverage"]
        self.assertEqual(d["market_unavailability_by_reason"]["15m"]["calculated"],1)
        self.assertEqual(d["candidate_unavailability_by_reason"]["15m"]
                         ["failed_same_scan_or_age_guard"],1)
        self.assertEqual(d["5m"],1)
        self.assertEqual(d["15m"],0)

    def test_research_arm_stays_nonpromotable(self):
        for arm in ("research_macd_antipump", "research_macd_confirmation"):
            self.assertIn(arm, POLICIES)
            self.assertTrue(arm.startswith("research_"))


if __name__ == "__main__":
    unittest.main()
