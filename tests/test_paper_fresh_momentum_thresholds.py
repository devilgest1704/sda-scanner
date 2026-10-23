"""Guard the experimental PAPER-only fresh-momentum expansion.

Run: python -m unittest discover -s tests -p 'test_paper_fresh_momentum_thresholds.py' -v
"""
import ast
import unittest
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


if __name__ == "__main__":
    unittest.main()
