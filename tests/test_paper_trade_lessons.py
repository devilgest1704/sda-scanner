import unittest
from tools.paper_trade_lessons import analyze, summary, normalize_trade


class HistoricalPaperLessonsTests(unittest.TestCase):
    def test_pine_and_loss_diagnostics_are_read_only(self):
        rows=[
            {"status":"CLOSED","label":"AITT/SDA","closed_at":"2026-10-10T12:07:00Z",
             "closed_profit_sda":-10.47,"close_reason":"V30.3 EMERGENCY/HARD STOP"},
            {"status":"CLOSED","label":"PINE/SDA","closed_at":"2026-10-10T16:20:00Z",
             "closed_profit_sda":14.21,"closed_roi_pct":14.07,
             "close_reason":"V30.3 TRAILING/STOP","entry_confidence":90},
            {"status":"OPEN","label":"FAKE","closed_at":"2026-10-10T12:00:00Z",
             "closed_profit_sda":3000},
        ]
        original=[dict(x) for x in rows]
        report=analyze(rows,{"realized_pnl_sda":3.74,"closed_trades":2})
        self.assertEqual(report["overall"]["closed"],2)
        self.assertAlmostEqual(report["overall"]["net_pnl_sda"],3.74)
        self.assertTrue(report["statistics_reconciled"])
        self.assertEqual(report["pine_recent"]["pnl_sda"],14.21)
        self.assertFalse(report["strategy_changed"])
        self.assertFalse(report["validates_counterfactual"])
        self.assertEqual(rows,original)

    def test_reconciliation_rejects_unmatched_stats(self):
        rows=[{"status":"CLOSED","closed_at":"2026-10-10T16:20:00Z",
               "closed_profit_sda":14.21}]
        self.assertFalse(analyze(rows,{"closed_trades":2,"realized_pnl_sda":14.21})["statistics_reconciled"])

    def test_ignore_invalid_nonfinite_pnl(self):
        rows=[{"status":"CLOSED","closed_at":"2026-10-10","closed_profit_sda":float("nan")}]
        self.assertEqual(analyze(rows)["overall"]["closed"],0)
        self.assertIsNone(normalize_trade(rows[0]))

    def test_break_even_win_rate(self):
        s=summary([{"pnl_sda":20},{"pnl_sda":-5}])
        self.assertEqual(s["breakeven_win_rate_pct"],20.0)
        self.assertEqual(s["profit_factor"],4)


if __name__=="__main__":
    unittest.main()
