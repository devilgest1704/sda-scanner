"""Real PAPER temporal-recovery entry tests: no stale-score or risk bypass."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import v30_pump_hunter as v30
from tools.temporal_recovery_buy import evaluate, evaluate_continuation

T = datetime.now(timezone.utc).replace(microsecond=0)
ADDRESS = "0xab"


def prior(*, age=400, score=83, impulse=14):
    start = T-timedelta(seconds=90)
    return {
        "decision_at": start.isoformat(),
        "quote_at": (start-timedelta(seconds=age)).isoformat(),
        "data_age_sec": age,
        "score": score, "impulse": impulse, "phase": "CONFIRMATION",
    }


def current(*, age=15, score=64, quality=60, impulse=6, **overrides):
    t = {
        "phase":"CONFIRMATION", "score":score, "quality":quality,
        "impulse":impulse, "quote_at":(T-timedelta(seconds=age)).isoformat(),
        "data_age_sec":age, "price_sda":1.0,
        "m1h":9, "m15":2, "volume_1h":1500, "flow_1h":400,
        "trades_1h":18, "buy_ratio":2.0,
    }
    t.update(overrides)
    return t


class TemporalSignalTests(unittest.TestCase):
    def test_recovered_strong_stale_signal_with_new_price(self):
        result = evaluate(ADDRESS, current(), previous=prior(), at=T)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["reason"], "NEW_FRESH_QUOTE_CONFIRMED")

    def test_no_current_quote_or_old_stale_score_only(self):
        self.assertFalse(evaluate(ADDRESS, current(age=350), previous=prior(), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(score=95), previous=prior(age=200), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(), previous=prior(score=60), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(quality=30), previous=prior(), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(trades_1h=3), previous=prior(), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(quote_at=(T-timedelta(seconds=550)).isoformat(),
                                                  data_age_sec=550), previous=prior(), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(phase="WATCH"), previous=prior(), at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS, current(m1h=18), previous=prior(), at=T)["ready"])

    def test_unsafe_prior_or_future_quote_never_eligible(self):
        bad = prior()
        bad["decision_at"] = (T+timedelta(seconds=30)).isoformat()
        self.assertFalse(evaluate(ADDRESS,current(),previous=bad,at=T)["ready"])
        self.assertFalse(evaluate(ADDRESS,current(quote_at=(T+timedelta(seconds=1)).isoformat()),
                                  previous=prior(),at=T)["ready"])

    def test_absent_prior_fails_closed(self):
        self.assertFalse(evaluate(ADDRESS,current(),previous={},at=T)["ready"])


class ActualPaperGateTests(unittest.TestCase):
    def decision(self, temporal_ready, *, loss=False, macd=False, cooldown=False, score=64):
        c = {"price":1, "m1":9, "m15":2, "m4":5, "flow":400,
             "flow15":100, "vol":1500, "trades":18, "buy_ratio":2,
             "trade_ratio":2, "data_age_sec":15,
             "last_transaction":(T-timedelta(seconds=15)).isoformat()}
        with (
            patch.object(v30, "PAPER_BUY_MODE", "temporal_recovery"),
            patch.object(v30, "score", return_value=(c,score,60,6,"CONFIRMATION")),
            patch.object(v30, "entry_gate", return_value={
                "buy":False, "entry_impulse":6, "liquidity_ok":True,
                "price_flow_ok":True, "ratio":.5,
            }),
            patch.object(v30, "cooldown_active", return_value=cooldown),
            patch.object(v30, "load_state", return_value={
                "history":{ADDRESS:{"prev":{"m1":0, "m15":0,
                    "flow":0, "vol":100, "price":.8,"trades":5}}}}),
            patch.object(v30, "temporal_recovery_signal", return_value={
                "ready":temporal_ready, "reason":"NEW_FRESH_QUOTE_CONFIRMED"
                if temporal_ready else "missing_prior_frozen_signal"
            }),
            patch.object(v30, "paper_macd_veto", return_value={"blocked":macd,"status":"BEARISH_VETO" if macd else "UNAVAILABLE"}),
            patch.object(v30, "paper_entry_risk_gate", return_value={"blocked":loss,"status":"LOSS_COOLDOWN" if loss else "ACTIVE"}),
        ):
            return v30.decision(ADDRESS,{},persist=False)

    def test_temporal_ready_is_real_buy_even_at_score_64(self):
        r=self.decision(True)
        self.assertTrue(r["eligible_for_buy"])
        self.assertFalse(r["paper_buy_blocked"])
        self.assertEqual(r["confidence"],v30.ENTRY_SCORE)
        self.assertEqual(r["pump_score"],64)
        self.assertEqual(r["paper_buy_mode"],"temporal_recovery")
        self.assertEqual(r["paper_buy_gate_failures"],[])

    def test_old_baseline_signal_does_not_open(self):
        r=self.decision(False,score=95)
        self.assertFalse(r["eligible_for_buy"])
        self.assertEqual(r["confidence"],v30.ENTRY_SCORE-1)
        self.assertIn("temporal_recovery",r["paper_buy_gate_failures"])

    def test_temporal_signal_does_not_bypass_hard_risk(self):
        for kwargs,gate in [
            ({"loss":True},"loss_circuit"),
            ({"macd":True},"macd_bearish"),
            ({"cooldown":True},"cooldown"),
        ]:
            with self.subTest(kwargs=kwargs):
                r=self.decision(True,**kwargs)
                self.assertFalse(r["eligible_for_buy"])
                self.assertIn(gate,r["paper_buy_gate_failures"])

    def test_exit_constants_unchanged(self):
        self.assertEqual(v30.SL_PCT,.025)
        self.assertEqual(v30.MAX_BUYS_PER_RUN,1)
        self.assertEqual(v30.MAX_OPEN,4)


class ContinuationQuoteOrderingTests(unittest.TestCase):
    def test_reject_quote_observed_before_previous_decision(self):
        prior_at = T - timedelta(seconds=100)
        old_quote = prior_at - timedelta(seconds=40)
        previous = {
            "decision_at": prior_at.isoformat(),
            "quote_at": old_quote.isoformat(),
            "data_age_sec": 40, "price_sda": 1.0,
            "score": 75, "impulse": 8, "phase": "CONFIRMATION",
        }
        candidate = current(age=120, score=75, quality=70, impulse=8,
                            price_sda=1.02, m1h=5, m15=2,
                            volume_1h=1500, flow_1h=400, flow_15m=100,
                            trades_1h=18, buy_ratio=2)
        result = evaluate_continuation(ADDRESS, candidate, previous=previous, at=T)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "no_new_fresh_price_followthrough")

    def test_accept_quote_observed_after_previous_decision(self):
        prior_at = T - timedelta(seconds=100)
        previous = {
            "decision_at": prior_at.isoformat(),
            "quote_at": (prior_at - timedelta(seconds=40)).isoformat(),
            "data_age_sec": 40, "price_sda": 1.0,
            "score": 75, "impulse": 8, "phase": "CONFIRMATION",
        }
        candidate = current(age=20, score=75, quality=70, impulse=8,
                            price_sda=1.02, m1h=5, m15=2,
                            volume_1h=1500, flow_1h=400, flow_15m=100,
                            trades_1h=18, buy_ratio=2)
        self.assertTrue(evaluate_continuation(ADDRESS, candidate, previous=previous, at=T)["ready"])


if __name__ == "__main__":
    unittest.main()
