"""Autonomous missed-pump BUY learner: causal price labels and immutable Shadow."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.candidate_price_tracker import update
from tools.missed_pump_learning import (
    learn, normalize, safe_missed_entry, net_sda,
)
from tools.strategy_evolution_agent import (
    evolve, initial_state, experiment_policies, BASELINE,
)
from tools.forward_shadow_agent import default_state, step

START=datetime(2026, 10, 1, tzinfo=timezone.utc)
TOKEN="0xfirst"


def frozen_candidate(time, address=TOKEN, **changes):
    f={
        "at":time.isoformat(),"address":address,
        "quote_at":(time-timedelta(minutes=1)).isoformat(),
        "phase":"WATCH","score":76,"quality":70,
        "snapshot_impulse":12,"impulse":0,"delta_impulse":0,
        "data_age_sec":60,"trades_1h":8,"buy_ratio":2.2,
        "volume_1h":1500,"flow_1h":800,"flow_15m":500,
        "m15":2.5,"m1h":4,"m4h":12,"price_sda":1.0,
        "eligible":False,"gates":["lifecycle","impulse"],
    }
    f.update(changes)
    return f


def research_row(i, good=True):
    t=START+timedelta(hours=i)
    entry=frozen_candidate(t,"0x%04x"%i,
                          flow_15m=500 if good else 150)
    exit_price=1.12 if good else .95
    return {
        "id":f"{t.isoformat()}|{entry['address']}",
        "at":t.isoformat(),"address":entry["address"],
        "quote_at":entry["quote_at"],
        "eligible":False,"gates":entry["gates"],
        "features":{k:v for k,v in entry.items() if k not in (
            "id","address","at","quote_at","gates","eligible","price_sda")},
        "entry_price_sda":1.0,
        "outcomes":{"60m":{"observed_at":(t+timedelta(minutes=65)).isoformat(),
                            "elapsed_minutes":65,"price_sda":exit_price}},
        "observed_peak_240m":{"price_return_pct":16 if good else -2},
    }


def dataset(n=140):
    # Only the 15-minute flow differentiates good and bad candidates.
    return [research_row(i,good=(i%2==0)) for i in range(n)]


class MissedPumpCausalTests(unittest.TestCase):
    def test_retains_first_fresh_snapshot_per_30min_not_each_scanner_minute(self):
        a=frozen_candidate(START)
        b=frozen_candidate(START+timedelta(minutes=7),score=99)
        state={"funnels":[{"at":a["at"],"candidate_snapshots":[a]},
                          {"at":b["at"],"candidate_snapshots":[b]}]}
        market={"updated_at":(START+timedelta(minutes=10)).isoformat(),
                "tokens":{TOKEN:{"analysis":{
                    "price_in_sda":1.05,
                    "last_transaction":(START+timedelta(minutes=9)).isoformat()}}}}
        result=update(state,market)
        self.assertEqual(len(result["observations"]),1)
        original=result["observations"][0]
        self.assertEqual(original["features"]["score"],76)
        self.assertEqual(original["at"],a["at"])
        # No later high from the current hour becomes a fake 60m exit.
        # The new 5m research observation is allowed, but cannot become a 60m exit.
        self.assertNotIn("60m",original["outcomes"])
        later=copy.deepcopy(state)
        later["observations"]=result["observations"]
        market["updated_at"]=(START+timedelta(minutes=66)).isoformat()
        market["tokens"][TOKEN]["analysis"]={
            "price_in_sda":1.15,
            "last_transaction":(START+timedelta(minutes=65)).isoformat()}
        final=update(later,market)
        outcome=final["observations"][0]["outcomes"]["60m"]
        self.assertAlmostEqual(outcome["net_sda_model"],net_sda(1.0,1.15),4)
        self.assertEqual(final["observations"][0]["features"]["score"],76)

    def test_no_lookahead_or_stale_candidate_can_enter_dataset(self):
        t=START
        row=research_row(1)
        stale={**row,"quote_at":(t-timedelta(hours=9)).isoformat()}
        self.assertEqual(normalize([stale]),[])
        future={**row, "outcomes":{"60m":{
            "observed_at":(t+timedelta(hours=10)).isoformat(),
            "price_sda":100}}}
        self.assertEqual(normalize([future]),[])
        # Extra 2h timestamp after hour 1 is not a reliable 60m label.
        bad={**row,"outcomes":{"60m":{
            "observed_at":(START+timedelta(hours=4)).isoformat(),
            "price_sda":100}}}
        self.assertEqual(normalize([bad]),[])

    def test_learns_from_winners_and_losers_and_validates_chronologically(self):
        x=learn(dataset())
        self.assertEqual(x["status"],"VALIDATED_FOR_FORWARD_SHADOW")
        self.assertIn("flow_15m", [
            rule["feature"] for rule in x["candidate"]["policy"]["learned_rules"]])
        self.assertGreater(x["candidate"]["train"]["net_sda_model"],0)
        self.assertGreater(x["candidate"]["validation"]["net_sda_model"],0)
        self.assertFalse(x["can_promote_champion"])
        self.assertFalse(x["candidate"]["can_promote_paper"])
        # Holdout futures MUST NOT change the train-selected threshold.
        altered=dataset()
        for r in altered[100:]:
            r["outcomes"]["60m"]["price_sda"]=0.60
        failed=learn(altered)
        self.assertEqual(failed["candidate"]["policy"],x["candidate"]["policy"])
        self.assertEqual(failed["status"],"TRAIN_RULE_FAILED_VALIDATION")

    def test_excludes_correlated_pump_replays_and_sparse_price_data(self):
        row=research_row(0,True)
        duplicated=[row]+[{**row,"id":str(i),"at":(
            START+timedelta(minutes=i)).isoformat(),
            "quote_at":(START+timedelta(minutes=i-1)).isoformat(),
            "outcomes":{"60m":{
                "observed_at":(START+timedelta(minutes=i+65)).isoformat(),
                "price_sda":1.12}}} for i in range(1,40)]
        self.assertEqual(len(normalize(duplicated)),1)
        self.assertEqual(learn([])["status"],"COLLECTING")

    def test_safe_forward_filter_rejects_late_pumps_or_stale_quotes(self):
        policy={
            **BASELINE,"entry_lane":"missed_pump","learned_feature":"flow_15m",
            "learned_mode":"min","learned_bound":350}
        good=frozen_candidate(START)
        self.assertTrue(safe_missed_entry(good,policy))
        self.assertFalse(safe_missed_entry({**good,"m4h":128},policy))
        self.assertFalse(safe_missed_entry({**good,"flow_15m":80},policy))
        self.assertFalse(safe_missed_entry({**good,"data_age_sec":400},policy))
        self.assertFalse(safe_missed_entry({**good,"trades_1h":2},policy))
        self.assertFalse(safe_missed_entry({**good,"phase":"NO"},policy))
        self.assertFalse(safe_missed_entry(good,{**policy,"learned_feature":"__hack__"}))

    def test_two_feature_rule_is_conjunctive_and_fail_closed(self):
        policy={
            **BASELINE,"entry_lane":"missed_pump",
            "learned_rules":[
                {"feature":"flow_15m","mode":"min","bound":350},
                {"feature":"m4h","mode":"max","bound":15},
            ],
        }
        row=frozen_candidate(START)
        self.assertTrue(safe_missed_entry(row,policy))
        self.assertFalse(safe_missed_entry({**row,"flow_15m":150},policy))
        self.assertFalse(safe_missed_entry({**row,"m4h":22},policy))
        self.assertFalse(safe_missed_entry(row,{
            **policy,"learned_rules":policy["learned_rules"]+[
                {"feature":"m15","mode":"min","bound":1.5}]}))
        self.assertFalse(safe_missed_entry(row,{
            **policy,"learned_rules":[
                policy["learned_rules"][0],policy["learned_rules"][0]]}))
        self.assertFalse(safe_missed_entry(row,{
            **policy,"learned_rules":[
                {"feature":"__unsafe__","mode":"min","bound":0}]}))

    def test_historical_training_uses_original_lane_and_quote_age(self):
        policy={
            **BASELINE,"entry_lane":"missed_pump",
        }
        row=research_row(0,good=True)
        normalized=normalize([row])
        self.assertEqual(len(normalized),1)
        frozen=normalized[0]["frozen_snapshot"]
        self.assertEqual(frozen["phase"],"WATCH")
        self.assertEqual(frozen["data_age_sec"],60)
        self.assertEqual(frozen["price_sda"],1.0)
        self.assertTrue(safe_missed_entry(frozen,policy))

        # Future winnings must never relabel a late breakout as WATCH.
        breakout=copy.deepcopy(row)
        breakout["features"]["phase"]="BREAKOUT"
        observed=normalize([breakout])
        self.assertEqual(observed[0]["frozen_snapshot"]["phase"],"BREAKOUT")
        self.assertFalse(safe_missed_entry(observed[0]["frozen_snapshot"],policy))

        # A 220s-old quote is a known original value; the forward arm's
        # immutable 180s maximum must reject it in historical selection.
        stale=copy.deepcopy(row)
        stale["quote_at"]=(START-timedelta(seconds=220)).isoformat()
        original=normalize([stale])[0]["frozen_snapshot"]
        self.assertEqual(original["data_age_sec"],220)
        self.assertFalse(safe_missed_entry(original,policy))

        # Even an unrealistically profitable historical BREAKOUT cohort
        # cannot fabricate a WATCH-only rule for future paper experiments.
        breakout_only=dataset()
        for item in breakout_only:
            item["features"]["phase"]="BREAKOUT"
        self.assertEqual(learn(breakout_only)["status"],"NO_PROFITABLE_TRAIN_RULE")

    def test_autonomous_enrollment_and_new_forward_virtual_portfolio_only(self):
        rule=learn(dataset())
        at=START+timedelta(days=9)
        with patch("tools.strategy_evolution_agent.proposals_from_evidence",
                   return_value=([],{"status":"EMPTY"})):
            evo=evolve(initial_state(),{"trades":[]},{"strategies":{}},
                       now=at, missed_report=rule,
                       llm_reviewer=lambda *_:{"status":"NOT_CONFIGURED"})
        ids=[e["id"] for e in evo["experiments"]
             if e["id"].startswith("research_miss_")]
        self.assertEqual(len(ids),1)
        self.assertIn(ids[0],experiment_policies(evo))
        self.assertFalse(evo["experiments"][0]["can_promote_paper"])
        with patch("tools.strategy_evolution_agent.proposals_from_evidence",
                   return_value=([],{"status":"EMPTY"})):
            again=evolve(evo,{"trades":[]},{"strategies":{}},
                         now=at+timedelta(hours=1),missed_report=rule)
        self.assertEqual([e["id"] for e in again["experiments"]],ids)
        t=at+timedelta(minutes=5)
        item=frozen_candidate(t)
        market={"updated_at":t.isoformat(),"tokens":{TOKEN:{"analysis":{
            "price_in_sda":1.0,"last_transaction":t.isoformat()}}}}
        funnel={"at":t.isoformat(),"candidate_snapshots":[item]}
        with patch("tools.forward_shadow_agent.load_evolution",return_value=evo):
            state=default_state()
            step(state,market,funnel)
        self.assertIn(TOKEN,state["strategies"][ids[0]]["positions"])
        self.assertEqual(state["strategies"]["baseline"]["positions"],{})


if __name__=="__main__":
    unittest.main()
