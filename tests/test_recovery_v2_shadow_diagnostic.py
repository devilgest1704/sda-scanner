import unittest
from strategy_lab.recovery_v2_shadow_diagnostic import diagnose


class RecoveryV2DiagnosticTests(unittest.TestCase):
    def test_summary_only_is_not_zero_buy(self):
        d = diagnose({"tokens": {}}, {"funnel": {"at": "2026-10-19T00:00:00Z", "fresh_tokens": 22}})
        self.assertEqual(d["status"], "missing_candidate_snapshots")
        self.assertIsNone(d["ready"])

    def test_unaligned_rejected(self):
        d = diagnose({"tokens": {}, "updated_at": "2026-10-18T00:00:00Z"}, {"candidate_snapshots": [], "at": "2026-10-19T00:00:00Z"})
        self.assertEqual(d["status"], "unaligned_market_snapshot")

    def test_empty(self):
        self.assertEqual(diagnose({"tokens": {}, "updated_at": "2026-10-19T00:00:00Z"}, {"candidate_snapshots": [], "at": "2026-10-19T00:00:00Z"})["ready"], 0)

    def test_missing_token_fails_closed(self):
        d = diagnose({"tokens": {}, "updated_at": "2026-10-19T00:00:00Z"}, {"candidate_snapshots": [{"address": "0xabc"}], "at": "2026-10-19T00:00:00Z"})
        self.assertEqual(d["reasons"]["missing_market_token"], 1)
        self.assertTrue(d["paper_unchanged"])

    def test_missing_macd_fails_closed(self):
        d = diagnose({"tokens": {"0xabc": {"analysis": {}}}, "updated_at": "2026-10-19T00:00:00Z"}, {"candidate_snapshots": [{"address": "0xabc"}], "at": "2026-10-19T00:00:00Z"})
        self.assertEqual(d["reasons"]["macd_not_verified"], 1)


if __name__ == "__main__":
    unittest.main()
