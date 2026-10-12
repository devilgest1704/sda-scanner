"""Forward-only, non-promotable V32 breakout and volume research tests."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from tools.v32_breakout import (
    analyze_breakout, research_signal_from_analysis,
    breakout_research_eligible,
)
from technical_analysis import analyze_token
from tools.forward_shadow_agent import default_state, step, POLICIES

T = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ADDRESS = "0xab"


def history(as_of=T, *, weak_volume=False, weak_flow=False, width=0.03):
    """Old 2h consolidation followed by fresh 15m breakout and buy surge."""
    rows = []
    # 15 old observations separated by 8m, last at t-33m.
    for i in range(15):
        at = as_of-timedelta(minutes=145-8*i)
        price = 1.0 + (i%4)/3 * width
        rows.append({
            "timestamp": at.isoformat(), "price": price,
            "volume": 24.0, "type": "buy" if i%2 else "sell",
        })
    # Six BUY and one SELL inside the 15m confirmation interval.
    for i in range(7):
        at = as_of - timedelta(minutes=13 - i*2)
        price = 1.033 + i*0.004
        side = "sell" if (i == 0 or (weak_flow and i in (1,2,3,4,5))) else "buy"
        vol = 2 if weak_volume else 70
        rows.append({
            "timestamp": at.isoformat(), "price": price,
            "volume": float(vol), "type": side,
        })
    return rows


def candidate(price=1.057, phase="WATCH", **extra):
    row = {
        "address": ADDRESS, "phase": phase, "score": 65, "quality": 60,
        "impulse": 0, "snapshot_impulse": 9, "trades_1h": 14,
        "buy_ratio": 2, "flow_1h": 400, "volume_1h": 900,
        "m15": 2, "m1h": 4, "price_sda": price,
        "data_age_sec": 60,
    }
    row.update(extra)
    return row


def market(t=T, *, price=1.057, source=None, txn_at=None):
    source = history() if source is None else source
    breakout = analyze_breakout(source, t)
    return {
        "updated_at": t.isoformat(),
        "tokens": {
            ADDRESS: {"analysis": {
                "price_in_sda": price,
                "last_transaction": (txn_at or t-timedelta(minutes=1)).isoformat(),
                "technical": {"breakout_volume_trend": breakout},
            }}
        }
    }


def funnel(t=T, **extra):
    return {"at": t.isoformat(), "candidate_snapshots": [candidate(**extra)]}


class BreakoutEvidenceTests(unittest.TestCase):
    def test_early_breakout_requires_real_volume_and_consolidation(self):
        x = analyze_breakout(history(), T)
        self.assertTrue(x["eligible"], x)
        self.assertEqual(x["mode"], "RESEARCH_ONLY")
        self.assertEqual(x["reason"], "confirmed_early_breakout")
        self.assertGreater(x["base_trades"], 10)
        self.assertGreater(x["breakout_pct"], .5)
        self.assertLess(x["breakout_pct"], 8)
        self.assertGreater(x["volume_multiple"], 1.5)
        self.assertGreater(x["recent_15m_net_flow_sda"], 150)

    def test_no_future_transaction_or_transaction_order_lookahead(self):
        src=history()
        actual=analyze_breakout(src,T)
        future={"timestamp": (T+timedelta(minutes=1)).isoformat(),
                "price":100, "volume":1e6, "type":"sell"}
        self.assertEqual(actual,analyze_breakout(list(reversed(src))+[future],T))

    def test_no_yesterday_price_as_breakout(self):
        src=history()
        at=T+timedelta(minutes=12)
        x=analyze_breakout(src,at)
        self.assertFalse(x["eligible"])
        self.assertIn("stale_quote",x["rejection_reasons"])

    def test_no_short_consolidation_or_sparse_old_data(self):
        src=history()
        src=[r for r in src if datetime.fromisoformat(r["timestamp"])>=T-timedelta(minutes=42)]
        x=analyze_breakout(src,T)
        self.assertFalse(x["eligible"])
        self.assertIn("insufficient_base_trades",x["rejection_reasons"])

    def test_weak_volume_or_sell_pressure_fail_closed(self):
        v=analyze_breakout(history(weak_volume=True),T)
        self.assertFalse(v["eligible"])
        self.assertIn("insufficient_recent_volume",v["rejection_reasons"])
        sells=analyze_breakout(history(weak_flow=True),T)
        self.assertFalse(sells["eligible"])
        self.assertIn("weak_buying_pressure",sells["rejection_reasons"])

    def test_chasing_late_pump_not_accepted(self):
        src=history()
        src[-1]["price"]=1.20
        r=analyze_breakout(src,T)
        self.assertFalse(r["eligible"])
        self.assertIn("outside_early_breakout_zone",r["rejection_reasons"])

    def test_technical_analysis_includes_breakout_without_rewriting_old_frames(self):
        a=analyze_token({"transactions":history()},T)
        self.assertIn("breakout_volume_trend",a)
        self.assertTrue(a["breakout_volume_trend"]["eligible"])
        self.assertIn("1h",a)
        self.assertIn("5m",a)

    def test_join_requires_price_and_timestamp_alignment(self):
        src=market()
        info=src["tokens"][ADDRESS]["analysis"]
        joined=research_signal_from_analysis(info,T,T)
        self.assertTrue(joined["eligible"])
        bad=copy.deepcopy(info)
        bad["price_in_sda"]=.70
        self.assertIsNone(research_signal_from_analysis(bad,T,T))
        self.assertIsNone(research_signal_from_analysis(info,T+timedelta(minutes=10),T))
        self.assertIsNone(research_signal_from_analysis(info,T,T+timedelta(minutes=1)))
        future=copy.deepcopy(info)
        future["technical"]["breakout_volume_trend"]["observed_at"]=(T+timedelta(minutes=2)).isoformat()
        self.assertIsNone(research_signal_from_analysis(future,T,T))

    def test_independent_research_watch_gate_and_unpromotable_label(self):
        assert "research_breakout_volume_trend" in POLICIES
        assert "research_breakout_volume_trend".startswith("research_")
        row=candidate()
        row["v32_breakout"]=analyze_breakout(history(),T)
        self.assertTrue(breakout_research_eligible(row,POLICIES["research_breakout_volume_trend"]))
        row["quality"]=15
        self.assertFalse(breakout_research_eligible(row,POLICIES["research_breakout_volume_trend"]))


class BreakoutShadowTests(unittest.TestCase):
    def test_watch_breakout_opens_only_research_position(self):
        state=default_state()
        result=step(state,market(),funnel())
        self.assertEqual(result["status"],"processed")
        self.assertTrue(result["breakout_research_paper_only"])
        self.assertEqual(len(state["strategies"]["baseline"]["positions"]),0)
        breakout=state["strategies"]["research_breakout_volume_trend"]
        self.assertEqual(len(breakout["positions"]),1)
        entry=breakout["positions"][ADDRESS]
        self.assertTrue(entry["v32_breakout_at_entry"]["eligible"])
        diag=state["last_diagnostic"]["breakout_research"]
        self.assertEqual(diag["eligible_from_transactions"],1)
        self.assertEqual(diag["eligible_after_shadow_gates"],1)
        self.assertEqual(diag["qualified_samples"][0]["address"],ADDRESS)

    def test_late_entry_fails_without_affecting_existing_watch_research(self):
        state=default_state()
        src=history()
        src[-1]["price"]=1.19
        # Keep price data aligned with the late spike.
        late=market(price=1.19,source=src)
        step(state,late,funnel(price=1.19))
        self.assertEqual(len(state["strategies"]["research_breakout_volume_trend"]["positions"]),0)
        self.assertEqual(state["last_diagnostic"]["breakout_research"]["eligible_from_transactions"],0)
        self.assertIn("outside_early_breakout_zone",
                      state["last_diagnostic"]["breakout_research"]["rejection_reasons"])

    def test_closed_outcomes_preserve_entry_signal_and_after_fee_sda(self):
        state=default_state()
        step(state,market(),funnel())
        end=T+timedelta(minutes=6)
        old=market()
        old["updated_at"]=end.isoformat()
        old["tokens"][ADDRESS]["analysis"]["price_in_sda"]=1.16
        old["tokens"][ADDRESS]["analysis"]["last_transaction"]=end.isoformat()
        # Old technical evidence is no longer same-scan; exit must still
        # evaluate a newly observed quote independently of entry signals.
        step(state,old,funnel(end,price=1.16,phase="WATCH"))
        trades=state["strategies"]["research_breakout_volume_trend"]["trades"]
        self.assertEqual(len(trades),1)
        self.assertGreater(trades[0]["pnl_sda"],0)
        self.assertEqual(trades[0]["reason"],"TAKE")
        self.assertTrue(trades[0]["v32_breakout_at_entry"]["eligible"])
        self.assertTrue(trades[0]["valid_for_learning"])
        self.assertEqual(len(state["strategies"]["baseline"]["trades"]),0)

    def test_old_shadow_journals_are_preserved_on_new_arm(self):
        previous=default_state()
        previous["strategies"].pop("research_breakout_volume_trend")
        from tools.forward_shadow_agent import load_state
        import json
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"old.json"
            p.write_text(json.dumps(previous))
            upgraded=load_state(p)
        self.assertIn("research_breakout_volume_trend",upgraded["strategies"])
        self.assertEqual(upgraded["runs"],previous["runs"])


if __name__ == "__main__":
    unittest.main()
