"""Regression: crossing the 500-paper-close boundary must not erase SDA P/L."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import engine_legacy as engine
import paper_stats
from tools.paper_state_guard import validate


def old_record(i):
    return {
        "address": f"0x{i:040x}",
        "opened_at": f"2026-10-01T10:{i % 60:02d}:00Z",
        "closed_at": f"2026-10-02T10:{i % 60:02d}:{i // 60:02d}Z",
        "status": "CLOSED",
        "closed_profit_sda": -1.0,
        "investment_sda": 50.0,
        "closed_fraction": 1.0,
    }


def open_position():
    return {
        "address": "0xnewposition",
        "entry_price": 1.0,
        "investment_sda": 50.0,
        "remaining_fraction": 1.0,
        "status": "OPEN",
        "opened_at": "2026-10-13T10:00:00Z",
    }


def published_stats(rows):
    values = [x["closed_profit_sda"] for x in rows]
    return {
        "closed_trades": len(rows),
        "realized_pnl_sda": sum(values),
        "wins": sum(x > 0 for x in values),
        "losses": sum(x < 0 for x in values),
    }


class PaperAccountingOver500Tests(unittest.TestCase):
    def test_close_501_retains_earliest_trade_and_reconciles(self):
        prior = [old_record(i) for i in range(500)]
        positions = {"positions": {"0xnewposition": open_position()},
                     "closed_trades": prior}
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel.json"
            validate(positions, published_stats(prior), sentinel)

            result = engine.close(positions, "0xnewposition", 1.15, "TEST TRAILING")
            self.assertIsNotNone(result)
            self.assertEqual(len(positions["closed_trades"]), 501)
            self.assertEqual(positions["closed_trades"][0], prior[0])
            self.assertEqual(positions["closed_trades"][-1]["close_reason"], "TEST TRAILING")

            total = published_stats(positions["closed_trades"])
            observed = validate(positions, total, sentinel)
            self.assertEqual(observed["current_closed"], 501)
            self.assertTrue(observed["paper_stats_match"])
            self.assertAlmostEqual(observed["realized_pnl_sda"],
                                   -500.0 + result["closed_profit_sda"], places=5)

    def test_partial_fill_is_valid_and_no_prior_close_is_removed(self):
        old = old_record(0)
        pos = open_position()
        positions = {"positions": {"0xnewposition": pos}, "closed_trades": [old]}
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel.json"
            validate(positions, published_stats([old]), sentinel)
            partial = engine.close(positions, "0xnewposition", 1.08, "TEST TP1",
                                   fraction=0.4)
            self.assertEqual(partial["status"], "PARTIAL CLOSED")
            self.assertEqual(len(positions["closed_trades"]), 2)
            self.assertEqual(positions["positions"]["0xnewposition"]["remaining_fraction"], 0.6)
            validate(positions, published_stats(positions["closed_trades"]), sentinel)

    def test_end_to_end_paper_stats_count_and_pnl_above_500(self):
        with TemporaryDirectory() as directory:
            workdir = Path(directory)
            rows = [old_record(i) for i in range(500)]
            positions = {"positions": {"0xnewposition": open_position()},
                         "closed_trades": rows}
            result = engine.close(positions, "0xnewposition", 1.15, "TEST")
            (workdir / "positions.json").write_text(json.dumps(positions))
            (workdir / "market_data.json").write_text(json.dumps({"tokens": {}}))
            previous = Path.cwd()
            try:
                os.chdir(workdir)
                with patch.object(paper_stats, "TOKEN", None), patch.object(
                        paper_stats, "CHAT_ID", None), contextlib.redirect_stdout(io.StringIO()):
                    paper_stats.main()
            finally:
                os.chdir(previous)
            stats = json.loads((workdir / "paper_stats.json").read_text())
            self.assertEqual(stats["closed_trades"], 501)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["losses"], 500)
            self.assertAlmostEqual(stats["realized_pnl_sda"],
                                   -500 + result["closed_profit_sda"], places=5)


if __name__ == "__main__":
    unittest.main()
