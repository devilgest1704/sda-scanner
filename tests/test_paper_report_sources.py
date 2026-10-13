"""CLI integration: only CURRENT closed positions may count as current P/L."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.export_paper_trades import export
from tools.paper_reconciliation import main as reconciliation_main
from tools.v31_profit_learning import main as v31_main, net_exit
from tools.paper_trade_lessons import main as lessons_main


def trade(symbol, opened, closed, close_price):
    entry = 1.0
    return {
        "address": symbol, "status": "CLOSED", "label": symbol+"/SDA",
        "opened_at": opened, "closed_at": closed,
        "entry_price": entry, "close_price": close_price,
        "investment_sda": 50, "closed_profit_sda": net_exit(entry, close_price, 50),
        "close_reason": "V30.3 TRAILING/STOP", "v30_mode": "V30.8",
    }


class PaperReportSourceTests(unittest.TestCase):
    def test_archive_only_trade_never_inflates_accounting_reports(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            current=trade("0xcurr", "2026-10-12T08:00:00Z",
                          "2026-10-12T09:00:00Z", .98)
            retired=trade("0xold", "2026-10-09T08:00:00Z",
                          "2026-10-09T09:00:00Z", .70)
            ledger=folder/"positions.json"
            archive=folder/"closed_trades.json"
            stats=folder/"paper_stats.json"
            v31out=folder/"v31.json"
            lessonsout=folder/"lessons.json"
            auditout=folder/"reconciliation.json"
            ledger.write_text(json.dumps({"positions":{}, "closed_trades":[current]}))
            archive.write_text(json.dumps([retired]))
            stats.write_text(json.dumps({
                "updated_at":"2026-10-13T06:00:00Z",
                "closed_trades":1,
                "realized_pnl_sda":current["closed_profit_sda"],
                "wins":0,"losses":1,
            }))
            # Additive historical export preserves genuinely old observations.
            archived, current_count=export(ledger,archive)
            self.assertEqual((archived,current_count),(2,1))
            with patch.object(sys,"argv",["v31",
                  "--ledger",str(ledger),"--archive",str(archive),
                  "--stats",str(stats),"--output",str(v31out)]):
                v31_main()
            with patch.object(sys,"argv",["lessons",
                  "--ledger",str(ledger),"--stats",str(stats),
                  "--output",str(lessonsout)]):
                lessons_main()
            with patch.object(sys,"argv",["reconciliation",
                  "--ledger",str(ledger),"--archive",str(archive),
                  "--stats",str(stats),"--output",str(auditout)]):
                reconciliation_main()
            v31=json.loads(v31out.read_text())
            lessons=json.loads(lessonsout.read_text())
            audit=json.loads(auditout.read_text())
            self.assertEqual(v31["historical_observed"]["overall"]["closed"],1)
            self.assertAlmostEqual(v31["historical_observed"]["overall"]["net_sda"],
                                   current["closed_profit_sda"],places=3)
            self.assertTrue(v31["statistics_reconciled"])
            self.assertEqual(v31["accounting_source"],
                             "CURRENT_positions_json_closed_trades_only")
            self.assertEqual(v31["ledger_reconciliation"]["historic_only_count"],1)
            self.assertTrue(lessons["statistics_reconciled"])
            self.assertEqual(lessons["overall"]["closed"],1)
            self.assertFalse(lessons["historical_union_included"])
            self.assertEqual(audit["archive"]["historic_only_count"],1)
            self.assertTrue(audit["current_accounting_reconciled"])
            self.assertEqual(len(json.loads(archive.read_text())),2)
            self.assertEqual(len(json.loads(ledger.read_text())["closed_trades"]),1)

    def test_bad_current_ledger_fails_instead_of_falling_back_to_archive(self):
        with TemporaryDirectory() as d:
            base=Path(d)
            bad=base/"missing.json"
            archive=base/"archive.json"
            stats=base/"stats.json"
            archive.write_text(json.dumps([trade("0xX","2026-10-12T07:00:00Z",
                                                 "2026-10-12T08:00:00Z",1.5)]))
            stats.write_text(json.dumps({"closed_trades":1,"realized_pnl_sda":24}))
            with patch.object(sys,"argv",["v31","--ledger",str(bad),
                  "--archive",str(archive),"--stats",str(stats)]):
                with self.assertRaises(OSError):
                    v31_main()


if __name__=="__main__":
    unittest.main()
