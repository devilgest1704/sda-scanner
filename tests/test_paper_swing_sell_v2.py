"""PAPER uses the exact shared Swing SELL V2 policy (same decide() as REAL)."""
import ast
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import engine  # production engine (engine_legacy + paper_stats_patch partial handling)
import paper_swing_sell_v2 as paper_v2
import swing_sell_v2 as policy
import v30_pump_hunter as v
from tools import paper_research_dataset as dataset
from tools import paper_optimizer as optimizer

ENTRY_REF = 100.0
UNIT = ENTRY_REF * 1.001 * 1.01  # engine slippage on entry, then fee in the cost basis


def price_for(pnl):
    return UNIT * (1 + pnl)


class PaperSequence:
    """Runs the real PAPER engine through successive fresh observations."""

    def __init__(self, tmp):
        self.d = dict(confidence=40, pump_score=40, pump_quality=40, pump_change=0, pump_phase='NO',
                      m1h=0, m15=0, m4h=0, net_1h=100, volume_1h=250, trades_1h=2, data_age_sec=20)
        self.e = SimpleNamespace(create=engine.create, close=engine.close, main=lambda: None, FEE_RATE=engine.FEE_RATE)
        self.patches = [patch.object(v, 'PAPER_TP4', True), patch.object(v, 'decision', return_value=self.d),
                        patch.object(v, 'load_state', return_value={}), patch.object(v, 'save_state'),
                        patch.object(dataset, 'DATASET_DIR', Path(tmp))]
        for p in self.patches:
            p.start()
        v.patch(SimpleNamespace(), self.e)
        pos = self.e.create('x', {'price_in_sda': ENTRY_REF}, self.d, {}, {}, 100)
        pos['opened_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.book = dict(positions={'x': pos}, closed_trades=[])
        self.events = []

    def step(self, price):
        qt = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        self.events += self.e._auto_exit(self.book, {'x': {'analysis': {'price_in_sda': price, 'last_transaction': qt}}}, {})
        return self.book

    @property
    def pos(self):
        return self.book['positions'].get('x')

    def reasons(self):
        rows = (self.book.get('partial_exits') or []) + self.book['closed_trades']
        return [t['close_reason'] for t in rows]

    def stop(self):
        for p in reversed(self.patches):
            p.stop()


class PaperSwingSellV2ParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seq = PaperSequence(self.tmp.name)

    def tearDown(self):
        self.seq.stop()
        self.tmp.cleanup()

    def test_new_position_uses_swing_v2_profile(self):
        self.assertEqual(self.seq.pos['paper_exit_profile'], 'swing_v2')
        self.assertIsNone(self.seq.pos['paper_exit_rules']['time_exit'])
        self.assertAlmostEqual(float(self.seq.pos['remaining_cost_basis']), 20 * 1.01)

    def test_minus_49_9_holds_and_minus_50_full_sells(self):
        self.seq.step(price_for(-0.499))
        self.assertEqual(self.seq.reasons(), [])
        self.seq.step(price_for(-0.5001))
        self.assertEqual(self.seq.reasons(), ['SWING V2 STOP -50'])
        self.assertIsNone(self.seq.pos)

    def test_plus_24_9_holds_and_plus_25_partial_once(self):
        self.seq.step(price_for(0.249))
        self.assertEqual(self.seq.reasons(), [])
        self.seq.step(price_for(0.2501))
        self.seq.step(price_for(0.27))
        self.seq.step(price_for(0.26))
        self.assertEqual(self.seq.reasons(), ['SWING V2 PARTIAL +25'])
        row = self.seq.book['partial_exits'][0]
        self.assertEqual(self.seq.book['closed_trades'], [])
        self.assertEqual(row['status'], 'PARTIAL CLOSED')
        self.assertAlmostEqual(row['closed_fraction'], 0.2)
        pos = self.seq.pos
        self.assertTrue(pos['profit_partial_done'])
        self.assertTrue(pos['trailing_armed'])
        self.assertAlmostEqual(float(pos['remaining_quantity']), 20 / 100.1 * 0.8, places=9)
        self.assertAlmostEqual(float(pos['remaining_cost_basis']), 20 * 1.01 * 0.8, places=9)
        self.assertAlmostEqual(float(pos['sold_cost_basis']), 20 * 1.01 * 0.2, places=9)
        self.assertAlmostEqual(float(pos['realized_pnl']), row['closed_profit_sda'], places=9)

    def test_trailing_peak_and_15_percent_exit(self):
        self.seq.step(price_for(0.26))
        self.seq.step(price_for(0.45))
        peak = price_for(0.45)
        self.assertAlmostEqual(float(self.seq.pos['highest_price_since_trailing_activation']), peak)
        self.seq.step(peak * 0.851)
        self.assertEqual(len(self.seq.reasons()), 1)
        self.seq.step(peak * 0.8499)
        self.assertEqual(self.seq.reasons(), ['SWING V2 PARTIAL +25', 'SWING V2 TRAIL'])
        self.assertIsNone(self.seq.pos)

    def test_plus_50_full_sell_after_partial(self):
        self.seq.step(price_for(0.26))
        self.seq.step(price_for(0.5001))
        self.assertEqual(self.seq.reasons(), ['SWING V2 PARTIAL +25', 'SWING V2 TP +50'])
        final = self.seq.book['closed_trades'][0]
        partial = self.seq.book['partial_exits'][0]
        self.assertAlmostEqual(final['realized_from_partial_exits_sda'], partial['closed_profit_sda'], places=6)
        records = dataset.load_all(self.tmp.name)
        self.assertAlmostEqual(sum(x['pnl_sda'] for x in records[0]['exits']), final['closed_profit_sda'], places=5)

    def test_direct_jump_to_plus_50_is_full_sell_without_partial(self):
        self.seq.step(price_for(0.60))
        self.assertEqual(self.seq.reasons(), ['SWING V2 TP +50'])
        self.assertAlmostEqual(self.seq.book['closed_trades'][0]['closed_fraction'], 1.0)

    def test_no_time_exit(self):
        self.seq.pos['opened_at'] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.seq.step(price_for(0.01))
        self.assertEqual(self.seq.reasons(), [])

    def test_swing_v1_open_position_migrates_to_v2(self):
        self.seq.pos['paper_exit_profile'] = 'swing_v1'
        self.seq.step(price_for(-0.20))  # would have been a swing_v1 -8% stop
        self.assertEqual(self.seq.reasons(), [])
        self.assertEqual(self.seq.pos['paper_exit_profile'], 'swing_v2')
        self.assertEqual(self.seq.pos['paper_exit_profile_previous'], 'swing_v1')

    def test_dataset_records_observations_and_exits(self):
        self.seq.step(price_for(0.26))
        self.seq.step(price_for(0.6))
        records = dataset.load_all(self.tmp.name)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]['observations']), 2)
        self.assertEqual([x['reason'] for x in records[0]['exits']], ['SWING V2 PARTIAL +25', 'SWING V2 TP +50'])
        self.assertEqual(records[0]['status'], 'CLOSED')


class PolicyIdentityTests(unittest.TestCase):
    def test_paper_decisions_equal_shared_policy(self):
        for pnl in (-0.6, -0.5, -0.499, 0, 0.249, 0.25, 0.3, 0.5, 0.7):
            for done in (False, True):
                pos = {"profit_partial_done": done, "trailing_armed": done,
                       "highest_price_since_trailing_activation": "120" if done else None}
                price = 100 * (1 + pnl) + 1
                with self.subTest(pnl=pnl, done=done):
                    expected = policy.decide(pos, pnl, price)
                    state = {"profit_partial_done": done, "trailing_armed": done, "peak": 120 if done else None}
                    got = optimizer.param_decide(state, Decimal(str(pnl)), price, optimizer.CHAMPION_SELL)
                    self.assertEqual(got["action"], expected["action"])
                    self.assertEqual(got["reason"], expected["reason"])

    def test_rules_are_read_from_the_shared_policy(self):
        self.assertEqual(paper_v2.RULES["emergency_pnl"], str(policy.LOSS_EXIT_PNL))
        self.assertEqual(paper_v2.RULES["partial_trigger_pnl"], str(policy.PARTIAL_TRIGGER_PNL))
        self.assertEqual(paper_v2.RULES["trailing_distance"], str(policy.TRAILING_DISTANCE))
        self.assertEqual(paper_v2.RULES["hard_tp_pnl"], str(policy.HARD_TP_PNL))
        self.assertEqual(paper_v2.policy, policy)

    def test_pnl_formula_matches_real_cost_basis_formula(self):
        pos = {"entry_price": 100.1, "investment_sda": 20}
        paper_v2.ensure_state(pos, 0.01)
        self.assertAlmostEqual(float(paper_v2.pnl_fraction(pos, UNIT * 1.25)), 0.25, places=12)

    def test_paper_modules_never_import_real_execution(self):
        forbidden = {"real_trader", "real_swing_sell_executor", "real_trading_config", "real_paper_bridge"}
        root = Path(__file__).resolve().parents[1]
        for name in ("paper_swing_sell_v2.py", "paper_champion_config.py", "paper_optimizer_view.py",
                     "tools/paper_optimizer.py", "tools/paper_research_dataset.py", "v30_pump_hunter.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            with self.subTest(module=name):
                self.assertFalse(imported & forbidden)


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        now = datetime.now(timezone.utc)
        self.pos = {"address": "0xabc", "label": "ABC", "opened_at": now.isoformat(), "entry_price": 1.0,
                    "reference_price": 1.0, "investment_sda": 20, "paper_strategy": "temporal_recovery_tp4"}
        self.now = now

    def tearDown(self):
        self.tmp.cleanup()

    def obs(self, seconds, price, quote=None):
        at = (self.now + timedelta(seconds=seconds)).isoformat()
        return {"at": at, "quote_at": quote or at, "price": price}

    def test_dedupe_out_of_order_and_restart(self):
        dataset.record_observation(self.pos, self.obs(60, 1.1), 0.01, root=self.tmp.name)
        dataset.record_observation(self.pos, self.obs(60, 1.1), 0.01, root=self.tmp.name)
        dataset.record_observation(self.pos, self.obs(30, 1.2), 0.01, root=self.tmp.name)
        dataset.record_observation(self.pos, self.obs(120, 1.3), 0.01, root=self.tmp.name)
        # "restart": a fresh load sees exactly the persisted file
        record = dataset.load_all(self.tmp.name)[0]
        self.assertEqual([o["price"] for o in record["observations"]], [1.1, 1.3])
        self.assertEqual(record["entry"]["contract"], "0xabc")
        self.assertTrue(record["data_quality"]["complete_from_entry"])
        self.assertFalse(any(p.suffix == ".tmp" for p in Path(self.tmp.name).iterdir()))


if __name__ == "__main__":
    unittest.main()
