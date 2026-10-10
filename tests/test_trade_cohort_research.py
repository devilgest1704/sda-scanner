import unittest
from tools.trade_cohort_research import evaluate

class TradeCohortTests(unittest.TestCase):
    def test_chronological_holdout_and_no_promotion(self):
        rows=[]
        for i in range(100):
            rows.append({"status":"CLOSED","closed_at":f"2026-10-{i//10+1:02d}T{i%10:02d}:00:00Z",
                         "entry_confidence":80 if i%2==0 else 70,
                         "closed_profit_sda":2 if i%2==0 else -3})
        r=evaluate(rows,75)
        self.assertEqual(r["train"]["observed_trades"],70)
        self.assertEqual(r["holdout"]["observed_trades"],30)
        self.assertEqual(r["holdout"]["retained_trades"],15)
        self.assertFalse(r["promotion_eligible"])
        self.assertTrue(r["sufficient_sample"])

    def test_insufficient_data_fails_closed(self):
        r=evaluate([{"status":"CLOSED","closed_at":"2026-10-01",
                     "entry_confidence":90,"closed_profit_sda":2}],75)
        self.assertFalse(r["sufficient_sample"])
        self.assertFalse(r["promotion_eligible"])

    def test_invalid_and_partial_ignored(self):
        rows=[{"status":"PARTIAL CLOSED","closed_at":"2026-10-01",
               "entry_confidence":80,"closed_profit_sda":1},
              {"status":"CLOSED","closed_at":"2026-10-02",
               "entry_confidence":80,"closed_profit_sda":"nan"}]
        self.assertEqual(evaluate(rows)["sample_count"],0)

if __name__=="__main__":unittest.main()
