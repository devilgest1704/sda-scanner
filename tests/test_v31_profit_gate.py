"""Unit tests for V31 shadow gate; no production trading changes."""
import unittest
from experiments.v31_profit_gate import evaluate

GOOD = dict(trades=12,vol=1200,flow=250,buy_ratio=2.0,m15=4.0,m1=3.0,quality=80,data_age_sec=30)

class TestProfitGate(unittest.TestCase):
    def test_good_snapshot(self):
        self.assertTrue(evaluate(GOOD)["eligible"])
        self.assertEqual(evaluate(GOOD)["mode"], "SHADOW_ONLY")

    def test_missing_field_fails_closed(self):
        sample = dict(GOOD)
        del sample["flow"]
        self.assertFalse(evaluate(sample)["eligible"])

    def test_stale_data_rejected(self):
        self.assertFalse(evaluate({**GOOD, "data_age_sec": 900})["eligible"])

    def test_spike_rejected(self):
        self.assertFalse(evaluate({**GOOD, "m15": 30})["eligible"])

    def test_invalid_data_rejected(self):
        self.assertFalse(evaluate({**GOOD, "vol": float("nan")})["eligible"])

if __name__ == "__main__":
    unittest.main()
