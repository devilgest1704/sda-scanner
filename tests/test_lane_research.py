"""BUY-lane research: attribution, isolation, counterfactual capacity replay, safety."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import swing_sell_v2 as policy
from tools import lane_research as lr
from tools import paper_optimizer as po

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
REAL_FILES = ("real_trader.py", "real_swing_sell_executor.py", "real_paper_bridge.py",
              "real_trading_config.py", "real_market_price.py", "swing_sell_v2.py")


def signal(token, minutes, lane="confirmed_continuation", price=1.0, score=60.0, seen_minutes=0, **features):
    at = T0 + timedelta(minutes=minutes)
    f = {"score": score, "quality": score, "impulse": 1.0, "flow_15m": 50.0, "flow_1h": 300.0,
         "volume_1h": 800.0, "trades_1h": 10.0, "buy_ratio": 1.5, "m15": 1.0, "m1h": 2.0, "phase": "WATCH"}
    f.update(features)
    return {"token": token, "symbol": token.upper(), "quote_at": (at - timedelta(seconds=30)).isoformat(),
            "first_seen": at.isoformat(), "last_seen": (at + timedelta(minutes=seen_minutes)).isoformat(),
            "price": price, "lane": lane, "source": "live_recorder", "features": f}


def path(*points):
    return [(T0 + timedelta(minutes=m), p) for m, p in points]


def digest(names):
    return {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in names if (ROOT / n).exists()}


class ReplayTests(unittest.TestCase):
    def test_full_chronological_replay_regardless_of_input_order(self):
        sigs = [signal(f"t{i}", 10 * i) for i in range(5)]
        paths = {s["token"]: path((10 * i + 5, 1.0)) for i, s in enumerate(sigs)}
        sim = lr.simulate(list(reversed(sigs)), paths, lambda s: True, as_of=T0 + timedelta(days=1))
        self.assertEqual([t["token"] for t in sim["trades"]], [s["token"] for s in sigs])

    def test_max_ten_positions_and_capacity_blocking(self):
        sigs = [signal(f"t{i:02d}", i) for i in range(15)]
        paths = {s["token"]: path((100, 1.0)) for s in sigs}
        sim = lr.simulate(sigs, paths, lambda s: True, as_of=T0 + timedelta(days=1))
        self.assertEqual(sim["max_open"], 10)
        self.assertEqual(len(sim["trades"]), 10)
        self.assertEqual(sim["counters"]["blocked_capacity"], 5)

    def test_slot_release_lets_later_candidate_occupy_freed_slot(self):
        sigs = [signal("a", 0), signal("b", 30), signal("c", 120)]
        # 'a' hits the -50% loss limit at minute 60 and releases its slot.
        paths = {"a": path((60, 0.4)), "b": path((200, 1.0)), "c": path((200, 1.0))}
        sim = lr.simulate(sigs, paths, lambda s: True, as_of=T0 + timedelta(days=1), slots=1)
        tokens = [t["token"] for t in sim["trades"]]
        self.assertEqual(tokens, ["a", "c"])
        self.assertEqual(sim["counters"]["blocked_capacity"], 1)
        first = sim["trades"][0]
        self.assertEqual(first["exit_outcome"], "CLOSED")
        self.assertEqual(first["exit_at"], (T0 + timedelta(minutes=60)).isoformat())

    def test_rejected_champion_trade_frees_slot_for_later_signal(self):
        sigs = [signal("a", 0, lane="swing_single_quote"), signal("b", 30)]
        paths = {"a": path((500, 1.0)), "b": path((500, 1.0))}
        champion = lr.simulate(sigs, paths, lambda s: True, as_of=T0 + timedelta(days=1), slots=1)
        confirmed = lr.simulate(sigs, paths, lambda s: s["lane"] != lr.SINGLE_QUOTE,
                                as_of=T0 + timedelta(days=1), slots=1)
        self.assertEqual([t["token"] for t in champion["trades"]], ["a"])
        self.assertEqual([t["token"] for t in confirmed["trades"]], ["b"])
        self.assertNotEqual({t["token"] for t in confirmed["trades"]}, {"a"},
                            "BUY filters must not be evaluated as a subset of Champion trades")

    def test_signal_still_current_after_release_is_taken_on_next_scan(self):
        sigs = [signal("a", 0), signal("b", 30, seen_minutes=60)]
        paths = {"a": path((60, 0.4)), "b": path((300, 1.0))}
        sim = lr.simulate(sigs, paths, lambda s: True, as_of=T0 + timedelta(days=1), slots=1)
        self.assertEqual([t["token"] for t in sim["trades"]], ["a", "b"])
        self.assertEqual(sim["trades"][1]["opened_at"], (T0 + timedelta(minutes=60)).isoformat())

    def test_one_buy_per_scan_and_cooldown(self):
        sigs = [signal("a", 0, score=90), signal("b", 0, score=80), signal("a", 120)]
        paths = {"a": path((60, 0.4), (300, 1.0)), "b": path((500, 1.0))}
        sim = lr.simulate(sigs, paths, lambda s: True, as_of=T0 + timedelta(days=1))
        self.assertEqual([t["token"] for t in sim["trades"]], ["a"])
        self.assertEqual(sim["counters"]["blocked_per_scan"], 1)

    def test_no_lookahead_past_or_future_quotes(self):
        sig = signal("a", 0)
        # A crash quote BEFORE the signal quote and one AFTER as_of must both be ignored.
        paths = {"a": [(T0 - timedelta(minutes=5), 0.1), (T0 + timedelta(minutes=10), 1.05),
                       (T0 + timedelta(days=3), 0.1)]}
        sim = lr.simulate([sig], paths, lambda s: True, as_of=T0 + timedelta(days=1))
        trade = sim["trades"][0]
        self.assertEqual(trade["exit_outcome"], "OPEN")
        self.assertEqual(trade["mae"], 0.0)
        self.assertAlmostEqual(trade["mfe"], 1.05 / 1.01 - 1, places=6)

    def test_untracked_positions_occupy_slot_without_inferred_pnl(self):
        sim = lr.simulate([signal("a", 0)], {}, lambda s: True, as_of=T0 + timedelta(hours=10))
        trade = sim["trades"][0]
        self.assertEqual(trade["exit_outcome"], "UNTRACKED")
        self.assertIsNone(trade["net_pnl"])
        self.assertEqual(lr.lane_metrics(sim["trades"])["tracked_trades"], 0)

    def test_identical_swing_sell_v2_and_modeled_costs(self):
        sig = signal("a", 0)
        series = path((10, 1.3), (20, 1.6))
        calls = []
        original = policy.decide

        def spy(*args):
            calls.append(args)
            return original(*args)

        with patch.object(policy, "decide", side_effect=spy):
            sim = lr.simulate([sig], {"a": series}, lambda s: True, as_of=T0 + timedelta(days=1))
        self.assertTrue(calls)
        trade = {"id": "x", "token": "a", "opened_at": sig["quote_at"], "fill_price": 1.0,
                 "investment": lr.INVESTMENT_SDA, "status": "OPEN", "complete": True,
                 "observations": [{"at": w.isoformat(), "price": p} for w, p in series]}
        expected = po.replay(trade)
        got = sim["trades"][0]
        self.assertAlmostEqual(got["net_pnl"], round(expected["net"], 6))
        self.assertAlmostEqual(got["modeled_costs"], round(expected["fees"], 6))
        self.assertEqual(got["sells"], 2)  # +25% partial, then +50% hard TP
        higher = lr.simulate([sig], {"a": series}, lambda s: True, as_of=T0 + timedelta(days=1),
                             fee_rate=po.FEE_RATE * 1.5)
        self.assertLess(higher["trades"][0]["net_pnl"], got["net_pnl"])

    def test_constraints_match_paper_champion(self):
        source = (ROOT / "v30_pump_hunter.py").read_text(encoding="utf-8")
        self.assertIn("MAX_OPEN=10 if PAPER_TP4 else 4", source)
        self.assertIn("MAX_BUYS_PER_RUN=1", source)
        self.assertIn("COOLDOWN_HOURS=6.0", source)
        self.assertEqual((lr.SLOTS, lr.MAX_BUYS_PER_SCAN, lr.COOLDOWN_HOURS), (10, 1, 6.0))


class LaneTests(unittest.TestCase):
    def test_lane_attribution_and_isolation(self):
        sigs = [signal("a", 0, lane="swing_single_quote", score=40),
                signal("b", 10, lane="confirmed_continuation"),
                signal("c", 20, lane="temporal_recovery")]
        paths = {s["token"]: path((s_i * 10 + 5, 1.1)) for s_i, s in enumerate(sigs)}
        challengers = {c["id"]: c for c in lr.lane_challengers({})}
        everything = lr.simulate(sigs, paths, challengers["A_CURRENT_CHAMPION"]["accept"], as_of=T0 + timedelta(days=1))
        lanes = lr.by_lane(everything["trades"])
        self.assertEqual(set(lanes), {"swing_single_quote", "confirmed_continuation", "temporal_recovery"})
        self.assertTrue(all(m["trades"] == 1 for m in lanes.values()))
        a = everything["trades"][0]
        for key in ("buy_lane", "score", "quality", "impulse", "flow_15m", "flow_1h", "volume", "trade_count",
                    "buy_ratio", "m15", "m1h", "phase", "entry_price", "exit_outcome", "net_pnl", "mfe", "mae",
                    "holding_hours", "modeled_costs"):
            self.assertIn(key, a)
        self.assertEqual((a["buy_lane"], a["score"]), ("swing_single_quote", 40))
        only = lr.simulate(sigs, paths, challengers["C_SINGLE_QUOTE_ONLY"]["accept"], as_of=T0 + timedelta(days=1))
        self.assertEqual({t["buy_lane"] for t in only["trades"]}, {"swing_single_quote"})
        confirmed = lr.simulate(sigs, paths, challengers["B_CONFIRMED_ONLY"]["accept"], as_of=T0 + timedelta(days=1))
        self.assertNotIn("swing_single_quote", {t["buy_lane"] for t in confirmed["trades"]})

    def test_strict_single_quote_thresholds_from_train_only(self):
        train = [signal(f"s{i}", i, lane="swing_single_quote", score=30 + i) for i in range(6)]
        strict = lr.strict_thresholds(train)
        self.assertEqual(strict["min_score"], 33)
        accept = {c["id"]: c for c in lr.lane_challengers(strict)}["D_STRICT_SINGLE_QUOTE"]["accept"]
        self.assertFalse(accept(signal("x", 0, lane="swing_single_quote", score=31)))
        self.assertTrue(accept(signal("x", 0, lane="swing_single_quote", score=40)))
        self.assertTrue(accept(signal("x", 0, lane="confirmed_continuation", score=1)))
        self.assertEqual(lr.strict_thresholds([signal("y", 0, lane="confirmed_continuation")]), {})

    def test_metrics_capital_efficiency(self):
        sim = lr.simulate([signal("a", 0)], {"a": path((60 * 24, 1.6))}, lambda s: True,
                          as_of=T0 + timedelta(days=2))
        m = lr.lane_metrics(sim["trades"])
        self.assertEqual(m["trades"], 1)
        self.assertAlmostEqual(m["capital_sda_hours"], 20 * 24, places=0)
        self.assertAlmostEqual(m["pnl_per_100_sda_days"], m["net_pnl"] / 20 * 100, places=2)
        self.assertEqual(m["best_trade_contribution_pct"], 100.0)


class RecorderAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = self.base / "lane.json"
        self.positions = self.base / "positions.json"
        self.real = self.base / "real.json"
        self.positions.write_text(json.dumps({"positions": {f"0x{i}": {"status": "OPEN"} for i in range(10)}}))
        self.real.write_text(json.dumps({"positions": {f"0x{i}": {"amount_sda": 20.0} for i in range(10)},
                                         "pending_buys": {}}))

    def candidate(self, token, minutes, eligible=True, lane="swing_single_quote", price=1.0):
        at = T0 + timedelta(minutes=minutes)
        return {"address": token, "symbol": "S", "price_sda": price, "quote_at": (at - timedelta(seconds=20)).isoformat(),
                "eligible": eligible, "paper_entry_lane": lane if eligible else None, "score": 50, "quality": 50,
                "impulse": 0, "flow_15m": 10, "flow_1h": 200, "volume_1h": 300, "trades_1h": 3,
                "buy_ratio": 2, "m15": 0, "m1h": 1, "phase": "WATCH", "data_age_sec": 20}

    def record(self, rows, minutes):
        return lr.record_scan(rows, (T0 + timedelta(minutes=minutes)).isoformat(), state_file=self.state,
                              positions_file=self.positions, real_state_file=self.real)

    def test_recorder_tracks_lane_capacity_and_forward_quotes(self):
        self.record([self.candidate("0xaa", 0), self.candidate("0xbb", 0, eligible=False)], 0)
        self.record([self.candidate("0xaa", 0)], 1)
        state = self.record([self.candidate("0xaa", 10, eligible=False, price=1.2)], 10)
        sig = next(iter(state["signals"].values()))
        self.assertEqual((sig["lane"], sig["sightings"], sig["paper_full_sightings"],
                          sig["real_full_sightings"]), ("swing_single_quote", 2, 2, 2))
        self.assertEqual([p for _, p in state["paths"]["0xaa"]], [1.0, 1.2])
        self.assertNotIn("0xbb", state["paths"])
        signals, paths, live = lr.load_stream(self.state, self.base / "missing.json")
        blocked = lr._blocked(signals, live)
        self.assertEqual((blocked["blocked_paper_10_of_10"], blocked["eligible_while_real_10_of_10"]), (1, 1))

    def test_research_keeps_champion_and_real_unchanged(self):
        before = digest(REAL_FILES + ("paper_champion_config.json", "positions.json", "real_trade_state.json"))
        sys.modules.pop("real_trader", None)
        self.record([self.candidate("0xaa", 0)], 0)
        report = lr.research(T0 + timedelta(days=1), state_file=self.state, archive_file=self.base / "none.json",
                             positions_file=self.positions, dataset_dir=self.base / "paths")
        self.assertEqual(report["decision"], "KEEP CHAMPION")
        self.assertFalse(report["promotion_applied"])
        self.assertFalse(report["promotable"])
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertEqual(digest(REAL_FILES + ("paper_champion_config.json", "positions.json",
                                              "real_trade_state.json")), before)
        self.assertNotIn("real_trader", sys.modules)
        source = (ROOT / "tools" / "lane_research.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"import real_|from real_")
        self.assertIn("BUY LANE RESEARCH", lr.render_text(report))

    def test_empty_stream_is_insufficient_data(self):
        report = lr.research(T0, state_file=self.base / "a.json", archive_file=self.base / "b.json",
                             positions_file=self.positions, dataset_dir=self.base / "paths")
        self.assertEqual((report["status"], report["decision"]), ("INSUFFICIENT_DATA", "KEEP CHAMPION"))


if __name__ == "__main__":
    unittest.main()
