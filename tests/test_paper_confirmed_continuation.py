"""Causal + risk regression tests for stronger two-quote PAPER BUY."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import v30_pump_hunter as v30
from tools.temporal_recovery_buy import evaluate, evaluate_continuation, evaluate_paper

NOW=datetime(2026,10,18,10,0,tzinfo=timezone.utc)
ADDRESS="0xabc"

def prior(**changes):
    at=NOW-timedelta(seconds=90)
    row={"decision_at":at.isoformat(),
         "quote_at":(at-timedelta(seconds=80)).isoformat(),
         "data_age_sec":80,
         "price_sda":.98,"score":80,"impulse":8,"phase":"CONFIRMATION"}
    row.update(changes)
    return row

def candidate(**changes):
    row={"phase":"CONFIRMATION","score":76,"quality":67,"impulse":9,
         "quote_at":(NOW-timedelta(seconds=15)).isoformat(),
         "data_age_sec":15,"price_sda":1,
         "m1h":7,"m15":2.5,"volume_1h":1200,"flow_1h":400,
         "flow_15m":100,"trades_1h":9,"buy_ratio":2}
    row.update(changes)
    return row


class TwoQuoteSignalTests(unittest.TestCase):
    def test_qualifies_new_high_conviction_lane_not_original_stale_recovery(self):
        self.assertFalse(evaluate(ADDRESS,candidate(),at=NOW,previous=prior())["ready"])
        result=evaluate_paper(ADDRESS,candidate(),at=NOW,previous=prior())
        self.assertTrue(result["ready"],result)
        self.assertEqual(result["lane"],"confirmed_continuation")
        self.assertGreater(result["price_advance_pct"],0)

    def test_unverified_prior_is_never_an_entry(self):
        for row in (prior(data_age_sec=400),
                    prior(price_sda=None),
                    prior(score=60),prior(impulse=0),
                    prior(phase="NO"),{},
                    prior(quote_at=NOW.isoformat())):
            with self.subTest(prior=row):
                self.assertFalse(evaluate_paper(
                    ADDRESS,candidate(),at=NOW,previous=row)["ready"])

    def test_quote_must_advance_but_not_spike(self):
        for kw in ({"price_sda":.98},{"price_sda":.97},{"price_sda":1.05},
                   {"data_age_sec":195,
                    "quote_at":(NOW-timedelta(seconds=195)).isoformat()},
                   {"quote_at":(NOW+timedelta(seconds=2)).isoformat()},
                   {"quote_at":prior()["quote_at"]}):
            with self.subTest(current=kw):
                self.assertFalse(evaluate_continuation(
                    ADDRESS,candidate(**kw),at=NOW,previous=prior())["ready"])

    def test_quality_flow_volume_activity_and_anti_chase(self):
        for kw in ({"trades_1h":7},{"volume_1h":800},{"flow_1h":250},
                   {"flow_15m":50},{"buy_ratio":1.3},{"m15":.1},
                   {"m1h":11},{"score":65},{"quality":55},{"impulse":3}):
            with self.subTest(current=kw):
                self.assertFalse(evaluate_continuation(
                    ADDRESS,candidate(**kw),at=NOW,previous=prior())["ready"])

    def test_12_trades_need_standard_not_thin_activity_strength(self):
        row=candidate(trades_1h=12,volume_1h=800,
                      flow_1h=250,flow_15m=70,buy_ratio=1.5)
        self.assertTrue(evaluate_continuation(
            ADDRESS,row,at=NOW,previous=prior())["ready"])


class ActivePaperEntryTests(unittest.TestCase):
    def decide(self, *, lane="confirmed_continuation", trades=9,
               loss=False,macd=False,cooldown=False):
        raw={"price":1,"m1":7,"m15":2.5,"m4":5,"flow":400,
             "flow15":100,"vol":1200,"trades":trades,"buy_ratio":2,
             "trade_ratio":2,"data_age_sec":15,
             "last_transaction":(NOW-timedelta(seconds=15)).isoformat()}
        result={"ready":True,"lane":lane,"reason":"TWO_FRESH_QUOTES_CONFIRMED"}
        with (
            patch.object(v30,"PAPER_BUY_MODE","temporal_recovery"),
            patch.object(v30,"score",return_value=(raw,76,67,9,"CONFIRMATION")),
            patch.object(v30,"entry_gate",return_value={
                "buy":False,"entry_impulse":9,"liquidity_ok":True,
                "price_flow_ok":True,"ratio":.5}),
            patch.object(v30,"cooldown_active",return_value=cooldown),
            patch.object(v30,"load_state",return_value={"history":{ADDRESS:{
                "prev":{"m1":1,"m15":1,"flow":300,"vol":700,"price":.98,
                        "trades":9}}}}),
            patch.object(v30,"temporal_recovery_signal",return_value=result),
            patch.object(v30,"paper_macd_veto",return_value={
                "blocked":macd,"status":"BEARISH_VETO" if macd else "UNAVAILABLE"}),
            patch.object(v30,"paper_entry_risk_gate",return_value={
                "blocked":loss,"status":"LOSS_COOLDOWN" if loss else "ACTIVE"})
        ):
            return v30.decision(ADDRESS,{},persist=False)

    def test_only_confirmed_continuation_allows_8_to_11_trade_exception(self):
        for trades in (8,9,11):
            with self.subTest(trades=trades):
                r=self.decide(trades=trades)
                self.assertTrue(r["eligible_for_buy"],r)
                self.assertEqual(r["confidence"],76)
                self.assertEqual(r["paper_temporal_recovery"]["lane"],
                                 "confirmed_continuation")
        r=self.decide(lane="temporal_recovery",trades=9)
        self.assertFalse(r["eligible_for_buy"])
        self.assertIn("v31_activity",r["paper_buy_gate_failures"])

    def test_protective_circuit_macd_cooldown_not_bypassed(self):
        for kw,reason in (({"loss":True},"loss_circuit"),
                          ({"macd":True},"macd_bearish"),
                          ({"cooldown":True},"cooldown")):
            with self.subTest(condition=kw):
                r=self.decide(**kw)
                self.assertFalse(r["eligible_for_buy"])
                self.assertIn(reason,r["paper_buy_gate_failures"])

    def test_absolute_limits_and_stop_not_relaxed(self):
        self.assertEqual(v30.SL_PCT,.025)
        self.assertEqual(v30.MAX_BUYS_PER_RUN,1)
        self.assertEqual(v30.MAX_OPEN,4)


if __name__=="__main__":
    unittest.main()
