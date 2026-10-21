import unittest
from strategy_lab.recovery_v2_frozen_export import export


class FrozenExportTests(unittest.TestCase):
    def test_summary_only_is_not_a_zero_buy(self):
        result = export({"updated_at": "2026-10-20T10:00:00Z", "tokens": {}},
                        {"at": "2026-10-20T10:00:00Z", "fresh_tokens": 25})
        self.assertEqual(result["status"], "missing_frozen_candidates_or_tokens")
        self.assertEqual(result["records"], [])

    def test_future_market_rejected(self):
        result = export({"updated_at": "2026-10-20T10:01:00Z", "tokens": {}},
                        {"at": "2026-10-20T10:00:00Z", "candidate_snapshots": []})
        self.assertEqual(result["status"], "unaligned_or_future_market")

    def test_missing_token_is_explicit(self):
        result = export({"updated_at": "2026-10-20T10:00:00Z", "tokens": {}},
                        {"at": "2026-10-20T10:00:00Z", "candidate_snapshots": [{"address": "0xAbC"}]})
        self.assertEqual(result["status"], "research_only")
        self.assertEqual(result["records"][0]["reasons"], ["missing_market_token"])
        self.assertFalse(result["records"][0]["ready"])
        self.assertTrue(result["paper_unchanged"])


if __name__ == "__main__":
    unittest.main()
