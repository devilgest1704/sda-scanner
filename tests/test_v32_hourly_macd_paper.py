import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from technical_analysis import analyze_token
from tools.v32_hourly_macd import (
    completed_hourly_macd, paper_macd_veto
)
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
        with (patch.object(v30, "score", return_value=(c, 90, 90, 12, "CONFIRMATION")),
              patch.object(v30, "entry_gate", return_value={
                  "buy": True, "entry_impulse": 12,
                  "liquidity_ok": True, "price_flow_ok": True, "ratio": 1.0}),
              patch.object(v30, "cooldown_active", return_value=False),
              patch.object(v30, "load_state", return_value={
                  "history": {"0xab": {"prev": prev}}})):
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
