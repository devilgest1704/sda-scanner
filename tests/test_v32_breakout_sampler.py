"""Prove lower-score V32 breakout candidates reach Shadow before WATCH top-N clipping."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import v30_pump_hunter as v30


def fake_decision(address, analysis, ws, persist=True):
    return {
        "pump_phase": "WATCH", "pump_score": analysis["test_score"],
        "pump_quality": 60, "pump_change": 6, "pump_delta_impulse": 6,
        "shadow_snapshot_impulse": 10, "trades_1h": 12,
        "buy_ratio": 2.0, "net_1h": 450, "volume_1h": 800,
        "m15": 2.0, "m1h": 4.0, "data_age_sec": 30,
        "data_fresh_for_entry": True, "eligible_for_buy": False,
        "shadow_confirmation_ready": False, "shadow_early_ready": False,
        "paper_buy_gate_failures": ["phase"], "paper_buy_block_reason": "WATCH",
    }


def make_data(ready):
    tokens={}
    for idx in range(130):
        address=f"0x{idx:040x}"
        breakout = ({"eligible": True, "breakout_pct": 1.7,
                     "volume_multiple": 3.5}
                    if idx == 129 and ready else
                    {"eligible": False, "breakout_pct": 0,
                     "volume_multiple": 0})
        tokens[address]={"analysis":{
            "test_score": 150 - idx,
            "price_in_sda": 1,
            "technical": {"breakout_volume_trend": breakout},
        }}
    return {"tokens": tokens}


class ResearchSamplerTests(unittest.TestCase):
    def _run(self, ready):
        with TemporaryDirectory() as d:
            market=Path(d)/"market_analysis.json"
            state=Path(d)/"v30_execution_state.json"
            market.write_text(json.dumps(make_data(ready)), encoding="utf-8")
            with patch.object(v30,"EXECUTION_STATE_FILE",str(state)), \
                 patch.object(v30,"decision",side_effect=fake_decision):
                v30.write_scan_funnel(str(market))
            self.assertTrue(state.exists(), "Telemetry should not fail silently")
            return json.loads(state.read_text())["funnels"][-1]

    def test_low_score_watch_breakout_survives_top_35_sampling(self):
        without=self._run(False)
        with_breakout=self._run(True)
        target=f"0x{129:040x}"
        self.assertNotIn(target, {
            row["address"] for row in without["candidate_snapshots"]
        })
        selected={row["address"]:row for row in with_breakout["candidate_snapshots"]}
        self.assertIn(target,selected)
        self.assertEqual(selected[target]["phase"],"WATCH")
        self.assertEqual(selected[target]["score"],21)
        self.assertTrue(selected[target]["research_breakout_ready"])
        self.assertEqual(with_breakout["breakout_research_ready_total"],1)
        self.assertEqual(with_breakout["breakout_research_sampled"],1)
        self.assertLessEqual(len(selected),120)
        self.assertEqual(with_breakout["champion_ready"],without["champion_ready"])
        self.assertEqual(with_breakout["tokens"],130)

    def test_no_breakout_signal_keeps_existing_candidate_order(self):
        snap=self._run(False)
        row=snap["candidate_snapshots"]
        self.assertEqual(len(row),40)  # 35 WATCH + 40 top-40, with dedup
        self.assertEqual(row[0]["score"],150)
        self.assertEqual(row[-1]["score"],111)
        self.assertEqual(snap["breakout_research_ready_total"],0)
        self.assertEqual(snap["breakout_research_sampled"],0)


if __name__ == "__main__":
    unittest.main()
