"""Ensure no-BUY audit does not turn absent signals into a trading fault."""
import unittest
from datetime import datetime, timedelta, timezone

from tools.no_buy_diagnostic import diagnose, market_coverage

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


def market(m15=2.5, trades=12, net=300, minutes_old=1):
    last = (NOW - timedelta(minutes=minutes_old)).isoformat()
    return {"updated_at": NOW.isoformat(), "tokens": {
        "0xtoken": {"analysis": {
            "price_in_sda": 1.2,
            "last_transaction": last,
            "momentum": {"15m_pct": m15, "1h_pct": 3},
            "flow": {"1h": {
                "transactions": trades, "net_flow": net,
                "total_volume": 900,
            }},
        }},
    }}


def funnel(gates=None, champion=0, minutes_ago=0):
    gates = ["score", "lifecycle"] if gates is None else gates
    return {"funnels": [{
        "at": (NOW-timedelta(minutes=minutes_ago)).isoformat(),
        "tokens": 1, "fresh": 1,
        "champion_ready": champion, "shadow_ready": 0,
        "candidate_snapshots": [{
            "address": "0xtoken", "phase": "WATCH",
            "gates": gates, "score": 50,
        }],
        "near_ready": [{
            "address": "0xtoken", "gates": gates,
        }],
        "fresh_gate_counts": {name: 1 for name in gates},
    }]}


def ledger(losses=0):
    rows = [{
        "status": "CLOSED",
        "closed_at": (NOW - timedelta(minutes=i+3)).isoformat(),
        "closed_profit_sda": -3.0,
    } for i in range(losses)]
    return {"closed_trades": rows}


class NoBuyDiagnosisTests(unittest.TestCase):
    def test_missing_signals_distinguished_from_blocking_circuit(self):
        report = diagnose(market(), funnel(), ledger(8), NOW)
        self.assertEqual(report["assessment"], "NO_ELIGIBLE_BUY_SIGNALS_OBSERVED")
        self.assertTrue(report["paper_loss_circuit"]["blocked"])
        self.assertEqual(report["paper_loss_circuit"]["window_pnl_sda"], -24)
        self.assertEqual(report["funnel"]["risk_only_rejected_rows"], 0)
        self.assertFalse(report["paper_buy_rules_modified"])
        self.assertFalse(report["real_trading"])

    def test_risk_only_candidate_identified_without_disabling_protection(self):
        report = diagnose(market(), funnel(["loss_circuit"]), ledger(8), NOW)
        self.assertEqual(report["assessment"], "ONLY_PAPER_LOSS_CIRCUIT_BLOCKED_SOME_SIGNALS")
        self.assertEqual(report["funnel"]["risk_only_rejected_rows"], 1)
        self.assertTrue(report["paper_loss_circuit"]["blocked"])

    def test_ready_candidate_requires_execution_check(self):
        report = diagnose(market(), funnel([], champion=1), ledger(), NOW)
        self.assertEqual(report["assessment"], "CHAMPION_BUY_READY_CHECK_EXECUTION_CAPACITY")

    def test_stale_funnel_not_reported_as_no_signal(self):
        report = diagnose(market(), funnel(minutes_ago=50), ledger(), NOW)
        self.assertEqual(report["assessment"], "STALE_OR_UNALIGNED_SCANNER_OBSERVATIONS")
        self.assertEqual(report["funnel"]["status"], "STALE_OR_NOT_TIME_ALIGNED")

    def test_inactive_market_and_future_trades_do_not_count_as_fresh(self):
        m = market(minutes_old=300)
        m["tokens"]["future"] = {"analysis": {
            "price_in_sda": 2.0,
            "last_transaction": (NOW+timedelta(minutes=1)).isoformat(),
        }}
        result = market_coverage(m)
        self.assertEqual(result["quotes_last_5m"], 0)
        self.assertEqual(result["tokens"], 2)
        self.assertEqual(result["fresh_with_12_trades_1h"], 0)


if __name__ == "__main__":
    unittest.main()
