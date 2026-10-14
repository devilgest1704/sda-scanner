"""The hourly agent must produce the missed-opportunity report before reading it.

No broker orders, network or future quote lookahead. Test the SAME entry points
used by scheduled GitHub Actions rather than only isolated learner helpers.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.missed_pump_learning import learn
from tools.strategy_evolution_agent import main as evolve_main
from tools.forward_shadow_optimizer import report as optimizer_report
from tools.forward_shadow_agent import default_state, POLICIES

ORIGIN = datetime(2026, 9, 1, tzinfo=timezone.utc)


def archived_rows(count=140):
    """Causal rejected candidates with distinct tokens and true future quotes."""
    rows = []
    for i in range(count):
        now = ORIGIN + timedelta(hours=2*i)
        good = i % 2 == 0
        row = {
            "at": now.isoformat(),
            "address": "0x%040x" % (i+1),
            "quote_at": (now-timedelta(minutes=1)).isoformat(),
            "entry_price_sda": 1.0,
            "eligible": False,
            "gates": ["impulse", "lifecycle"],
            "features": {
                "score": 76, "quality": 70, "snapshot_impulse": 12,
                "m1h": 4, "m15": 2.5, "m4h": 12,
                "trades_1h": 8, "buy_ratio": 2.2,
                "volume_1h": 1500, "flow_1h": 800,
                "flow_15m": 500 if good else 150,
            },
            "outcomes": {
                "60m": {
                    "observed_at": (now+timedelta(minutes=65)).isoformat(),
                    "price_sda": 1.12 if good else 0.95,
                }
            },
        }
        rows.append(row)
    return rows


class MissedPumpAutonomousPipelineTest(unittest.TestCase):
    def test_archive_coverage_does_not_masquerade_as_zero_candidates(self):
        result = learn([{"at":ORIGIN.isoformat(), "eligible":False,
                         "gates":["score"], "outcomes":{}}])
        self.assertEqual(result["archived_candidate_snapshots"],1)
        self.assertEqual(result["candidate_snapshots_with_60m_quote"],0)
        self.assertEqual(result["sample_count"],0)
        self.assertEqual(result["status"],"NO_VALID_MATURED_OUTCOMES")
        self.assertEqual(learn([])["status"],"COLLECTING")

    def test_scheduled_evolution_produces_optimizer_visible_report(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                lab=Path("strategy_lab")
                lab.mkdir()
                (lab/"candidate_outcomes.json").write_text(
                    json.dumps({"observations":archived_rows()}),encoding="utf-8")
                (lab/"forward_shadow_state.json").write_text(
                    json.dumps({"strategies":{}}),encoding="utf-8")
                Path("v30_optimizer_trades.json").write_text(
                    '{"trades":[]}',encoding="utf-8")
                evolve_main()
                missed=json.loads((lab/"missed_pump_learning.json").read_text())
                evolution=json.loads((lab/"evolution_state.json").read_text())
                self.assertEqual(missed["sample_count"],140)
                self.assertEqual(missed["archived_candidate_snapshots"],140)
                self.assertEqual(missed["candidate_snapshots_with_60m_quote"],140)
                self.assertEqual(missed["status"],"VALIDATED_FOR_FORWARD_SHADOW")
                self.assertEqual(
                    evolution["diagnosis"]["missed_pump"]["observed_candidates"],140)
                self.assertTrue(any(
                    str(e["id"]).startswith("research_miss_")
                    and e["can_promote_paper"] is False
                    for e in evolution["experiments"]))
                self.assertFalse(evolution["control"]["real_trading"])
                now=datetime.now(timezone.utc)
                state=default_state()
                state["updated_at"]=now.isoformat()
                config={
                    "version":"V30-AUTO-CONFIG-1","generation":0,
                    "filters":POLICIES["baseline"].copy(),
                    "previous_filters":None,
                }
                output,unchanged=optimizer_report(
                    state,config,{"closed_trades":[]},now=now)
                self.assertEqual(output["missed_pump_learning"]["observed_candidates"],140)
                self.assertEqual(output["missed_pump_learning"]["status"],
                                 "VALIDATED_FOR_FORWARD_SHADOW")
                self.assertFalse(output["missed_pump_learning"]["paper_buy_rules_modified"])
                self.assertFalse(output["promotion_applied"])
                self.assertEqual(unchanged,config)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
