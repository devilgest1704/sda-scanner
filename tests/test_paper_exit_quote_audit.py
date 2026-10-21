import unittest
from strategy_lab.paper_exit_quote_audit import audit


class QuoteAuditTests(unittest.TestCase):
    def test_stale_flag_not_proof_of_bad_exit(self):
        result = audit({"closed_trades": [{"label": "TSLA", "pump_exit_data_stale": True}]})
        self.assertEqual(result["review_count"], 1)
        self.assertFalse(result["records"][0]["proven_bad_execution"])
        self.assertIsNone(result["records"][0]["exit_quote_age_sec"])

    def test_fresh_quote_not_flagged(self):
        result = audit({"closed_trades": [{"closed_at": "2026-10-21T06:00:00Z",
                                            "exit_quote_at": "2026-10-21T05:59:00Z"}]})
        self.assertEqual(result["review_count"], 0)

    def test_old_quote_flagged(self):
        result = audit({"closed_trades": [{"closed_at": "2026-10-21T06:00:00Z",
                                            "exit_quote_at": "2026-10-21T05:40:00Z"}]})
        self.assertEqual(result["records"][0]["exit_quote_age_sec"], 1200)
        self.assertEqual(result["review_count"], 1)


if __name__ == "__main__":
    unittest.main()
