"""Guard the experimental PAPER-only fresh-momentum expansion.

Run: python -m unittest discover -s tests -p 'test_paper_fresh_momentum_thresholds.py' -v
"""
import ast
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "v30_pump_hunter.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
ASSIGNMENTS = {}
for node in TREE.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                try:
                    ASSIGNMENTS[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError, KeyError):
                    pass


class FreshMomentumExpansionTests(unittest.TestCase):
    def test_thresholds_are_selectively_expanded(self):
        self.assertEqual(ASSIGNMENTS["FRESH_MOMENTUM_MIN_SCORE"], 72)
        self.assertEqual(ASSIGNMENTS["FRESH_MOMENTUM_MIN_QUALITY"], 65)
        self.assertEqual(ASSIGNMENTS["MAX_BUYS_PER_RUN"], 1)

    def test_only_fresh_momentum_lane_uses_expanded_thresholds(self):
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("fresh_momentum_ready=(")
        end = source.index("\n    if PAPER_BUY_MODE == \"temporal_recovery\":", start)
        lane = source[start:end]
        self.assertIn("s >= FRESH_MOMENTUM_MIN_SCORE", lane)
        self.assertIn("q >= FRESH_MOMENTUM_MIN_QUALITY", lane)
        for condition in (
            'PAPER_BUY_MODE == "temporal_recovery"',
            'data_age <= 180', 'v31_trades_ok', 'c["vol"] >= 500',
            'c["flow"] >= 200', 'c["flow15"] >= 60',
            'c["buy_ratio"] >= 1.3', 'fresh and entry_impulse >= 6',
            'liquidity_ok and price_flow_ok',
        ):
            self.assertIn(condition, lane)

    def test_fresh_momentum_threshold_boundary_behavior(self):
        """Execute the actual lane expression, not a reimplemented approximation."""
        decision = next(node for node in TREE.body
                        if isinstance(node, ast.FunctionDef) and node.name == "decision")
        assign = next(node for node in ast.walk(decision)
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "fresh_momentum_ready"
                              for t in node.targets))
        expr = compile(ast.Expression(body=assign.value), str(SOURCE), "eval")
        c = {"vol": 700, "flow": 250, "flow15": 80, "buy_ratio": 1.5,
             "m15": 2.0, "m1": 2.0}
        base = dict(PAPER_BUY_MODE="temporal_recovery", v31_age_ok=True,
                    data_age=60, v31_trades_ok=True, c=c, s=72, q=65,
                    phase="CONFIRMATION", fresh=True, entry_impulse=8,
                    liquidity_ok=True, price_flow_ok=True,
                    FRESH_MOMENTUM_MIN_SCORE=72,
                    FRESH_MOMENTUM_MIN_QUALITY=65)
        self.assertTrue(eval(expr, {"__builtins__": {}}, base))
        for changes in ({"s": 71.9}, {"q": 64.9}, {"data_age": 181},
                        {"fresh": False}, {"v31_trades_ok": False},
                        {"liquidity_ok": False}, {"price_flow_ok": False},
                        {"PAPER_BUY_MODE": "baseline"}):
            candidate = dict(base, **changes)
            self.assertFalse(eval(expr, {"__builtins__": {}}, candidate),
                             changes)

    def test_global_safety_vetoes_still_apply(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('or macd_gate["blocked"] or loss_circuit["blocked"]', source)
        self.assertIn('or cd or not v31_age_ok', source)
        self.assertIn('or not temporal_activity_ok or not price_flow_ok or late_spike', source)

    def test_strong_unconfirmed_snapshot_is_research_only_in_actual_buy_path(self):
        import v30_pump_hunter as v30
        c = dict(price=1, m1=3, m15=2, m4=4, flow=500, flow15=100,
                 vol=2000, trades=24, buy_ratio=3, trade_ratio=2,
                 data_age_sec=30, last_transaction=datetime.now(timezone.utc).isoformat())
        with (patch.object(v30, 'PAPER_BUY_MODE', 'temporal_recovery'),
              patch.object(v30, 'score', return_value=(c,90,78,12,'IGNITION')),
              patch.object(v30, 'entry_gate', return_value=dict(
                  buy=True,entry_impulse=12,liquidity_ok=True,price_flow_ok=True,ratio=.25)),
              patch.object(v30, 'load_state', return_value={'history':{'0xab':{'last':{
                  'm1':1,'m15':1,'flow':100,'vol':500}}}}),
              patch.object(v30, 'cooldown_active', return_value=False),
              patch.object(v30, 'paper_macd_veto', return_value={'blocked':False}),
              patch.object(v30, 'paper_entry_risk_gate', return_value={'blocked':False}),
              patch.object(v30, 'temporal_recovery_signal', return_value={
                  'ready':False,'reason':'missing_prior_frozen_signal'}) as signal):
            result=v30.decision('0xab',{},persist=False)
            signal.return_value={'ready':True,'lane':'temporal_recovery','reason':'NEW_FRESH_QUOTE_CONFIRMED'}
            confirmed=v30.decision('0xab',{},persist=False)
        self.assertTrue(result['fresh_momentum_research_ready'])
        self.assertFalse(result['eligible_for_buy'])
        self.assertLess(result['confidence'],v30.ENTRY_SCORE)
        self.assertIn('fresh_momentum_research_only',result['paper_buy_gate_failures'])
        self.assertTrue(confirmed['eligible_for_buy'])
        self.assertFalse(confirmed['fresh_momentum_research_ready'])

    def test_unconfirmed_shadow_requires_frozen_readiness_and_current_safety(self):
        from tools.forward_shadow_agent import eligible,POLICIES,FRESH_MOMENTUM_RESEARCH_MODE as mode
        row=dict(phase='IGNITION',data_age_sec=30,score=90,quality=78,impulse=12,
                 trades_1h=24,buy_ratio=3,volume_1h=2000,flow_1h=500,m15=2,m1h=3)
        self.assertFalse(eligible(row,POLICIES[mode],macd_mode=mode))
        row['fresh_momentum_research_ready']=True
        self.assertTrue(eligible(row,POLICIES[mode],macd_mode=mode))
        self.assertFalse(eligible({**row,'data_age_sec':901},POLICIES[mode],macd_mode=mode))


if __name__ == "__main__":
    unittest.main()
