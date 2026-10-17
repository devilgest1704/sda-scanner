"""Safety, chronology, accounting and restart tests for isolated temporal PAPER."""
import unittest
from datetime import datetime, timedelta, timezone

from tools.temporal_paper_trial import (
    fresh_state, step, summary, STRATEGY, validate_ledger
)

T0 = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
ADDRESS = "0xabc"


def source(t, opens=None, closes=None):
    return {
        "paper_only": True,
        "method": "forward_observed_quote_shadow_simulation",
        "last_snapshot_at": t.isoformat(),
        "strategies": {STRATEGY: {
            "positions": opens or {}, "trades": closes or [],
        }},
    }


def opened(t):
    return {
        ADDRESS: {
            "opened_at": t.isoformat(), "entry_price_sda": 1.0,
            "last_quote_at": (t - timedelta(seconds=10)).isoformat(),
        },
    }


def closed(t1, t2, profit=3.0, valid=True):
    return {
        "address": ADDRESS,
        "opened_at": t1.isoformat(), "closed_at": t2.isoformat(),
        "pnl_sda": profit, "valid_for_learning": valid,
        "reason": "TAKE" if profit > 0 else "STOP",
    }


ALLOW = {"blocked": False, "status": "ACTIVE"}
BLOCK = {"blocked": True, "status": "LOSS_COOLDOWN"}


class TemporalPaperTrialTests(unittest.TestCase):
    def test_initial_activation_does_not_backfill_old_winner(self):
        trial = fresh_state(T0)
        past = T0 - timedelta(minutes=1)
        result = step(trial, source(T0, opens=opened(past), closes=[
            closed(past-timedelta(minutes=5), past, 12)]), T0, ALLOW)
        self.assertEqual(result["opened"], 0)
        self.assertEqual(len(trial["closed_trades"]), 0)
        self.assertEqual(summary(trial)["realized_pnl_sda"], 0)

    def test_new_entry_and_subsequent_forward_close(self):
        trial = fresh_state(T0)
        at = T0 + timedelta(minutes=1)
        first = step(trial, source(at, opens=opened(at)), at, ALLOW)
        self.assertEqual(first["opened"], 1)
        self.assertEqual(first["closed"], 0)
        self.assertEqual(summary(trial)["open_notional_sda"], 50)
        self.assertEqual(step(trial, source(at, opens=opened(at)), at, ALLOW)["status"],
                         "already_processed")
        after = at + timedelta(minutes=5)
        finished = step(trial, source(after, closes=[closed(at, after, 4.2)]),
                        after, BLOCK)  # blocking must NOT block exits
        self.assertEqual(finished["closed"], 1)
        self.assertEqual(summary(trial)["realized_pnl_sda"], 4.2)
        self.assertEqual(summary(trial)["open_positions"], 0)
        self.assertEqual(summary(trial)["valid_quote_closes"], 1)

    def test_global_risk_stops_new_entries(self):
        trial = fresh_state(T0)
        at = T0 + timedelta(minutes=1)
        result = step(trial, source(at, opens=opened(at)), at, BLOCK)
        self.assertEqual(result["opened"], 0)
        self.assertEqual(trial["rejected_entries"]["main_loss_circuit"], 1)

    def test_unknown_global_risk_fails_closed(self):
        trial = fresh_state(T0)
        at = T0 + timedelta(minutes=1)
        self.assertEqual(step(trial, source(at, opens=opened(at)),
                              at, {"status": "UNKNOWN"})["opened"], 0)

    def test_unverified_exit_cannot_credit_profit(self):
        trial = fresh_state(T0)
        at = T0 + timedelta(minutes=1)
        step(trial, source(at, opens=opened(at)), at, ALLOW)
        later = at + timedelta(minutes=5)
        step(trial, source(later, closes=[closed(at, later, 20, False)]),
             later, ALLOW)
        self.assertEqual(summary(trial)["realized_pnl_sda"], -50)
        self.assertEqual(summary(trial)["valid_quote_closes"], 0)

    def test_independent_24h_loss_circuit(self):
        trial = fresh_state(T0)
        for i in range(8):
            trial["closed_trades"].append({
                "id": str(i), "address": ADDRESS, "status": "CLOSED",
                "opened_at": (T0 - timedelta(hours=1)).isoformat(),
                "closed_at": (T0 - timedelta(minutes=1)).isoformat(),
                "closed_profit_sda": -3.0,
            })
        at = T0 + timedelta(minutes=1)
        self.assertEqual(step(trial, source(at, opens=opened(at)), at, ALLOW)["opened"], 0)
        self.assertEqual(trial["rejected_entries"]["trial_loss_circuit"], 1)

    def test_stale_snapshot_fails_closed(self):
        trial = fresh_state(T0)
        with self.assertRaises(ValueError):
            step(trial, source(T0, opens=opened(T0)),
                 T0 + timedelta(minutes=4), ALLOW)

    def test_invalid_state_does_not_silently_reset(self):
        with self.assertRaises(ValueError):
            validate_ledger({"schema": 1, "positions": {}, "closed_trades": []})


if __name__ == "__main__":
    unittest.main()
