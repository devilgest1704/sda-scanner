import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.strategy_evolution_agent import (
    BASELINE, initial_state, evolve, proposals_from_evidence,
    experiment_policies, limited_llm_review, normalize_rows,
)
from tools.forward_shadow_agent import default_state, step, eligible

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def historical_trades(n=180):
    rows = []
    for i in range(n):
        winner = i % 5 in (0, 1)
        # Most loss entries bought a late hourly price surge. A third of
        # losers are early enough to survive a diagnosed entry cutoff.
        m1h = 3.0 if winner else (4.0 if i % 3 == 0 else 9.0)
        rows.append({
            "closed_at": (T0-timedelta(minutes=(n-i)*10)).isoformat(),
            "closed_profit_sda": 2.0 if winner else -3.0,
            "entry_metrics": {
                "v30": {"phase":"CONFIRMATION"}, "confidence": 88,
                "quality": 70, "impulse": 11, "buy_ratio":2.0,
                "trades_1h":20,"flow_1h":500,
                "m1h":m1h,"m15":2.2 if winner else 6.3,
            }
        })
    return {"trades": rows}


def market(t, price=1):
    return {"updated_at":t.isoformat(),"tokens":{
        "0xab":{"analysis":{"price_in_sda":price,"last_transaction":t.isoformat()}}}}


def funnel(t, m1h=3, price=1):
    return {"at":t.isoformat(),"candidate_snapshots":[{
        "address":"0xab","phase":"CONFIRMATION","score":90,"quality":80,
        "impulse":12,"trades_1h":20,"buy_ratio":2.0,
        "flow_1h":500,"volume_1h":2000,"m15":2.2,
        "m1h":m1h,"price_sda":price,"data_age_sec":0,
    }]}


class EvidenceEvolutionTests(unittest.TestCase):
    def test_reasons_are_derived_from_winning_vs_losing_entries(self):
        choices, diagnostics = proposals_from_evidence(normalize_rows(
            historical_trades()["trades"]))
        self.assertEqual(diagnostics["status"],"DIAGNOSED")
        self.assertTrue(choices,diagnostics)
        self.assertIn("avoid_late_hourly_chase",
                      [x["hypothesis"] for x in choices])
        for x in choices:
            self.assertGreater(x["train_avg_improvement_sda"],0)
            self.assertGreater(x["validation_avg_improvement_sda"],0)
            self.assertGreaterEqual(x["validation"]["coverage_pct"],35)

    def test_evolution_creates_bounded_immutable_research_hypotheses(self):
        result=evolve(initial_state(), historical_trades(), {"strategies":{}},
                      now=T0, llm_reviewer=lambda _a,_b:{"status":"NOT_CONFIGURED"})
        self.assertEqual(result["control"]["research_only"], True)
        self.assertFalse(result["control"]["runtime_parity_verified"])
        self.assertGreater(len(result["experiments"]),0)
        self.assertLessEqual(len(result["experiments"]),2)
        self.assertEqual(set(experiment_policies(result)),
                         {x["id"] for x in result["experiments"]})
        for x in result["experiments"]:
            self.assertFalse(x["can_promote_paper"])
            self.assertFalse(x["can_trade_real"])
            for k,v in BASELINE.items():
                self.assertGreaterEqual(x["policy"][k],v)

    def test_replayed_diagnosis_does_not_create_duplicate_experiments(self):
        first=evolve(initial_state(), historical_trades(), {"strategies":{}},
                     now=T0, llm_reviewer=lambda _a,_b:{"status":"NOT_CONFIGURED"})
        before=[x["id"] for x in first["experiments"]]
        second=evolve(first, historical_trades(), {"strategies":{}},
                      now=T0+timedelta(hours=1),
                      llm_reviewer=lambda _a,_b:{"status":"NOT_CONFIGURED"})
        self.assertEqual(before,[x["id"] for x in second["experiments"]])

    def test_failing_real_forward_quotes_retire_research_not_champion(self):
        result=evolve(initial_state(), historical_trades(), {"strategies":{}},
                      now=T0, llm_reviewer=lambda _a,_b:{"status":"NOT_CONFIGURED"})
        name=result["experiments"][0]["id"]
        shadow={"strategies":{name:{"trades":[
            {"valid_for_learning":True,"pnl_sda":-1.0}
            for _ in range(12)]}}}
        next_state=evolve(result, historical_trades(),shadow,
                          now=T0+timedelta(hours=3),
                          llm_reviewer=lambda _a,_b:{"status":"NOT_CONFIGURED"})
        e=next(x for x in next_state["experiments"] if x["id"]==name)
        self.assertEqual(e["status"],"RETIRED")
        self.assertEqual(e["forward_net_sda"],-12)
        self.assertIn(name,experiment_policies(next_state))
        self.assertTrue(next_state["control"]["paper_champion_unchanged"])

    def test_llm_unconfigured_makes_no_network_calls(self):
        with patch.dict("os.environ",{"STRATEGY_LLM_API_KEY":""}):
            with patch("urllib.request.urlopen",side_effect=AssertionError("no network")):
                self.assertEqual(limited_llm_review([{"hypothesis":"test"}],{})["status"],
                                 "NOT_CONFIGURED")

    def test_dynamic_arm_has_separate_virtual_position_and_retirement(self):
        name="research_evo_test"
        evolved={"schema":1,"paper_only":True,"experiments":[{
            "id":name,"status":"FORWARD_TEST","policy":{
                **BASELINE,"m1h_max":5.0}}]}
        with patch("tools.forward_shadow_agent.load_evolution",return_value=evolved):
            state=default_state()
            first=step(state,market(T0),funnel(T0))
            self.assertIn(name,state["strategies"])
            self.assertEqual(len(state["strategies"][name]["positions"]),1)
            self.assertGreater(first["opened"],0)
            # A late chased entry is not allowed as a second shadow entry.
            self.assertFalse(eligible(funnel(T0,m1h=7)["candidate_snapshots"][0],
                                      evolved["experiments"][0]["policy"],False,name))
            # Retirement disables entries but does NOT abandon open risk.
            evolved["experiments"][0]["status"]="RETIRED"
            t1=T0+timedelta(minutes=5)
            step(state,market(t1,.96),funnel(t1,price=.96))
            self.assertEqual(len(state["strategies"][name]["positions"]),0)
            self.assertEqual(len(state["strategies"][name]["trades"]),1)
            self.assertLess(state["strategies"][name]["trades"][0]["pnl_sda"],0)


if __name__=="__main__":
    unittest.main()
