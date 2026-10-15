"""Forward-only signal timing and 5/15/60-minute missed-opportunity research.

All future outcomes must come from genuine swaps, with original entry inputs
frozen at decision time. Virtual research cannot modify Champion or real orders.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.candidate_price_tracker import update
from tools.missed_pump_learning import early_horizon_research, learn
from tools.forward_shadow_agent import default_state, step
from tools.strategy_evolution_agent import evolve, initial_state, BASELINE, experiment_policies

T0=datetime(2026,10,15,8,0,tzinfo=timezone.utc)
ADDR="0x"+"e"*40


def t(minutes):
    return (T0+timedelta(minutes=minutes)).isoformat()


def candidate():
    return {
        "at":t(0),"address":ADDR,"quote_at":t(-1),
        "price_sda":1.0,"phase":"IGNITION","eligible":False,
        "score":75,"quality":68,"impulse":0,"snapshot_impulse":12,
        "m1h":4,"m15":2.5,"m4h":9,
        "trades_1h":13,"buy_ratio":2.1,"volume_1h":1100,
        "flow_1h":550,"flow_15m":200,
        "gates":["loss_circuit","impulse"],
    }


def tx(minutes,price):
    return {"timestamp":t(minutes),"price":price,
            "type":"buy","volume":150.0,"hash":f"onchain-{minutes}"}


def market(minutes,price=None,history=None):
    info={"last_transaction":t(minutes),"price_in_sda":price}
    token={"analysis":info}
    if history is not None:
        token["transactions"]=history
    return {"updated_at":t(minutes),"tokens":{ADDR:token}}


class FastCausalOutcomeTests(unittest.TestCase):
    def test_first_genuine_swap_at_each_horizon_not_future_peak(self):
        origin=update({"funnels":[{"at":t(0),"candidate_snapshots":[candidate()]}]},
                      market(0,1.0))
        self.assertEqual(len(origin["observations"]),1)
        changes=[tx(7,1.05),tx(9,8.0),tx(17,1.08),
                 tx(20,7.0),tx(63,1.12),tx(68,20.0)]
        past=update({"observations":origin["observations"]},market(65,None),
                    history_market=market(65,None,changes))
        observed=past["observations"][0]["outcomes"]
        self.assertEqual(observed["5m"]["price_sda"],1.05)
        self.assertEqual(observed["15m"]["price_sda"],1.08)
        self.assertEqual(observed["60m"]["price_sda"],1.12)
        self.assertEqual(observed["5m"]["tx_hash"],"onchain-7")
        stats=early_horizon_research(past["observations"])["horizons"]
        for h in ("5m","15m","60m"):
            self.assertEqual(stats[h]["valid_distinct_cases"],1)
            self.assertEqual(stats[h]["model_positive_after_costs"],1)
        self.assertFalse(learn(past["observations"])["can_promote_champion"])

    def test_5m_late_swap_and_fabricated_peak_are_not_training_data(self):
        origin=update({"funnels":[{"at":t(0),"candidate_snapshots":[candidate()]}]},
                      market(0,1.0))
        rows=update({"observations":origin["observations"]},market(66,None),
                    history_market=market(66,None,[tx(11,100),tx(18,1.04),tx(62,.90)])
                    )["observations"]
        self.assertNotIn("5m",rows[0]["outcomes"])
        rows[0]["outcomes"]["5m"]={
            "observed_at":t(7),"price_sda":999,
            "source":"claimed_peak_not_real_swap",
        }
        stats=early_horizon_research(rows)["horizons"]
        self.assertEqual(stats["5m"]["valid_distinct_cases"],0)
        self.assertEqual(stats["15m"]["valid_distinct_cases"],1)
        self.assertEqual(stats["60m"]["model_positive_after_costs"],0)

    def test_future_or_misaligned_history_never_labels_early_exit(self):
        origin=update({"funnels":[{"at":t(0),"candidate_snapshots":[candidate()]}]},
                      market(0,1.0))
        for wrong in (market(11,None,[tx(12,2.0)]),
                      market(10,None,[tx(7,2.0)])):
            row=update({"observations":origin["observations"]},market(11,None),
                       history_market=wrong)["observations"][0]
            self.assertNotIn("5m",row["outcomes"])


class TemporalRecoveryResearchTests(unittest.TestCase):
    def snapshot(self,m,quote_offset,score,impulse,quality=68):
        return {"at":t(m),"candidate_snapshots":[{
            "at":t(m),"address":ADDR,"symbol":"SGWA-TEST",
            "quote_at":t(m+quote_offset),
            "phase":"IGNITION","score":score,"quality":quality,
            "impulse":impulse,"data_age_sec":-quote_offset*60,
            "m1h":3.0,"m15":1.5,"volume_1h":1800,
            "trades_1h":20,"flow_1h":900,"flow_15m":300,
            "buy_ratio":2.0,"price_sda":1.0,"gates":["loss_circuit"],
        }]}

    def market_at(self,m,offset):
        return {"updated_at":t(m),"tokens":{
            ADDR:{"analysis":{"price_in_sda":1.0,
                              "last_transaction":t(m+offset)}}}}

    def test_stale_strength_then_new_fresh_fade_is_shadow_only(self):
        with patch("tools.forward_shadow_agent.load_evolution",return_value={}):
            s=default_state()
            step(s,self.market_at(0,-6),self.snapshot(0,-6,88,17))
            self.assertFalse(s["strategies"]["research_temporal_recovery"]["positions"])
            # NEW observed quote at +1m30s: no stale entry price reused.
            d=step(s,self.market_at(2,-.5),self.snapshot(2,-.5,65,5))
            self.assertEqual(d["qualified"]["research_temporal_recovery"],1)
            self.assertEqual(len(s["strategies"]["research_temporal_recovery"]["positions"]),1)
            self.assertEqual(len(s["strategies"]["baseline"]["positions"]),0)
            temporal=s["last_diagnostic"]["temporal_recovery_research"]
            self.assertEqual(temporal["recovered_total"],1)
            self.assertTrue(temporal["recent_examples"][0]["quote_advanced"])
            self.assertTrue(temporal["paper_only"])
            self.assertFalse(temporal["changes_champion"])

    def test_no_quote_advance_or_stale_current_blocks_temporal_research(self):
        with patch("tools.forward_shadow_agent.load_evolution",return_value={}):
            s=default_state()
            step(s,self.market_at(0,-6),self.snapshot(0,-6,88,17))
            step(s,self.market_at(2,-8),self.snapshot(2,-8,65,5))
            self.assertEqual(s["last_diagnostic"]["temporal_recovery_research"]["recovered_total"],0)
            # Fake future quote must never be interpreted as confirmed buy.
            step(s,self.market_at(4,+1),self.snapshot(4,+1,65,5))
            self.assertEqual(s["last_diagnostic"]["temporal_recovery_research"]["recovered_total"],0)
            self.assertFalse(s["strategies"]["research_temporal_recovery"]["positions"])


class DiverseHypothesisTests(unittest.TestCase):
    def test_duplicate_impulse_research_is_retired_without_abandoning_open_position(self):
        s=initial_state()
        s["experiments"]=[
            {"id":"research_evo_first","status":"FORWARD_TEST",
             "hypothesis":"require_stronger_impulse",
             "policy":{**BASELINE,"impulse":17.05}},
            {"id":"research_evo_duplicate","status":"FORWARD_TEST",
             "hypothesis":"require_stronger_impulse",
             "policy":{**BASELINE,"impulse":17.025}},
        ]
        shadow={"strategies":{
            "research_evo_duplicate":{"positions":{ADDR:{"opened_at":t(0)}},
                                      "trades":[]}}}
        done=evolve(s,{"trades":[]},shadow,
                    now=T0,llm_reviewer=lambda *_:{"status":"NOT_CONFIGURED"})
        self.assertEqual(done["experiments"][0]["status"],"FORWARD_TEST")
        self.assertEqual(done["experiments"][1]["status"],"RETIRED")
        self.assertEqual(done["experiments"][1]["conclusion"],
                         "REDUNDANT_HYPOTHESIS_FAMILY")
        self.assertIn("research_evo_duplicate",experiment_policies(done))
        self.assertEqual(done["diagnosis"]["diversity_retirements"],1)
        self.assertTrue(done["control"]["paper_champion_unchanged"])


if __name__=="__main__":
    unittest.main()
