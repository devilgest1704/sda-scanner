import unittest
from tools.paper_buy_gate_diagnostics import analyze


class DiagnosticsTests(unittest.TestCase):
    def test_reports_blockers_without_mutating_state(self):
        row = {"phase": "IGNITION", "score": 75, "quality": 70,
               "impulse": 7, "data_age_sec": 30,
               "last": {"trades": 13, "vol": 600, "flow": 250,
                        "flow15": 70, "buy_ratio": 1.4, "m15": 2, "m1": 3}}
        state = {"updated_at": "2026-10-24T00:00:00Z", "history": {"a": row}}
        before = repr(state)
        report = analyze(state)
        self.assertEqual(report["tokens"], 1)
        self.assertEqual(report["all_observable_gates_pass"], 1)
        self.assertEqual(repr(state), before)

    def test_stale_low_impulse_is_blocked(self):
        state = {"history": {"a": {"phase": "WATCH", "score": 89,
                                  "quality": 89, "impulse": 0,
                                  "data_age_sec": 300, "last": {}}}}
        report = analyze(state)
        self.assertEqual(report["all_observable_gates_pass"], 0)
        self.assertEqual(report["gate_pass_counts"]["impulse_ge_6"], 0)


if __name__ == "__main__":
    unittest.main()
