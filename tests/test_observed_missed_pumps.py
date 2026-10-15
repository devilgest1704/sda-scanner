"""Track genuinely observed rejected-BUY pumps separately from executable P/L."""
import unittest
from datetime import datetime, timedelta, timezone

from tools.missed_pump_learning import learn, observed_peak_diagnostics
from optimizer_dashboard import overview_report

T = datetime(2026, 10, 15, 6, tzinfo=timezone.utc)


def timestamp(hours=0, minutes=0):
    return (T + timedelta(hours=hours, minutes=minutes)).isoformat()


def row(address, h=0, peak_return=20, peak_delay=90,
        source="genuine_swap_observed_peak_not_executable_fill",
        features=True, eligible=False):
    decision = T + timedelta(hours=h)
    return {
        "at": decision.isoformat(),
        "quote_at": (decision - timedelta(minutes=1)).isoformat(),
        "address": address,
        "entry_price_sda": 1.0,
        "eligible": eligible,
        "gates": ["score", "lifecycle"],
        "features": ({
            "symbol": "EXAMPLE", "score": 65, "quality": 63,
            "snapshot_impulse": 9, "m1h": 5, "m15": 2,
            "trades_1h": 11, "volume_1h": 1000,
            "flow_1h": 300, "flow_15m": 120,
        } if features else None),
        "observed_peak_240m": {
            "observed_at": (decision + timedelta(minutes=peak_delay)).isoformat(),
            "price_return_pct": peak_return, "source": source,
        },
        "outcomes": {},
    }


class MissedPumpPeakReportingTest(unittest.TestCase):
    def test_genuine_peaks_without_60m_fills_are_visible_only_as_diagnostics(self):
        rows = [
            row("0xaaa", peak_return=35),
            row("0xaaa", h=1, peak_return=100),  # correlated; exclude
            row("0xbbb", h=2, peak_return=12),
            row("0xccc", h=4, peak_return=200,
                source="legacy_unverified_snapshot"),  # not confirmed swap
            row("0xddd", h=5, peak_return=90, peak_delay=241),
            row("0xeee", h=6, peak_return=60, features=False),
            row("0xfff", h=7, peak_return=55, eligible=True),
        ]
        result = learn(rows)
        peaks = result["observed_4h_pump_research"]
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(peaks["distinct_observed_4h_peaks"], 2)
        self.assertEqual(peaks["observed_peak_ge_10_pct"], 2)
        self.assertEqual(peaks["observed_peak_ge_25_pct"], 1)
        self.assertEqual(peaks["observed_peak_ge_50_pct"], 0)
        self.assertEqual(peaks["blocking_gates_ge_10_pct"]["score"], 2)
        self.assertFalse(peaks["executable_peak_fills"])
        self.assertIsNone(result["candidate"])
        self.assertFalse(result["can_promote_champion"])

    def test_dashboard_does_not_call_observed_peaks_realized_profit(self):
        peaks=observed_peak_diagnostics([row("0xaaa",peak_return=30)])
        text = overview_report({
            "updated_at": timestamp(), "source_last_scan_at":timestamp(),
            "missed_pump_learning": {
                "status": "COLLECTING", "observed_candidates": 0,
                "missed_60m_positive_after_costs": 0,
                "observed_4h_pump_research": peaks,
            },
            "strategies": {}, "promotion_applied": False,
        }, {"generation": 0, "filters": {}})
        self.assertIn("růst ≥10 %: 1", text)
        self.assertIn("score (1)", text)
        self.assertIn("NIKOLI dosažitelné prodejní ceny", text)
        self.assertIn("Nový Champion: NE", text)


if __name__=="__main__":
    unittest.main()
