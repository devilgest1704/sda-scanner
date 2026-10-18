"""Real PAPER temporal-recovery entry tests: no stale-score or risk bypass."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import v30_pump_hunter as v30
from tools.temporal_recovery_buy import evaluate, evaluate_fresh_confirmation

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




def fresh_signal(**changes):
    row={
        "phase":"IGNITION","score":78,"quality":65,
        "impulse":9,"delta_impulse":9,"fresh_transition":True,
        "quote_at":(T-timedelta(seconds=15)).isoformat(),
        "data_age_sec":15,"price_sda":1.0,
        "m1h":5,"m15":2.3,"m4h":3,
        "volume_1h":800,"flow_1h":300,"flow_15m":100,
        "trades_1h":12,"buy_ratio":1.6,
    }
    row.update(changes)
    return row


class FreshConfirmationSafetyTests(unittest.TestCase):
    def test_high_quality_fresh_signal_without_old_stale_signal(self):
        self.assertTrue(evaluate_fresh_confirmation(fresh_signal(),at=T)["ready"])

    def test_rejects_old_quote_nonmoving_market_and_missing_transaction(self):
        for row in (
            fresh_signal(data_age_sec=301),
            fresh_signal(quote_at=(T+timedelta(seconds=1)).isoformat()),
            fresh_signal(quote_at=""),
            fresh_signal(fresh_transition=False),
            fresh_signal(delta_impulse=0),
            fresh_signal(price_sda=0),
            fresh_signal(m1h=15),
            fresh_signal(m15=12),
            fresh_signal(m4h=-3),
            fresh_signal(phase="WATCH"),
        ):
            with self.subTest(row=row):
                self.assertFalse(evaluate_fresh_confirmation(row,at=T)["ready"])

    def test_rejects_weak_low_volume_or_few_swaps(self):
        for row in (
            fresh_signal(score=60),fresh_signal(quality=48),
            fresh_signal(impulse=2),fresh_signal(volume_1h=220),
            fresh_signal(flow_1h=20),fresh_signal(flow_15m=2),
            fresh_signal(buy_ratio=1.05),fresh_signal(trades_1h=4),
            fresh_signal(trades_1h=9),  # 8-9 swaps require stronger confirmation
        ):
            with self.subTest(row=row):
                self.assertFalse(evaluate_fresh_confirmation(row,at=T)["ready"])

    def test_tightly_bounded_early_eight_nine_swaps(self):
        row=fresh_signal(
            trades_1h=9,score=85,quality=73,delta_impulse=10,
            volume_1h=1100,flow_1h=350,flow_15m=125,buy_ratio=1.8,
        )
        self.assertTrue(evaluate_fresh_confirmation(row,at=T)["ready"])
        self.assertFalse(evaluate_fresh_confirmation(
            {**row,"volume_1h":650},at=T)["ready"])


class LivePaperFreshLaneTests(unittest.TestCase):
    def run_decision(self, mode, *, loss=False,macd=False,cooldown=False,
                     age=15,trades=12,score=78,quality=65,flow=300,
                     volume=800,delta=9,ratio=1.6,flow15=100):
        c={
            "price":1,"m1":5,"m15":2.3,"m4":3,
            "flow":flow,"flow15":flow15,
            "vol":volume,"trades":trades,"buy_ratio":ratio,
            "trade_ratio":2,"data_age_sec":age,
            "last_transaction":(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(),
        }
        with (
            patch.object(v30,"PAPER_BUY_MODE",mode),
            patch.object(v30,"score",return_value=(c,score,quality,delta,"IGNITION")),
            patch.object(v30,"entry_gate",return_value={
                "buy":False,"entry_impulse":delta,
                "liquidity_ok":True,"price_flow_ok":True,"ratio":.375,
            }),
            patch.object(v30,"cooldown_active",return_value=cooldown),
            patch.object(v30,"load_state",return_value={
                "history":{ADDRESS:{"prev":{"m1":0,"m15":0,
                    "flow":0,"vol":100,"price":.8,"trades":5}}}
            }),
            patch.object(v30,"temporal_recovery_signal",return_value={
                "ready":False,"reason":"missing_prior_frozen_signal"
            }),
            patch.object(v30,"paper_macd_veto",return_value={
                "blocked":macd,"status":"BEARISH_VETO" if macd else "UNAVAILABLE"
            }),
            patch.object(v30,"paper_entry_risk_gate",return_value={
                "blocked":loss,"status":"LOSS_COOLDOWN" if loss else "ACTIVE"
            }),
        ):
            return v30.decision(ADDRESS,{},persist=False)

    def test_adding_lane_genuinely_expands_buy_eligibility(self):
        before=self.run_decision("temporal_recovery")
        after=self.run_decision("temporal_recovery_plus")
        self.assertFalse(before["eligible_for_buy"])
        self.assertTrue(after["eligible_for_buy"])
        self.assertEqual(after["paper_buy_lane"],"fresh_momentum_confirmation")
        self.assertEqual(after["confidence"],78)
        self.assertTrue(after["paper_fresh_confirmation"]["ready"])
        self.assertNotIn("temporal_recovery",after["paper_buy_gate_failures"])

    def test_10_swaps_with_strong_flow_meet_v31_early_exception(self):
        result=self.run_decision("temporal_recovery_plus",trades=10)
        self.assertTrue(result["eligible_for_buy"])
        self.assertTrue(result["v31_early_exception_passed"])

    def test_risk_still_vetoes_fresh_confirmation(self):
        for opts,gate in (
            ({"loss":True},"loss_circuit"),
            ({"macd":True},"macd_bearish"),
            ({"cooldown":True},"cooldown"),
        ):
            with self.subTest(opts=opts):
                result=self.run_decision("temporal_recovery_plus",**opts)
                self.assertFalse(result["eligible_for_buy"])
                self.assertIn(gate,result["paper_buy_gate_failures"])

    def test_invalid_market_data_is_never_promoted(self):
        for opts in (
            {"age":350},{"trades":2},
            {"score":60},{"quality":45},
            {"flow":30},{"volume":200},
            {"delta":0},{"ratio":1.05},
        ):
            with self.subTest(opts=opts):
                result=self.run_decision("temporal_recovery_plus",**opts)
                self.assertFalse(result["eligible_for_buy"])

    def test_legacy_exits_and_order_capacity_unchanged(self):
        self.assertEqual(v30.SL_PCT,.025)
        self.assertEqual(v30.MAX_OPEN,4)
        self.assertEqual(v30.MAX_BUYS_PER_RUN,1)


if __name__ == "__main__":
    unittest.main()
