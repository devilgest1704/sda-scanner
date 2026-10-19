import unittest
from strategy_lab.recovery_v2_shadow_diagnostic import diagnose


class RecoveryV2DiagnosticTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(diagnose({"tokens": {}}, {"candidate_snapshots": [], "at": "2026-10-19T00:00:00Z"})["ready"], 0)

    def test_missing_token_fails_closed(self):
        d = diagnose({"tokens": {}}, {"candidate_snapshots": [{"address": "0xabc"}], "at": "2026-10-19T00:00:00Z"})
        self.assertEqual(d["reasons"]["missing_market_token"], 1)
        self.assertTrue(d["paper_unchanged"])

    def test_missing_macd_fails_closed(self):
        d = diagnose({"tokens": {"0xabc": {"analysis": {}}}}, {"candidate_snapshots": [{"address": "0xabc"}], "at": "2026-10-19T00:00:00Z"})
        self.assertEqual(d["reasons"]["macd_not_verified"], 1)


if __name__ == "__main__":
    unittest.main()
