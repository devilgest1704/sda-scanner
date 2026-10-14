"""Future-only research for missed high-volume pumps with few observed swaps."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.v32_breakout import (
    analyze_breakout, research_signal_from_analysis,
    sparse_volume_research_eligible,
)
from tools.forward_shadow_agent import (
    default_state, eligible, step, POLICIES, SPARSE_VOLUME_RESEARCH_MODE,
)

T = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ADDR = "0xearly"


def observed_history():
    rows = []
    for i in range(15):
        # Real two-hour consolidation; at least three trades in previous 45m.
        at = T - timedelta(minutes=145 - 8*i)
        rows.append({
            "timestamp": at.isoformat(),
            "price": 1.0 + (i%4)*.01,
            "volume": 40.0,
            "type": "buy" if i%2 else "sell",
        })
    for minutes, price, side, volume in (
        (9, 1.04, "buy", 650.0),
        (5, 1.055, "buy", 650.0),
        (1, 1.06, "sell", 30.0),
    ):
        rows.append({
            "timestamp": (T - timedelta(minutes=minutes)).isoformat(),
            "price": price, "volume": volume, "type": side,
        })
    return rows


def candidate(**changes):
    row = {
        "address": ADDR, "phase": "WATCH", "score": 75, "quality": 66,
        "snapshot_impulse": 9, "delta_impulse": 0, "impulse": 0,
        "data_age_sec": 120, "m15": 3, "m1h": 5, "m4h": 12,
        "trades_1h": 7, "buy_ratio": 2.1,
        "volume_1h": 1550, "flow_1h": 1200, "flow_15m": 1270,
        "price_sda": 1.06,
        "v32_breakout": analyze_breakout(observed_history(), T),
    }
    row.update(changes)
    return row


class SparseVolumeResearchTests(unittest.TestCase):
    def test_three_swap_burst_is_rejected_by_normal_breakout_but_shadow_observable(self):
        signal = analyze_breakout(observed_history(), T)
        self.assertTrue(signal["available"], signal)
        self.assertFalse(signal["eligible"], signal)
        self.assertEqual(set(signal["rejection_reasons"]), {
            "insufficient_recent_trades", "insufficient_buy_transactions"})
        self.assertTrue(sparse_volume_research_eligible(
            candidate(), POLICIES[SPARSE_VOLUME_RESEARCH_MODE]))
        self.assertTrue(eligible(candidate(), POLICIES[SPARSE_VOLUME_RESEARCH_MODE],
                                 macd_mode=SPARSE_VOLUME_RESEARCH_MODE))

    def test_no_lookahead_and_same_scan_quote_evidence(self):
        history = observed_history()
        future = {
            "timestamp": (T+timedelta(minutes=3)).isoformat(),
            "price": 10, "volume": 99999, "type": "buy",
        }
        self.assertEqual(analyze_breakout(history, T),
                         analyze_breakout([future]+list(reversed(history)), T))
        signal = analyze_breakout(history, T)
        analysis = {
            "price_in_sda": 1.06,
            "technical": {"breakout_volume_trend": signal},
        }
        self.assertIsNotNone(research_signal_from_analysis(
            analysis, T + timedelta(minutes=1), T))
        self.assertIsNone(research_signal_from_analysis(
            analysis, T + timedelta(minutes=7), T))

    def test_stale_late_low_pressure_and_unverified_rejections_fail_closed(self):
        cfg = POLICIES[SPARSE_VOLUME_RESEARCH_MODE]
        self.assertFalse(sparse_volume_research_eligible(candidate(m4h=128), cfg))
        self.assertFalse(sparse_volume_research_eligible(candidate(flow_15m=80), cfg))
        self.assertFalse(sparse_volume_research_eligible(candidate(data_age_sec=400), cfg))
        self.assertFalse(sparse_volume_research_eligible(candidate(score=30), cfg))
        raw = analyze_breakout(observed_history(), T)
        raw = {**raw, "rejection_reasons":raw["rejection_reasons"]+["stale_quote"]}
        self.assertFalse(sparse_volume_research_eligible(
            candidate(v32_breakout=raw), cfg))
        # A volume-only signal must never bypass confirmation failures.
        self.assertFalse(sparse_volume_research_eligible(
            candidate(v32_breakout={**raw,"rejection_reasons":[]}), cfg))

    def test_forward_shadow_opens_only_virtual_research_position(self):
        rows=observed_history()
        signal=analyze_breakout(rows,T)
        market={
            "updated_at":T.isoformat(),
            "tokens":{ADDR:{"analysis":{
                "price_in_sda":1.06,
                "last_transaction":(T-timedelta(minutes=1)).isoformat(),
                "technical":{"breakout_volume_trend":signal},
            }}},
        }
        funnel={"at":(T+timedelta(minutes=1)).isoformat(),
                "candidate_snapshots":[candidate()]}
        state=default_state()
        with patch("tools.forward_shadow_agent.load_evolution",
                   return_value={"experiments":[]}):
            result=step(state,market,funnel)
        self.assertEqual(result["status"], "processed", result)
        self.assertIn(ADDR,state["strategies"][SPARSE_VOLUME_RESEARCH_MODE]["positions"])
        self.assertEqual(state["strategies"]["baseline"]["positions"], {})
        self.assertEqual(state["strategies"]["research_breakout_volume_trend"]["positions"], {})
        position=state["strategies"][SPARSE_VOLUME_RESEARCH_MODE]["positions"][ADDR]
        self.assertEqual(position["v32_breakout_at_entry"]["eligible"],False)


if __name__ == "__main__":
    unittest.main()
