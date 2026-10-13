"""Protect live PAPER closed ledger from silent resets and identity regression."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.paper_state_guard import validate


def trade(i, pnl):
    return {
        "address": f"0x{i:040x}",
        "opened_at": f"2026-10-13T00:{i:02d}:00Z",
        "closed_at": f"2026-10-13T00:{i:02d}:50Z",
        "status": "CLOSED", "closed_profit_sda": pnl,
    }


def fixture(rows):
    ledger = {"positions": {}, "closed_trades":rows}
    stats = {
        "closed_trades": len(rows),
        "realized_pnl_sda":sum(t["closed_profit_sda"] for t in rows),
        "wins":sum(t["closed_profit_sda"] > 0 for t in rows),
        "losses":sum(t["closed_profit_sda"] < 0 for t in rows),
    }
    return ledger, stats


class PaperStateGuardTests(unittest.TestCase):
    def test_monotonic_growth_preserves_sentinel(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            one=trade(1,-2)
            ledger, stats=fixture([one])
            x=validate(ledger,stats,p)
            self.assertEqual(x["current_closed"],1)
            before=json.loads(p.read_text())
            ledger2, stats2=fixture([one, trade(2,3)])
            result=validate(ledger2,stats2,p)
            self.assertEqual(result["current_closed"],2)
            self.assertEqual(len(json.loads(p.read_text())["closed_keys"]),2)
            self.assertEqual(len(before["closed_keys"]),1)

    def test_lost_closed_record_rejected_before_sentinel_write(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            one, two=trade(1,-2),trade(2,3)
            ledger,stats=fixture([one,two])
            validate(ledger,stats,p)
            previous=p.read_text()
            ledger2,stats2=fixture([two])
            with self.assertRaisesRegex(ValueError,"lost 1"):
                validate(ledger2,stats2,p)
            self.assertEqual(p.read_text(),previous)

    def test_missing_and_malformed_paper_state_rejected(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            good, st=fixture([trade(1,-2)])
            for candidate in ({}, {"positions":{}}, {"closed_trades":[]},
                              {"positions":{},"closed_trades":{}}):
                with self.assertRaises(ValueError):
                    validate(candidate,st,p)
            with self.assertRaises(ValueError):
                validate(good,{},p)
            self.assertFalse(p.exists())

    def test_matching_counts_but_incorrect_money_not_accepted(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            ledger,stats=fixture([trade(1,-2)])
            stats["realized_pnl_sda"]=-1
            with self.assertRaisesRegex(ValueError,"disagree"):
                validate(ledger,stats,p)
            self.assertFalse(p.exists())

    def test_unkeyed_and_duplicate_closed_rows_rejected(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            rows=[trade(1,-2)]
            bad=[{**rows[0],"closed_at":""}]
            ledger,stats=fixture(bad)
            with self.assertRaises(ValueError):
                validate(ledger,stats,p)
            ledger,stats=fixture(rows + deepcopy(rows))
            with self.assertRaises(ValueError):
                validate(ledger,stats,p)

    def test_corrupted_sentinel_is_not_overwritten(self):
        with TemporaryDirectory() as d:
            p=Path(d)/"sentinel.json"
            p.write_text("{}")
            ledger, stats=fixture([trade(1,-2)])
            with self.assertRaisesRegex(ValueError,"sentinel"):
                validate(ledger,stats,p)
            self.assertEqual(p.read_text(),"{}")


if __name__=="__main__":
    unittest.main()
