"""Regression tests for the read-only PAPER diagnostic."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import paper_risk_diagnostics as diagnostic


class PaperRiskDiagnosticTests(unittest.TestCase):
    def test_reports_stale_coverage_and_ledger_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "strategy_lab").mkdir()
            data = {
                "paper_stats.json": {
                    "closed_trades": 539, "realized_pnl_sda": -1440.42,
                    "open_positions": 0, "win_rate_pct": 18.18,
                },
                "v30_no_buy_diagnostic.json": {
                    "market": {
                        "market_at": "2020-01-01T00:00:00+00:00",
                        "tokens": 168, "quotes_last_5m": 4,
                    },
                    "paper_loss_circuit": {
                        "blocked": True, "status": "LOSS_COOLDOWN",
                        "window_pnl_sda": -26.58,
                    },
                    "funnel": {"champion_ready": 0, "risk_only_rejected_rows": 0},
                },
                "strategy_lab/trade_lessons.json": {
                    "generated_at": "2026-10-16T08:17:40+00:00",
                    "overall": {"closed": 536, "net_pnl_sda": -1427.72},
                    "by_exit_reason": [
                        {"reason": "STOP", "closed": 2, "net_pnl_sda": -10.0},
                        {"reason": "TAKE", "closed": 1, "net_pnl_sda": 5.0},
                    ],
                },
            }
            for path, payload in data.items():
                (root / path).write_text(json.dumps(payload), encoding="utf-8")
            output = io.StringIO()
            with patch.object(diagnostic, "ROOT", root), contextlib.redirect_stdout(output):
                diagnostic.main()
            report = json.loads(output.getvalue())
            self.assertEqual(report["paper"]["closed"], 539)
            self.assertEqual(report["lessons"]["top_negative_exit_reasons"][0]["reason"], "STOP")
            self.assertEqual(set(report["warnings"]), {
                "MARKET_DATA_STALE_OVER_15_MIN",
                "QUOTE_COVERAGE_BELOW_10_PERCENT",
                "TRADE_LESSONS_NOT_CURRENT",
                "PAPER_LOSS_CIRCUIT_ACTIVE",
            })


if __name__ == "__main__":
    unittest.main()
