import unittest
from tools.reentry_cooldown_audit import audit


class ReentryCooldownAuditTests(unittest.TestCase):
    def test_only_reentries_after_loss_are_flagged(self):
        def trade(address, opened, closed, pnl):
            return {"status":"CLOSED", "address":address, "opened_at":opened,
                    "closed_at":closed, "closed_profit_sda":pnl}
        rows=[
            trade("0xABC","2026-10-01T00:00:00Z","2026-10-01T01:00:00Z",-4),
            trade("0xabc","2026-10-01T05:00:00Z","2026-10-01T06:00:00Z",2),
            trade("0xabc","2026-10-01T07:00:00Z","2026-10-01T08:00:00Z",3),
            trade("0xDEF","2026-10-01T02:00:00Z","2026-10-01T03:00:00Z",-1),
        ]
        original=[dict(row) for row in rows]
        result=audit(rows,windows=(6,12))
        self.assertEqual(result["eligible_closed_trades"],4)
        self.assertEqual([w["historical_entries_flagged"] for w in result["windows"]],[1,1])
        self.assertEqual(result["windows"][0]["flagged_observed_pnl_sda"],2)
        self.assertTrue(result["not_counterfactual_portfolio_pnl"])
        self.assertEqual(rows,original)

    def test_missing_or_invalid_rows_ignored(self):
        rows=[{"status":"OPEN","address":"a","opened_at":"2026-10-01T00:00:00Z",
               "closed_at":"2026-10-01T01:00:00Z","closed_profit_sda":-1},
              {"status":"CLOSED","address":"b","opened_at":"bad",
               "closed_at":"2026-10-01T01:00:00Z","closed_profit_sda":-1}]
        self.assertEqual(audit(rows)["eligible_closed_trades"],0)


if __name__=="__main__":
    unittest.main()
