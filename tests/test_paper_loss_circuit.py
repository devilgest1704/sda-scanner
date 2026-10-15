"""Deterministic risk-circuit safety and integration tests."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.paper_loss_circuit import (
    evaluate_closed, paper_entry_risk_gate, _read_ledger
)
import v30_pump_hunter as v30

AT = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)


def trade(pnl, hours_ago=2):
    return {"status": "CLOSED", "closed_profit_sda": pnl,
            "closed_at": (AT - timedelta(hours=hours_ago)).isoformat()}


class PaperLossCircuitTests(unittest.TestCase):
    def test_eight_recent_losses_hit_budget(self):
        gate = evaluate_closed([trade(-3) for _ in range(8)], AT)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["status"], "LOSS_COOLDOWN")
        self.assertEqual(gate["window_closed"], 8)
        self.assertEqual(gate["window_pnl_sda"], -24)

    def test_recovery_projection_updates_as_losses_leave_rolling_window(self):
        # Eight losses hit -24. After the earliest closes age out the
        # risk gate MUST reopen automatically, without changing thresholds.
        data = [trade(-3, hours_ago=23) for _ in range(3)]
        data += [trade(-3, hours_ago=4) for _ in range(5)]
        blocked = evaluate_closed(data, AT)
        self.assertTrue(blocked["blocked"])
        eta = (AT + timedelta(hours=1, microseconds=1)).isoformat()
        self.assertEqual(blocked["projected_recovery_at_no_new_closes"], eta)
        self.assertFalse(evaluate_closed(data, AT+timedelta(hours=1, seconds=1))["blocked"])
        self.assertIsNone(evaluate_closed(data, AT+timedelta(hours=2))[
            "projected_recovery_at_no_new_closes"])

    def test_recovery_projection_accounts_for_winner_expiry(self):
        # Expiring a recent WIN may worsen the 24h result. The prediction
        # must keep iterating until remaining losses fall below the budget.
        data = [trade(10, hours_ago=23)]
        data += [trade(-5, hours_ago=22) for _ in range(7)]
        data += [trade(-4, hours_ago=10) for _ in range(2)]
        gate = evaluate_closed(data, AT)
        self.assertTrue(gate["blocked"])
        # After the win expires, still blocked. After old losses expire,
        # fewer than 8 closes remain and the gate clears.
        self.assertEqual(gate["projected_recovery_at_no_new_closes"],
                         (AT+timedelta(hours=2, microseconds=1)).isoformat())

    def test_insufficient_sample_does_not_invent_negative_result(self):
        gate = evaluate_closed([trade(-9) for _ in range(2)], AT)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["status"], "COLLECTING")

    def test_healthy_cohort_remains_allowed(self):
        gate = evaluate_closed([trade(-3) for _ in range(6)] +
                               [trade(4) for _ in range(2)], AT)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["status"], "ACTIVE")

    def test_expiry_allows_future_paper_entries_automatically(self):
        trades = [trade(-4, hours_ago=20) for _ in range(8)]
        self.assertTrue(evaluate_closed(trades, AT)["blocked"])
        self.assertFalse(evaluate_closed(trades, AT + timedelta(hours=5))["blocked"])

    def test_old_and_future_quotes_do_not_poison_rolling_window(self):
        rows = [trade(-20, hours_ago=26) for _ in range(8)]
        rows += [trade(-20, hours_ago=-1)]
        gate = evaluate_closed(rows, AT)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["window_closed"], 0)

    def test_corrupt_or_missing_ledger_fails_closed_for_new_entries(self):
        self.assertEqual(evaluate_closed(None, AT)["status"], "INVALID_LEDGER")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            self.assertTrue(paper_entry_risk_gate(AT, path)["blocked"])
            path.write_text("{broken", encoding="utf-8")
            _read_ledger.cache_clear()
            self.assertTrue(paper_entry_risk_gate(AT, path)["blocked"])
            path.write_text(json.dumps({"closed_trades":[trade(-4) for _ in range(8)]}),
                            encoding="utf-8")
            _read_ledger.cache_clear()
            self.assertTrue(paper_entry_risk_gate(AT, path)["blocked"])

    def test_nonfinite_current_closed_pnl_fails_closed(self):
        x=evaluate_closed([trade(float("nan"))], AT)
        self.assertTrue(x["blocked"])
        self.assertEqual(x["status"], "INVALID_LEDGER")

    def test_old_negative_history_does_not_permanently_block_entries(self):
        rows=[trade(-10, hours_ago=120) for _ in range(524)]
        self.assertFalse(evaluate_closed(rows, AT)["blocked"])

    def test_actual_v30_buy_gate_observes_circuit_without_changing_exits(self):
        c={"price":1.0,"m1":3.0,"m15":2.0,"m4":5.0,
           "flow":500,"flow15":100,"vol":2000,"trades":25,
           "buy_ratio":3.0,"trade_ratio":2.0,
           "data_age_sec":0,"last_transaction":AT.isoformat()}
        previous={"m1":0,"m15":0,"flow":0,"vol":100,"price":.99,"trades":12}
        with (patch.object(v30, "score", return_value=(c, 90, 90, 12, "CONFIRMATION")),
              patch.object(v30, "entry_gate", return_value={
                  "buy":True,"entry_impulse":12,"liquidity_ok":True,
                  "price_flow_ok":True,"ratio":1.0}),
              patch.object(v30, "cooldown_active", return_value=False),
              patch.object(v30, "load_state", return_value={
                  "history":{"0xab":{"prev":previous}}}),
              patch.object(v30, "paper_macd_veto",
                           return_value={"blocked":False,"status":"UNAVAILABLE"})):
            with patch.object(v30,"paper_entry_risk_gate",
                              return_value={"blocked":False,"status":"ACTIVE"}):
                baseline=v30.decision("0xab",{},persist=False)
            with patch.object(v30,"paper_entry_risk_gate",
                              return_value={"blocked":True,"status":"LOSS_COOLDOWN",
                                            "window_closed":8,"window_pnl_sda":-24}):
                guarded=v30.decision("0xab",{},persist=False)
        self.assertTrue(baseline["eligible_for_buy"])
        self.assertFalse(guarded["eligible_for_buy"])
        self.assertIn("loss_circuit",guarded["paper_buy_gate_failures"])
        self.assertEqual(guarded["paper_loss_circuit"]["status"],"LOSS_COOLDOWN")
        self.assertEqual(v30.SL_PCT,0.025)


if __name__=="__main__":
    unittest.main()
