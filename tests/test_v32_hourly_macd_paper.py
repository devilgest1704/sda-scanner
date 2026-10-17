import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from technical_analysis import analyze_token
from tools.v32_hourly_macd import (
    completed_hourly_macd, paper_macd_veto, MACD_METHOD
)
from tools.forward_shadow_agent import observed_macd
import v30_pump_hunter as v30

# Always use a completed, already observed reference hour for integration
# with v30.decision(), which intentionally evaluates freshness at wall-clock NOW.
AT = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
      - timedelta(minutes=38))


def history(prices=None):
    # Forty consecutive COMPLETED hourly buckets ending before AT.
    current_start = AT.replace(minute=0, second=0, microsecond=0)
    values = prices or [1.0 + k*.001 for k in range(40)]
    return [{
        "timestamp": (current_start - timedelta(hours=40-i, minutes=-12)).isoformat(),
        "price": p, "volume": 100.0, "hash": f"0x{i:02x}",
        "type": "buy"
    } for i,p in enumerate(values)]


def paper_frame(histogram=-.1, delta=-.02):
    return {
        "available": True,
        "completed_bars": 40, "traded_bars": 40,
        "method": MACD_METHOD,
        "lookback_traded_hours": 35, "max_lookback_gap_hours": 1,
        "recent_traded_hours": 12, "max_recent_gap_hours": 1,
        "last_completed_at": AT.replace(minute=0, second=0).isoformat(),
        "quote_age_seconds": 4300,
        "macd": {"histogram_pct": histogram,
                 "histogram_delta_pct": delta,
                 "histogram_rising": delta > 0,
                 "histogram_fading_2": False},
    }


class HourlyMacdEvidence(unittest.TestCase):
    def test_completed_1h_available_without_affecting_short_macd(self):
        a = analyze_token({"transactions": history()}, AT)
        self.assertTrue(a["1h_completed"]["available"], a["1h_completed"])
        self.assertEqual(a["1h_completed"]["recent_traded_hours"], 12)
        self.assertEqual(a["1h_completed"]["completed_bars"], 40)
        self.assertIn("5m", a)
        self.assertIn("15m", a)

    def test_incomplete_and_future_candles_cannot_change_signal(self):
        initial = history()
        same_hour = {"timestamp": (AT - timedelta(minutes=1)).isoformat(),
                     "price": 800, "volume": 10000, "hash": "0xc"}
        future = {"timestamp": (AT + timedelta(hours=5)).isoformat(),
                  "price": 10000, "volume": 10000, "hash": "0xd"}
        baseline = completed_hourly_macd(initial, AT)
        self.assertEqual(baseline, completed_hourly_macd(initial+[same_hour,future], AT))

    def test_reverse_input_order_preserves_hourly_closes(self):
        src = history()
        self.assertEqual(completed_hourly_macd(src, AT),
                         completed_hourly_macd(list(reversed(src)), AT))

    def test_missing_hour_preserves_clock_spacing_not_synthetic_trades(self):
        complete = completed_hourly_macd(history(), AT)
        missing = completed_hourly_macd([
            x for i,x in enumerate(history()) if i not in {5, 9, 11}
        ], AT)
        self.assertTrue(complete["available"])
        self.assertTrue(missing["available"], missing)
        self.assertEqual(missing["completed_bars"], 40)
        self.assertEqual(missing["traded_bars"], 37)
        self.assertEqual(missing["carried_bars"], 3)
        self.assertNotEqual(missing["macd"]["histogram_pct"],
                            complete["macd"]["histogram_pct"])

    def test_old_157h_gap_is_not_mistaken_for_recent_gap(self):
        # Older trades from an archive should not poison a well-observed
        # recent 35-hour window, or be compressed as consecutive 1h bars.
        origin = AT.replace(minute=0, second=0, microsecond=0)
        ancient = [{
            "timestamp": (origin - timedelta(hours=180 + i)).isoformat(),
            "hash": f"0xanc{i}", "price": 1.0+i*.001
        } for i in range(35)]
        hours_ago = [33,29,26,22,18,15,12,9,6,4,2,1]
        recent = [{
            "timestamp": (origin-timedelta(hours=hours)+timedelta(minutes=12)).isoformat(),
            "hash": f"0xrecent{hours}", "price": 1.15+(35-hours)*.001,
        } for hours in hours_ago]
        r = completed_hourly_macd(ancient+recent, AT)
        self.assertTrue(r["available"], r)
        self.assertGreaterEqual(r["completed_bars"], 35)
        self.assertEqual(r["lookback_traded_hours"], 12)
        self.assertLessEqual(r["max_recent_gap_hours"], 5)
        self.assertLessEqual(r["max_lookback_gap_hours"], 12)
        self.assertGreater(r["carried_bars"], 0)
        # Research gate must accept the same fixed-clock evidence.
        observed = observed_macd({"technical":{"1h_completed":r}}, AT, "1h_completed")
        self.assertIsNotNone(observed)

    def test_sparse_last35_hours_is_not_faked_by_old_history(self):
        origin=AT.replace(minute=0, second=0, microsecond=0)
        ancient=[{
            "timestamp":(origin-timedelta(hours=300+i)).isoformat(),
            "price":1.0+i*.001, "hash":f"old{i}"
        } for i in range(35)]
        recent=[{
            "timestamp":(origin-timedelta(hours=h)+timedelta(minutes=8)).isoformat(),
            "price":1.2+h*.0001, "hash":f"recent{h}"
        } for h in (10,7,4,1)]
        r=completed_hourly_macd(ancient+recent, AT)
        self.assertFalse(r["available"])
        self.assertEqual(r["reason"],"sparse_lookback_trade_hours")
        self.assertIsNone(r["macd"])

    def test_true_six_hour_gap_within_recent_window_fails_closed(self):
        src=[x for i,x in enumerate(history()) if i not in set(range(32,38))]
        r=completed_hourly_macd(src, AT)
        self.assertFalse(r["available"], r)
        self.assertEqual(r["reason"],"sparse_recent_trade_hours")
        self.assertGreater(r["max_recent_gap_hours"], 5)

    def test_shadow_hourly_validator_rejects_legacy_compressed_candles(self):
        raw = paper_frame()
        valid = observed_macd({"technical":{"1h_completed":raw}}, AT, "1h_completed")
        self.assertIsNotNone(valid)
        legacy = {**raw,"method":"completed_observed_trade_hour_macd_sparse_guard"}
        self.assertIsNone(observed_macd({"technical":{"1h_completed":legacy}},
                                        AT, "1h_completed"))
        sparse = {**raw,"lookback_traded_hours":1}
        self.assertIsNone(observed_macd({"technical":{"1h_completed":sparse}},
                                        AT, "1h_completed"))

    def test_stale_or_sparse_history_remains_unavailable(self):
        stale = history()[:-4]
        self.assertFalse(completed_hourly_macd(stale, AT)["available"])
        old = [
            {**p, "timestamp": (
                datetime.fromisoformat(p["timestamp"])-timedelta(days=6)).isoformat()}
            for p in history()
        ]
        self.assertEqual(completed_hourly_macd(old, AT)["reason"], "stale_completed_hour")
        broken = history()[:30] + history()[35:]
        self.assertEqual(completed_hourly_macd(broken, AT)["reason"],
                         "sparse_recent_trade_hours")

    def test_conservative_veto_distinguishes_negative_recovery(self):
        declining = paper_macd_veto({"technical":{"1h_completed":paper_frame()}}, AT)
        self.assertTrue(declining["applied"])
        self.assertTrue(declining["blocked"])
        self.assertEqual(declining["status"], "BEARISH_VETO")
        recovery = paper_macd_veto({"technical":{"1h_completed":paper_frame(-.1,.05)}}, AT)
        self.assertTrue(recovery["applied"])
        self.assertFalse(recovery["blocked"])
        self.assertEqual(recovery["status"], "ALLOWED")

    def test_unavailable_stale_future_or_nans_fail_to_unknown_not_bullish(self):
        for case in (
            None,
            {"available": False, "reason": "no_history"},
            {**paper_frame(), "quote_age_seconds": 9000},
            {**paper_frame(), "last_completed_at": (AT+timedelta(hours=1)).isoformat()},
            {**paper_frame(), "macd":{"histogram_pct": float("nan"),"histogram_delta_pct": -.1}},
        ):
            with self.subTest(case=case):
                x=paper_macd_veto({"technical":{"1h_completed":case}}, AT)
                self.assertFalse(x["applied"])
                self.assertFalse(x["blocked"])
                self.assertEqual(x["status"], "UNAVAILABLE")


class MacdRealPaperDecisionIntegration(unittest.TestCase):
    def evaluate(self, frame):
        c={"price": 1.0, "m1": 3.0, "m15": 2.0, "m4": 5.0,
           "flow": 500, "flow15": 100, "vol": 2000,
           "trades": 25, "buy_ratio": 3.0, "trade_ratio": 2.0,
           "data_age_sec": 0, "last_transaction": AT.isoformat()}
        # Isolate the actual V30 gate from dynamic lifecycle/network and
        # independently verify that MACD alone changes BUY eligibility.
        prev={"m1": 0, "m15": 0, "flow": 0, "vol": 100, "price": .99, "trades": 12}
        with (patch.object(v30, "PAPER_BUY_MODE", "baseline"),
              patch.object(v30, "score", return_value=(c, 90, 90, 12, "CONFIRMATION")),
              patch.object(v30, "entry_gate", return_value={
                  "buy": True, "entry_impulse": 12,
                  "liquidity_ok": True, "price_flow_ok": True, "ratio": 1.0}),
              patch.object(v30, "cooldown_active", return_value=False),
              patch.object(v30, "load_state", return_value={
                  "history": {"0xab": {"prev": prev}}}),
              patch.object(v30, "paper_entry_risk_gate",
                           return_value={"blocked":False,"status":"ACTIVE"})):
            return v30.decision("0xab", {"technical":{"1h_completed":frame}},
                                persist=False)

    def test_bearish_hourly_macd_really_vetoes_v30_paper_buy(self):
        # Actual runtime decision(), not just a research signal or Telegram label.
        strong=self.evaluate(paper_frame(histogram=.2, delta=.03))
        bearish=self.evaluate(paper_frame(histogram=-.2, delta=-.03))
        self.assertTrue(strong["eligible_for_buy"])
        self.assertFalse(strong["paper_buy_blocked"])
        self.assertFalse(bearish["eligible_for_buy"])
        self.assertTrue(bearish["paper_buy_blocked"])
        self.assertIn("macd_bearish", bearish["paper_buy_gate_failures"])
        self.assertIn("MACD bearish", bearish["paper_buy_block_reason"])

    def test_missing_indicator_is_visible_and_never_manufactures_bullish(self):
        result=self.evaluate(None)
        self.assertTrue(result["eligible_for_buy"])
        self.assertEqual(result["paper_macd_filter"]["status"], "UNAVAILABLE")

    def test_entry_veto_does_not_change_stop_loss_parameters(self):
        self.assertEqual(v30.SL_PCT, .025)
        self.assertEqual(v30.MAX_BUYS_PER_RUN, 1)


if __name__ == "__main__":
    unittest.main()
