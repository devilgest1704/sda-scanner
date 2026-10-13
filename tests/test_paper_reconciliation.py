import unittest
from copy import deepcopy
from datetime import datetime, timezone
from tools.paper_reconciliation import reconcile


def t(address, date, pnl, status="CLOSED"):
    return {"address": address, "opened_at": "2026-10-12T09:00:00Z",
            "closed_at": date, "status": status, "closed_profit_sda": pnl}


def stats(rows):
    return {"updated_at": "2026-10-13T05:00:00Z",
            "closed_trades": len(rows),
            "realized_pnl_sda": sum(x["closed_profit_sda"] for x in rows),
            "wins": sum(x["closed_profit_sda"] > 0 for x in rows),
            "losses": sum(x["closed_profit_sda"] < 0 for x in rows)}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.active = [
            t("0xA", "2026-10-12T09:10:00Z", -4.5),
            t("0xB", "2026-10-12T09:20:00Z", 3.0),
        ]
        self.retired = t("0xC", "2026-10-10T14:00:00Z", -7.2)

    def test_append_only_archive_does_not_change_live_pnl(self):
        rows = deepcopy(self.active)
        archive = rows + [self.retired]
        before = deepcopy(rows)
        result = reconcile({"closed_trades": rows}, archive, stats(rows))
        self.assertTrue(result["current_accounting_reconciled"])
        self.assertFalse(result["historical_archive_matches_current_ledger"])
        self.assertEqual(result["archive"]["historic_only_count"], 1)
        self.assertEqual(result["archive"]["historic_only_sum_sda"], -7.2)
        self.assertEqual(result["current_ledger"]["calculated_realized_pnl_sda"], -1.5)
        self.assertEqual(result["published_stats"]["closed_rows"], 2)
        self.assertFalse(result["promotion_eligible"])
        self.assertFalse(result["production_mutated"])
        self.assertEqual(rows, before)

    def test_archived_only_missing_pnl_is_not_silently_converted_to_live_pnl(self):
        past = deepcopy(self.retired)
        past["closed_profit_sda"] = None
        report = reconcile({"closed_trades": self.active},
                           self.active + [past], stats(self.active))
        self.assertTrue(report["current_accounting_reconciled"])
        self.assertEqual(report["archive"]["historic_only_count"], 1)
        self.assertEqual(report["archive"]["historic_only_samples"][0]["pnl_sda"], None)

    def test_archive_delta_current_only_and_identity_pnl_conflict(self):
        ledger = deepcopy(self.active)
        archive = [deepcopy(ledger[0])]
        archive[0]["closed_profit_sda"] = 500
        result = reconcile({"closed_trades":ledger}, archive, stats(ledger))
        self.assertEqual(result["archive"]["current_only_count"], 1)
        self.assertEqual(result["archive"]["same_identity_different_pnl_count"], 1)
        self.assertTrue(result["current_accounting_reconciled"])

    def test_invalid_stats_are_detected_without_rewriting(self):
        st = stats(self.active)
        st["closed_trades"] = 20
        self.assertFalse(reconcile({"closed_trades":self.active}, self.active, st)
                         ["current_accounting_reconciled"])
        st = stats(self.active)
        st["realized_pnl_sda"] += .9
        self.assertFalse(reconcile({"closed_trades":self.active}, self.active, st)
                         ["current_accounting_reconciled"])
        st = stats(self.active)
        st["losses"] += 1
        self.assertFalse(reconcile({"closed_trades":self.active}, self.active, st)
                         ["current_accounting_reconciled"])

    def test_invalid_current_amounts_and_nonclosed_status_are_explicit(self):
        rows = deepcopy(self.active)
        rows[1]["closed_profit_sda"] = "inf"
        rows[1]["status"] = "OPEN"
        r = reconcile({"closed_trades": rows}, rows, stats(self.active))
        self.assertFalse(r["current_accounting_reconciled"])
        self.assertEqual(r["current_ledger"]["invalid_pnl_rows"], 1)
        self.assertEqual(r["current_ledger"]["nonclosed_status_rows"], 1)

    def test_bad_ledger_or_history_fails_closed(self):
        for src in ({}, [], {"closed_trades": None}):
            with self.assertRaises(ValueError):
                reconcile(src, [], stats(self.active))
        with self.assertRaises(ValueError):
            reconcile({"closed_trades":self.active}, {}, stats(self.active))

    def test_duplicate_archive_identity_is_measured_not_deleted(self):
        archive = self.active + [deepcopy(self.active[0])]
        report = reconcile({"closed_trades":self.active}, archive, stats(self.active))
        self.assertEqual(report["archive"]["duplicate_identity_rows"], 1)
        self.assertEqual(report["archive"]["total_rows"], 3)
        self.assertFalse(report["historical_archive_matches_current_ledger"])


if __name__ == "__main__":
    unittest.main()
