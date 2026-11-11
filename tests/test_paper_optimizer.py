"""PAPER Champion vs Challenger optimizer: splits, gates, promotion, rollback, isolation."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_champion_config as champion_config
import paper_optimizer_view as view
from tools import paper_optimizer as opt
from tools import paper_research_dataset as dataset

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
REAL_FILES = ("real_trader.py", "real_swing_sell_executor.py", "swing_sell_v2.py", "real_trading_config.py",
              "real_paper_bridge.py", "real_pnl_stats.py")


def write_trade(root, i, opened, win, score=None, prices=None, status="CLOSED", token=None):
    pid = f"t{i:04d}"
    entry_price = 1.0
    unit = entry_price * 1.01
    path = prices if prices is not None else [unit * (1.55 if win else 0.45)]
    obs = [{"at": (opened + timedelta(minutes=5 * (k + 1))).isoformat(), "price": p} for k, p in enumerate(path)]
    record = {"schema": 1, "position_id": pid, "status": status,
              "entry": {"token": f"T{i}", "contract": token or f"0x{i % 20:04x}", "opened_at": opened.isoformat(),
                        "fill_price": entry_price, "investment_sda": 20.0,
                        "metrics": {"pump_score": score if score is not None else (80 if win else 20)}},
              "observations": obs, "exits": [], "data_quality": {"complete_from_entry": True}}
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")


class Workspace:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "paths"
        self.config = base / "paper_champion_config.json"
        self.report = base / "report.json"
        self.state = base / "state.json"
        self.positions = base / "positions.json"

    def run(self, now, apply=True, counterfactual=None):
        base = Path(self.tmp.name)
        return opt.run(now=now, dataset_root=self.data, positions_file=self.positions, report_file=self.report,
                       state_file=self.state, config_file=self.config, apply=apply,
                       lane_state_file=base / "lane.json", lane_archive_file=base / "archive.json",
                       counterfactual_gate=counterfactual or (lambda filters, when: (True, [])))


def digest(names):
    return {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names if (ROOT / n).exists()}


class SplitAndReplayTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.tmp.cleanup()

    def test_chronological_split_without_overlap(self):
        for i in range(50):
            write_trade(self.ws.data, i, T0 + timedelta(hours=(50 - i)), i % 2 == 0)  # written out of order
        trades = opt.load_trades(self.ws.data, self.ws.positions)
        splits = opt.chronological_splits(trades)
        self.assertEqual([len(splits[k]) for k in ("train", "validation", "holdout")], [30, 10, 10])
        self.assertLess(max(t["opened_at"] for t in splits["train"]), min(t["opened_at"] for t in splits["validation"]))
        self.assertLess(max(t["opened_at"] for t in splits["validation"]), min(t["opened_at"] for t in splits["holdout"]))

    def test_no_lookahead_observations_before_entry_dropped_and_mtm_last_observed(self):
        unit = 1.01
        write_trade(self.ws.data, 1, T0, True, prices=[unit * 1.1, unit * 1.2])
        record = json.loads((self.ws.data / "t0001.json").read_text())
        record["observations"].insert(0, {"at": (T0 - timedelta(minutes=5)).isoformat(), "price": 99.0})
        (self.ws.data / "t0001.json").write_text(json.dumps(record))
        trade = opt.load_trades(self.ws.data, self.ws.positions)[0]
        self.assertEqual([o["price"] for o in trade["observations"]], [unit * 1.1, unit * 1.2])
        result = opt.replay(trade)
        self.assertTrue(result["censored"])
        expected = 20 / 1.0 * unit * 1.2 * 0.999 * 0.99 - 20 * 1.01
        self.assertAlmostEqual(result["unrealized"], expected, places=9)

    def test_champion_replay_uses_shared_policy(self):
        unit = 1.01
        write_trade(self.ws.data, 1, T0, True, prices=[unit * 1.26, unit * 1.45, unit * 1.45 * 0.849])
        trade = opt.load_trades(self.ws.data, self.ws.positions)[0]
        result = opt.replay(trade)
        self.assertEqual(result["sells"], 2)  # +25% partial, then 15% trailing stop
        self.assertFalse(result["censored"])


class GateTests(unittest.TestCase):
    def m(self, **kw):
        base = {"trades": 20, "tokens": 10, "net_pnl": 10.0, "profit_factor": 2.0, "profit_factor_infinite": False,
                "expectancy": 0.5, "single_trade_share": 0.2, "max_drawdown": 2.0}
        base.update(kw)
        return base

    def test_holdout_gates(self):
        champ = self.m(net_pnl=1.0, expectancy=0.05, max_drawdown=2.0)
        self.assertTrue(opt.gate_holdout(self.m(), champ)[0])
        for kw in (dict(trades=9), dict(tokens=4), dict(net_pnl=-1), dict(profit_factor=1.1),
                   dict(single_trade_share=0.41), dict(max_drawdown=3.0), dict(net_pnl=1.01)):
            with self.subTest(kw=kw):
                self.assertFalse(opt.gate_holdout(self.m(**kw), champ)[0])

    def test_validation_and_train_gates(self):
        champ = self.m(net_pnl=0.0)
        self.assertTrue(opt.gate_validation(self.m(), champ)[0])
        self.assertFalse(opt.gate_validation(self.m(profit_factor=1.15), champ)[0])
        self.assertFalse(opt.gate_validation(self.m(expectancy=0), champ)[0])
        self.assertFalse(opt.gate_train(self.m(trades=4), champ)[0])

    def test_fee_sensitivity_blocks_promotion(self):
        candidate = {"id": "c", "kind": "buy", "sell_params": None, "buy_filters": {}}
        self.assertFalse(opt.gate_robustness(candidate, [], -0.01)[0])

    def test_data_quality_gate(self):
        self.assertFalse(opt.gate_data_quality([], [])[0])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.tmp.cleanup()

    def test_insufficient_data_keeps_champion(self):
        for i in range(10):
            write_trade(self.ws.data, i, T0 + timedelta(hours=i), i % 2 == 0)
        report = self.ws.run(T0 + timedelta(days=2))
        self.assertEqual(report["decision"], "KEEP CHAMPION")
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertFalse(self.ws.config.exists())
        self.assertEqual(champion_config.load(self.ws.config)["version"], champion_config.BASELINE_VERSION)

    def test_shadow_then_paper_only_promotion_never_touches_real(self):
        before = digest(REAL_FILES)
        for i in range(100):
            write_trade(self.ws.data, i, T0 + timedelta(hours=3 * i), i % 2 == 0)
        start = T0 + timedelta(hours=301)
        first = self.ws.run(start)
        self.assertEqual(first["status"], "SHADOW", first["reasons"])
        self.assertEqual(first["challenger"]["buy_filters"], {"min_score": 80.0})
        self.assertFalse(first["promotion_applied"])
        self.assertFalse(self.ws.config.exists())
        # Forward Shadow: identical opportunities opened after enrollment.
        for i in range(100, 150):
            write_trade(self.ws.data, i, start + timedelta(hours=3 * (i - 99)), i % 2 == 0)
        too_early = self.ws.run(start + timedelta(days=3))
        self.assertEqual(too_early["status"], "SHADOW")
        self.assertFalse(too_early["promotion_applied"])
        final = self.ws.run(start + timedelta(days=8))
        self.assertEqual(final["decision"], "PROMOTE TO PAPER", final["reasons"])
        config = champion_config.load(self.ws.config)
        self.assertEqual(config["buy_filters"], {"min_score": 80.0})
        self.assertEqual(config["previous"]["version"], champion_config.BASELINE_VERSION)
        self.assertEqual(config["sell_policy"], "swing_v2")
        self.assertEqual(final["real_promotion"], "NEVER")
        self.assertEqual(digest(REAL_FILES), before)

    def test_counterfactual_capacity_replay_blocks_promotion(self):
        for i in range(100):
            write_trade(self.ws.data, i, T0 + timedelta(hours=3 * i), i % 2 == 0)
        start = T0 + timedelta(hours=301)
        self.ws.run(start)
        for i in range(100, 150):
            write_trade(self.ws.data, i, start + timedelta(hours=3 * (i - 99)), i % 2 == 0)
        blocked = self.ws.run(start + timedelta(days=8),
                              counterfactual=lambda filters, when: (False, ["net PnL not above Champion"]))
        self.assertEqual(blocked["status"], "SHADOW")
        self.assertFalse(blocked["promotion_applied"])
        self.assertIn("counterfactual capacity replay", blocked["reasons"][0])
        self.assertFalse(self.ws.config.exists())
        self.assertEqual(blocked["lane_research"]["decision"], "KEEP CHAMPION")

    def test_sell_variants_are_never_promoted(self):
        report_like = opt.generate_challengers([])
        self.assertTrue(all(c["kind"] == "sell" for c in report_like))
        self.assertNotIn(opt.CHAMPION_SELL, [c["sell_params"] for c in report_like])

    def test_rollback_restores_previous_champion(self):
        promoted_at = T0 + timedelta(days=1)
        champion_config.promote({"id": "x", "version": "PAPER-CHAMPION-X", "buy_filters": {"min_score": 50}},
                                {}, "test", self.ws.config)
        config = json.loads(self.ws.config.read_text())
        config["promotion"]["at"] = promoted_at.isoformat()
        self.ws.config.write_text(json.dumps(config))
        for i in range(12):
            write_trade(self.ws.data, i, promoted_at + timedelta(hours=i + 1), False)
        report = self.ws.run(promoted_at + timedelta(days=2))
        self.assertTrue(report["rollback_applied"])
        restored = champion_config.load(self.ws.config)
        self.assertEqual(restored["version"], champion_config.BASELINE_VERSION)
        self.assertEqual(restored["buy_filters"], {})

    def test_report_and_state_written_atomically_and_damaged_state_tolerated(self):
        self.ws.state.parent.mkdir(parents=True, exist_ok=True)
        self.ws.state.write_text("{broken")
        write_trade(self.ws.data, 1, T0, True)
        self.ws.run(T0 + timedelta(days=1))
        self.assertTrue(json.loads(self.ws.report.read_text())["paper_only"])
        self.assertTrue(json.loads(self.ws.state.read_text())["last_run_at"])
        self.assertFalse(list(Path(self.ws.tmp.name).glob("*.tmp")))


class LockTests(unittest.TestCase):
    def test_lock_prevents_overlap_and_recovers_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            opt.acquire_lock(lock)
            with self.assertRaises(opt.OptimizerLocked):
                opt.acquire_lock(lock)
            old = time.time() - opt.LOCK_STALE_SECONDS - 10
            os.utime(lock, (old, old))
            self.assertEqual(opt.acquire_lock(lock), lock)


class ChampionConfigTests(unittest.TestCase):
    def test_empty_filters_never_veto_and_damaged_file_is_baseline(self):
        self.assertEqual(champion_config.buy_veto({"pump_score": 1}, {"buy_filters": {}}), [])
        self.assertTrue(champion_config.buy_veto({"pump_score": 1}, {"buy_filters": {"min_score": 50}}))
        self.assertTrue(champion_config.buy_veto({}, {"buy_filters": {"min_score": 50}}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{oops")
            self.assertEqual(champion_config.load(path)["buy_filters"], {})

    def test_repo_baseline_config_matches_expected_initial_state(self):
        config = champion_config.load(ROOT / "paper_champion_config.json")
        self.assertEqual(config["version"], "PAPER-CHAMPION-1")
        self.assertEqual(config["sell_policy"], "swing_v2")
        self.assertEqual(config["buy_filters"], {})


class TelegramViewTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        for i in range(10):
            write_trade(self.ws.data, i, T0 + timedelta(hours=i), i % 2 == 0)
        self.report = self.ws.run(T0 + timedelta(days=1))

    def tearDown(self):
        self.ws.tmp.cleanup()

    def test_all_screens_render_without_trading_calls(self):
        text = view.overview(self.report)
        self.assertIn("🧠 PAPER OPTIMIZER", text)
        self.assertIn("Decision: KEEP CHAMPION", text)
        self.assertIn("PAPER-CHAMPION-1", text)
        for fn in (view.champion, view.challenger, view.comparison, view.research, view.rollback):
            out = fn(self.report)
            self.assertIsInstance(out, str)
            self.assertLess(len(out), 4096)
            self.assertNotIn("Traceback", out)

    def test_missing_report_is_friendly(self):
        for fn in (view.overview, view.champion, view.challenger, view.comparison, view.research, view.rollback):
            self.assertIn("not available", fn({}))

    def test_keyboard_and_install(self):
        data = [b["callback_data"] for row in view.keyboard()["inline_keyboard"] for b in row]
        self.assertEqual(data, ["OPT_CHAMPION", "OPT_CHALLENGER", "OPT_COMPARE", "OPT_RESEARCH", "OPT_ROLLBACK", "MAIN"])

        class Dash:
            pass

        dash = view.install(Dash())
        for name in ("optimizer_overview_report", "optimizer_champion_report", "optimizer_challenger_report",
                     "optimizer_comparison_report", "optimizer_paper_research_report", "optimizer_rollback_report",
                     "optimizer_menu_keyboard"):
            self.assertTrue(callable(getattr(dash, name)))


if __name__ == "__main__":
    unittest.main()
