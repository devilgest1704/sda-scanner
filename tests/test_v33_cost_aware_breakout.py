"""Regression coverage: V33 fee-stressed breakout never changes PAPER buys."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.v32_breakout import analyze_breakout
from tools.v33_cost_aware_breakout import (
    SIDE_FRICTION, TARGET_GROSS, cost_aware_breakout_signal, modeled_net_sda,
)
from tools.forward_shadow_agent import (
    COST_BREAKOUT_V33_MODE, POLICIES, default_state, eligible, step,
)

T = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ADDR = "0xabcdef"


def swaps():
    rows = []
    for i in range(15):
        t = T - timedelta(minutes=145 - 8*i)
        rows.append({"timestamp":t.isoformat(),
                     "price":1.0 + (i % 4) * .01,
                     "volume":24.0,
                     "type":"buy" if i % 2 else "sell"})
    for i in range(7):
        # Explicit -13,-11,-9,-7,-5,-3,-1 minutes; never use future swaps.
        t = T - timedelta(minutes=13 - i*2)
        rows.append({"timestamp":t.isoformat(),
                     "price":1.033+i*.004,
                     "volume":70.0,
                     "type":"sell" if i == 0 else "buy"})
    return rows


def candidate(**updates):
    row = {"address":ADDR, "phase":"WATCH", "score":65, "quality":60,
           "impulse":0, "snapshot_impulse":9, "trades_1h":14,
           "buy_ratio":2.0, "flow_1h":400, "volume_1h":900,
           "m15":2, "m1h":4, "price_sda":1.057, "data_age_sec":60}
    row.update(updates)
    return row


def market(when=T, price=1.057, breakout=True):
    analysis={"price_in_sda":price,
              "last_transaction":(when-timedelta(minutes=1)).isoformat()}
    if breakout:
        analysis["technical"]={"breakout_volume_trend":analyze_breakout(swaps(),T)}
    return {"updated_at":when.isoformat(),"tokens":{ADDR:{"analysis":analysis}}}


def funnel(when=T, row=None):
    return {"at":when.isoformat(),
            "candidate_snapshots":[] if row is None else [row]}


class V33CostAwareTests(unittest.TestCase):
    def test_scenario_is_causal_and_cost_stressed(self):
        breakout = analyze_breakout(swaps(), T)
        self.assertTrue(breakout["eligible"], breakout)
        row=candidate(v32_breakout=breakout)
        signal=cost_aware_breakout_signal(row,breakout,POLICIES[COST_BREAKOUT_V33_MODE])
        self.assertTrue(signal["eligible"],signal)
        self.assertFalse(signal["can_promote_champion"])
        self.assertFalse(signal["expected_return_estimated"])
        self.assertTrue(signal["turnover_not_liquidity"])
        self.assertEqual(SIDE_FRICTION,0.015)
        self.assertGreater(signal["modeled_net_take_sda"],0)
        self.assertLess(signal["modeled_net_stop_sda"],0)
        self.assertLess(signal["break_even_win_rate_pct"],45)
        self.assertLess(modeled_net_sda(TARGET_GROSS),
                        50*TARGET_GROSS)
        self.assertTrue(eligible({**candidate(),"v33_cost_breakout":signal},
                                 POLICIES[COST_BREAKOUT_V33_MODE],
                                 macd_mode=COST_BREAKOUT_V33_MODE))
        self.assertFalse(eligible(candidate(),POLICIES[COST_BREAKOUT_V33_MODE],
                                  macd_mode=COST_BREAKOUT_V33_MODE))

    def test_late_breakout_or_weak_turnover_rejects(self):
        base=analyze_breakout(swaps(),T)
        row=candidate(v32_breakout=base)
        late=copy.deepcopy(base)
        late["breakout_pct"]=7.5
        self.assertIn("too_late_or_no_early_breakout",
                      cost_aware_breakout_signal(row,late,POLICIES[COST_BREAKOUT_V33_MODE])["rejection_reasons"])
        volume=copy.deepcopy(base)
        volume["recent_15m_volume_sda"]=100
        self.assertIn("insufficient_observed_turnover",
                      cost_aware_breakout_signal(row,volume,POLICIES[COST_BREAKOUT_V33_MODE])["rejection_reasons"])
        invalid=copy.deepcopy(base)
        invalid["volume_multiple"]=float("nan")
        self.assertFalse(cost_aware_breakout_signal(row,invalid,POLICIES[COST_BREAKOUT_V33_MODE])["eligible"])

    def test_no_lookahead_future_transaction_not_used(self):
        before=analyze_breakout(swaps(),T)
        later=swaps()+[{"timestamp":(T+timedelta(seconds=1)).isoformat(),
                        "price":100,"volume":100000,"type":"buy"}]
        self.assertEqual(before,analyze_breakout(later,T))

    def test_watch_research_opens_without_champion_and_net_exit(self):
        state=default_state()
        with patch("tools.forward_shadow_agent.load_evolution",return_value={}):
            first=step(state,market(),funnel(row=candidate()))
            self.assertEqual(first["status"],"processed")
            self.assertEqual(len(state["strategies"]["baseline"]["positions"]),0)
            positions=state["strategies"][COST_BREAKOUT_V33_MODE]["positions"]
            self.assertEqual(len(positions),1)
            self.assertTrue(positions[ADDR]["v33_cost_breakout_at_entry"]["eligible"])
            diagnostic=state["last_diagnostic"]["v33_cost_breakout_research"]
            self.assertEqual(diagnostic["eligible_after_shadow_gates"],1)
            self.assertTrue(diagnostic["paper_champion_unchanged"])
            self.assertEqual(len(state["strategies"][COST_BREAKOUT_V33_MODE]["trades"]),0)

            # A subsequent and actually newer observed quote is necessary.
            future=T+timedelta(minutes=10)
            quote=market(future,price=1.057*1.11,breakout=False)
            result=step(state,quote,funnel(future))
            self.assertGreaterEqual(result["closed"],1)
            records=state["strategies"][COST_BREAKOUT_V33_MODE]["trades"]
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]["reason"],"TAKE")
            self.assertTrue(records[0]["valid_for_learning"])
            self.assertAlmostEqual(records[0]["pnl_sda"],round(modeled_net_sda(.11),6))
            self.assertEqual(records[0]["v33_cost_breakout_at_entry"]["mode"],"RESEARCH_ONLY")
            self.assertEqual(len(state["strategies"]["baseline"]["trades"]),0)

    def test_stale_market_does_not_trigger_entry(self):
        state=default_state()
        with patch("tools.forward_shadow_agent.load_evolution",return_value={}):
            older=market(T-timedelta(minutes=11))
            older["tokens"][ADDR]["analysis"]["technical"]={
                "breakout_volume_trend":analyze_breakout(swaps(),T)}
            older["updated_at"]=T.isoformat()
            step(state,older,funnel(row=candidate()))
            self.assertEqual(len(state["strategies"][COST_BREAKOUT_V33_MODE]["positions"]),0)


if __name__=="__main__":
    unittest.main()
