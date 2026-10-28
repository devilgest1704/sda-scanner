import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.execution_quote_audit import (
    ROUTER, WSDA, audit, quote_provenance, rpc, sell_calldata, word,
)
from tools.tp4_trial_review import POLICY, review, trade_errors

NOW = datetime(2026, 10, 28, 20, tzinfo=timezone.utc)
TOKEN = "0x"+"1"*40
OWNER = "0x"+"2"*40


def trade(index=0, token=TOKEN, pnl_price=1.05):
    start = NOW-timedelta(hours=2, minutes=index)
    end = start+timedelta(minutes=5)
    return {"address": token, "opened_at": start.isoformat(), "closed_at": end.isoformat(),
            "exit_observed_at": end.isoformat(), "entry_price_sda": 1,
            "exit_price_sda": pnl_price, "reason": "TAKE", "notional_sda": 50,
            "pnl_sda": round(50*(pnl_price*.989/1.011-1), 6),
            "max_quote_gap_minutes": 5, "valid_for_learning": True,
            "tp4_entry_evidence": {"at": start.isoformat(), "quote_at": start.isoformat(),
                                    "research_temporal_recovery_ready": True}}


def state(trades=None, positions=None):
    return {"updated_at": NOW.isoformat(), "strategies": {POLICY: {
        "trades": trades or [], "positions": positions or {}}}}


class TrialReviewTests(unittest.TestCase):
    def test_waits_for_first_trade_then_reports_fixed_milestones(self):
        self.assertEqual(review(state(), NOW)["status"], "WAITING_FOR_FIRST_TRADE")
        for count, status in ((1, "COLLECTING"), (15, "PRELIMINARY_REVIEW_15"),
                              (30, "BREADTH_REVIEW_30"), (50, "DECISION_REVIEW_50")):
            rows = [trade(i, "0x"+f"{i%6+1:040x}") for i in range(count)]
            r = review(state(rows), NOW)
            self.assertEqual(r["status"], status)
            self.assertFalse(r["can_promote"])
            self.assertEqual(r["research_review_ready"], count >= 50)

    def test_single_token_profit_and_bad_costs_block_review(self):
        rows = [trade(i) for i in range(50)]
        r = review(state(rows), NOW)
        self.assertIn("profit_concentrated_in_one_token", r["research_quality_blockers"])
        rows[0]["pnl_sda"] += 10
        r = review(state(rows), NOW)
        self.assertEqual(r["status"], "JOURNAL_INTEGRITY_ERROR")
        self.assertIn("cost_accounting_mismatch", r["journal_errors"][0]["errors"])

    def test_invalid_closes_cannot_create_positive_evidence(self):
        row = trade(); row.update(valid_for_learning=False, pnl_sda=999)
        r = review(state([row]), NOW)
        self.assertEqual(r["valid_closed"], 0)
        self.assertEqual(r["conservative_closed_risk_sda"], -50)

    def test_bad_threshold_gap_and_missing_confirmation_are_detected(self):
        row = trade(pnl_price=1.01)
        row["max_quote_gap_minutes"] = 40
        row["tp4_entry_evidence"] = {}
        errors = trade_errors(row)
        self.assertIn("take_below_threshold", errors)
        self.assertIn("holding_gap_in_positive_learning", errors)
        self.assertIn("missing_frozen_temporal_confirmation", errors)

    def test_duplicates_and_stale_audit_block_inference(self):
        row = trade()
        r = review(state([row, copy.deepcopy(row)]), NOW,
                   {"updated_at": (NOW-timedelta(hours=1)).isoformat()})
        self.assertEqual(r["status"], "JOURNAL_INTEGRITY_ERROR")
        self.assertFalse(r["execution_audit_fresh"])


class SellAuditTests(unittest.TestCase):
    def discovery(self):
        # A genuine token-bound BUY template, not a copied output quote.
        raw = "0x414bf389"+"".join(word(x) for x in (
            int(WSDA, 16), int(TOKEN, 16), 3000, int(OWNER, 16), 1, 50*10**18, 0, 0))
        return {"v25_router_transactions": [{"raw_input": raw, "from": OWNER, "tx": "0xtx"}]}

    def test_no_positions_and_stale_source_do_not_make_rpc_calls(self):
        def forbidden(*args):
            self.fail("Unnecessary RPC")
        self.assertEqual(audit(state(), {}, {}, {}, NOW, forbidden)["status"], "NO_OPEN_TEMPORAL_POSITIONS")
        s = state(); s["updated_at"] = (NOW-timedelta(hours=1)).isoformat()
        self.assertEqual(audit(s, {}, {}, {}, NOW, forbidden)["status"], "STALE_SHADOW_SOURCE")

    def test_unbound_inherited_quotes_never_validate_sellability(self):
        report = quote_provenance({"quotes_50_sda": {TOKEN: {
            "source": "fallback_fee", "quote_status": "VALIDATED_READ_ONLY", "amount_out_raw": 999}}})
        self.assertEqual(report["issues"]["unbound_fallback_template"], 1)
        self.assertEqual(report["issues"]["missing_or_mismatched_token_binding"], 1)
        self.assertFalse(report["sellability_verified"])

    def test_full_size_simulation_uses_reverse_route_and_one_pinned_block(self):
        calls = []
        def call(method, params):
            calls.append((method, params))
            if method == "eth_blockNumber": return "0x123"
            if method == "eth_getBlockByNumber": return {"timestamp": hex(int(NOW.timestamp()))}
            self.assertEqual(method, "eth_call")
            self.assertEqual(params[1], "0x123")
            data = params[0]["data"]
            if data == "0x313ce567": return hex(18)
            if data.startswith(("0x70a08231", "0xdd62ed3e")): return hex(1000*10**18)
            words = [int(data[i:i+64], 16) for i in range(10, len(data), 64)]
            self.assertEqual(len(words), 8)
            self.assertEqual(words[:2], [int(TOKEN, 16), int(WSDA, 16)])
            self.assertEqual(params[0]["to"], ROUTER)
            self.assertEqual(params[0]["value"], "0x0")
            self.assertAlmostEqual(words[5]/10**18, 50/1.011)
            return "0x"+word(48*10**18)
        s = state(positions={TOKEN: {"entry_price_sda": 1, "opened_at": NOW.isoformat()}})
        result = audit(s, {}, self.discovery(), {}, NOW, call)
        row = result["positions"][0]
        self.assertEqual(row["status"], "SIMULATED_SELL_NOT_FILL")
        self.assertEqual(row["amount_out_sda"], 48)
        self.assertFalse(row["executable_fill"])
        self.assertFalse(result["can_promote"])

    def test_missing_holder_allowance_remains_unknown_not_zero_liquidity(self):
        def call(method, params):
            if method == "eth_blockNumber": return "0x123"
            if method == "eth_getBlockByNumber": return {"timestamp": hex(int(NOW.timestamp()))}
            return hex(18) if params[0]["data"] == "0x313ce567" else "0x0"
        s = state(positions={TOKEN: {"entry_price_sda": 1}})
        row = audit(s, {}, self.discovery(), {}, NOW, call)["positions"][0]
        self.assertEqual(row["status"], "HOLDER_BALANCE_OR_ALLOWANCE_UNAVAILABLE")
        self.assertNotIn("amount_out_sda", row)

    def test_rpc_cannot_broadcast_or_access_signing_accounts(self):
        for method in ("eth_sendRawTransaction", "eth_sendTransaction", "personal_sign", "eth_accounts"):
            with self.assertRaises(ValueError):
                rpc(method, [])


if __name__ == "__main__":
    unittest.main()
