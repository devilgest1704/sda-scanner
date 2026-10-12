"""Independent regression fixtures for observed V30 exit-reason parity."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import v30_pump_hunter as v30
from tools.v30_exit_parity import compare_closed_trade, audit_archive


START = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


def closed(prices, *, weak=None, actual="V30.3 TRAILING/STOP", gap_minutes=2):
    """Construct chronological observed samples, never hypothetical future bars."""
    weak = weak or [False] * len(prices)
    sample = []
    for i, (price, isweak) in enumerate(zip(prices, weak)):
        now = START + timedelta(minutes=1+i*gap_minutes)
        quote_at = now - timedelta(seconds=30)
        sample.append({
            "at": now.isoformat(), "quote_at": quote_at.isoformat(),
            "quote_age_seconds": 30, "price_sda": price,
            "score": 30 if isweak else 75,
            "m15": -1 if isweak else 1, "flow_1h": -1 if isweak else 250,
        })
    return {
        "status": "CLOSED", "v30_mode": "CLEAN-PUMP-HUNTER-V30.3",
        "opened_at": START.isoformat(),
        "closed_at": (now + timedelta(seconds=1)).isoformat(),
        "entry_price": 1.0, "close_price": prices[-1],
        "close_reason": actual, "pump_price_samples": sample,
    }


class ExitParityAuditTests(unittest.TestCase):
    def test_emergency_priority_over_trailing(self):
        t = closed([0.98, 0.96], actual="V30.3 EMERGENCY/HARD STOP")
        self.assertEqual(compare_closed_trade(t)["result"], "match")

    def test_adaptive_peak_trail_after_four_percent(self):
        t = closed([1.05, 1.01])
        self.assertEqual(compare_closed_trade(t)["result"], "match")

    def test_weakness_needs_two_fresh_signals(self):
        t = closed([0.98, 0.979], weak=[True, True],
                   actual="V30.3 EARLY WEAKNESS")
        self.assertEqual(compare_closed_trade(t)["result"], "match")

    def test_pump_breakdown_only_after_three_weak_scans(self):
        t = closed([1.03, 1.03, 1.03], weak=[True]*3,
                   actual="V30.3 PUMP BREAKDOWN")
        self.assertEqual(compare_closed_trade(t)["result"], "match")

    def test_earlier_trigger_is_a_real_mismatch(self):
        t = closed([0.96, 0.95], actual="V30.3 EMERGENCY/HARD STOP")
        x = compare_closed_trade(t)
        self.assertEqual(x["result"], "mismatch")
        self.assertEqual(x["reason"], "predicted_earlier_exit")

    def test_missing_or_stale_quote_cannot_be_called_match(self):
        t = closed([0.96], actual="V30.3 EMERGENCY/HARD STOP")
        del t["pump_price_samples"][0]["quote_at"]
        self.assertEqual(compare_closed_trade(t)["result"], "excluded")
        t = closed([0.96], actual="V30.3 EMERGENCY/HARD STOP")
        t["pump_price_samples"][0]["quote_age_seconds"] = 2000
        self.assertEqual(compare_closed_trade(t)["result"], "excluded")

    def test_missing_signal_and_time_gaps_exclude_not_match(self):
        t = closed([.98,.96], actual="V30.3 EMERGENCY/HARD STOP")
        del t["pump_price_samples"][0]["score"]
        self.assertEqual(compare_closed_trade(t)["result"], "excluded")
        t = closed([.98,.96], actual="V30.3 EMERGENCY/HARD STOP", gap_minutes=25)
        self.assertEqual(compare_closed_trade(t)["result"], "excluded")

    def test_excluded_data_not_counted_in_parity_rate(self):
        good = closed([.96], actual="V30.3 EMERGENCY/HARD STOP")
        missing = copy.deepcopy(good)
        missing["pump_price_samples"][0].pop("quote_at")
        mismatch = copy.deepcopy(good)
        mismatch["close_reason"] = "V30.3 PUMP BREAKDOWN"
        result = audit_archive([good, missing, mismatch, {}])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["mismatched"], 1)
        self.assertEqual(result["excluded"], 2)
        self.assertEqual(result["comparable_match_pct"], 50.0)
        self.assertFalse(result["can_unlock_paper_autopromotion"])

    def test_mirror_against_actual_v30_exit_runtime_fixture(self):
        """Run real V30 exit function, then replay what it recorded."""
        start = datetime.now(timezone.utc) - timedelta(minutes=2)
        def fake_create(address, analysis, signal, meta, liq, investment=None):
            return {"address":address,"label":"TEST/SDA","opened_at":start.isoformat(),
                    "entry_price":100.0,"investment_sda":50.0,
                    "remaining_fraction":1.0}
        def fake_close(book, address, price, reason):
            pos = book["positions"].pop(address)
            r = {**pos, "status":"CLOSED", "close_reason":reason,
                 "close_price":price, "closed_at":datetime.now(timezone.utc).isoformat()}
            book["closed_trades"].append(r)
            return r
        decision = {"pump_score":75,"pump_quality":65,"pump_change":10,
                    "pump_phase":"IGNITION", "m1h":2,"m15":1,
                    "m4h":1,"net_1h":250,"volume_1h":800,
                    "trades_1h":15, "data_age_sec":30}
        engine = SimpleNamespace(create=fake_create, close=fake_close, main=lambda: None)
        main = SimpleNamespace()
        with patch.object(v30,"decision",return_value=decision), \
             patch.object(v30,"load_state",return_value={"cooldowns":{}}), \
             patch.object(v30,"save_state"):
            v30.patch(main,engine)
            pos=engine.create("0xtest",{"price_in_sda":100},decision,{}, {})
            book={"positions":{"0xtest":pos},"closed_trades":[]}
            quote=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
            engine._auto_exit(book,{"0xtest":{"analysis":{
                "price_in_sda":96.0, "last_transaction":quote}}},{})
        self.assertEqual(len(book["closed_trades"]),1)
        result=compare_closed_trade(book["closed_trades"][0])
        self.assertEqual(result["result"],"match",result)
        self.assertEqual(result["actual"],"V30.3 EMERGENCY/HARD STOP")


if __name__ == "__main__":
    unittest.main()
