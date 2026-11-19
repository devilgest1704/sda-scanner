import json
import unittest
from datetime import UTC, datetime, timedelta

import real_stagnant_profit as policy

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
PRICE_TIME = NOW.isoformat()
TREND = {"fresh": True, "bullish": False, "state": "FLAT"}
HISTORY = [
    {"timestamp": (NOW - timedelta(hours=h)).isoformat(), "price": "1",
     "pnl_pct": pnl}
    for h, pnl in ((24, 4.0), (18, 4.1), (12, 4.2), (6, 4.3), (0, 4.3))
]


def position(**changes):
    value = {"opened_at": (NOW - timedelta(hours=100)).isoformat(),
             "sellManagementEnabled": True, "source": "AUTO"}
    value.update(changes)
    return value


def evaluate(pos=None, *, pnl=5.0, history=None, trend=TREND,
             net=1.0, timestamp=PRICE_TIME, pending=False):
    return policy.evaluate(pos or position(), now=NOW, price="1",
                           price_timestamp=timestamp,
                           history=HISTORY if history is None else history,
                           trend=trend, current_pnl_pct=pnl,
                           net_exit_pnl_sda=net, pending_sell=pending)


class MaxHoldStagnantProfitTests(unittest.TestCase):
    def test_window_start_includes_overlapping_sample_bucket_maximum(self):
        history = [
            {"timestamp": (NOW-timedelta(hours=24, minutes=2)).isoformat(),
             "latest_timestamp": (NOW-timedelta(hours=23, minutes=58)).isoformat(),
             "start_pnl_pct": 4.0, "pnl_pct": 4.2, "max_pnl_pct": 5.5},
            {"timestamp": (NOW-timedelta(hours=18)).isoformat(), "price": "1", "pnl_pct": 4.2},
            {"timestamp": (NOW-timedelta(hours=12)).isoformat(), "price": "1", "pnl_pct": 4.2},
            {"timestamp": (NOW-timedelta(hours=6)).isoformat(), "price": "1", "pnl_pct": 4.2},
            {"timestamp": NOW.isoformat(), "price": "1", "pnl_pct": 4.2},
        ]
        result = evaluate(history=history)
        self.assertEqual(result["blocker"], "PROFIT_IMPROVED")
        self.assertEqual(result["improvement_pp"], 1.5)

    def test_a_positive_stagnant_candidate(self):
        history = [{"timestamp": (NOW-timedelta(hours=h)).isoformat(),
                    "price": "1", "pnl_pct": p} for h,p in
                   ((24,5.0),(18,5.1),(12,5.2),(6,5.3),(0,5.3))]
        self.assertEqual(evaluate(history=history)["decision"], "MAX_HOLD_STAGNANT_PROFIT")

    def test_b_low_but_positive_profit_is_allowed(self):
        history = [{"timestamp": (NOW-timedelta(hours=h)).isoformat(),
                    "price": "1", "pnl_pct": p} for h,p in
                   ((24,.2),(18,.3),(12,.4),(6,.5),(0,.5))]
        self.assertEqual(evaluate(pnl=.5, history=history)["decision"], "MAX_HOLD_STAGNANT_PROFIT")

    def test_c_net_exit_nonpositive_holds(self):
        self.assertEqual(evaluate(net=0)["blocker"], "NET_EXIT_NOT_PROFITABLE")

    def test_d_loss_never_uses_max_hold_even_at_300h(self):
        old_loss = position(opened_at=(NOW-timedelta(hours=300)).isoformat())
        self.assertEqual(evaluate(old_loss, pnl=-1)["blocker"], "NON_POSITIVE_PNL")

    def test_e_age_under_72h_holds(self):
        self.assertEqual(evaluate(position(opened_at=(NOW-timedelta(hours=48)).isoformat()))["blocker"],
                         "MIN_HOLD_NOT_REACHED")

    def test_f_improvement_two_pp_holds(self):
        history = [{"timestamp": (NOW-timedelta(hours=h)).isoformat(),
                    "price": "1", "pnl_pct": p} for h,p in
                   ((24,4),(18,5),(12,6),(6,6),(0,6))]
        self.assertEqual(evaluate(history=history)["blocker"], "PROFIT_IMPROVED")

    def test_g_bullish_four_hour_momentum_holds(self):
        context = {
            "_snapshot_updated_at": NOW.isoformat(),
            "momentum": {"1h_pct": 0.0, "4h_pct": 1.2},
            "flow": {"1h": {"net_flow": 0, "transactions": 0},
                     "4h": {"net_flow": 0, "transactions": 0}},
            "technical": {
                "1h": {"available": True, "last_timestamp": (NOW-timedelta(hours=1)).isoformat()},
                "4h": {"available": True, "last_timestamp": (NOW-timedelta(hours=4)).isoformat()},
            },
        }
        trend = policy.assess_trend(context, now=NOW)
        self.assertTrue(trend["bullish"])
        self.assertEqual(evaluate(trend=trend)["blocker"], "POSITIVE_MOMENTUM")

    def test_bullish_technical_trend_label_blocks_exit(self):
        context = {
            "_snapshot_updated_at": NOW.isoformat(),
            "momentum": {"1h_pct": 0.0, "4h_pct": 0.0},
            "flow": {"1h": {"net_flow": 0, "transactions": 0},
                     "4h": {"net_flow": 0, "transactions": 0}},
            "technical": {
                "1h": {"available": True, "last_timestamp": (NOW-timedelta(hours=1)).isoformat(),
                       "trend": "BULLISH"},
                "4h": {"available": True, "last_timestamp": (NOW-timedelta(hours=4)).isoformat(),
                       "trend": "BEARISH"},
            },
        }
        trend = policy.assess_trend(context, now=NOW)
        self.assertTrue(trend["bullish"])
        self.assertEqual(trend["technical_trend_1h"], "BULLISH")

    def test_bullish_macd_histogram_recovery_blocks_exit(self):
        context = {
            "_snapshot_updated_at": NOW.isoformat(),
            "momentum": {"1h_pct": 0.0, "4h_pct": 0.0},
            "flow": {"1h": {"net_flow": 0, "transactions": 0},
                     "4h": {"net_flow": 0, "transactions": 0}},
            "technical": {
                "1h": {"available": True, "last_timestamp": (NOW-timedelta(hours=1)).isoformat(),
                       "macd": {"bullish": False, "histogram": -0.01, "histogram_rising": True}},
                "4h": {"available": True, "last_timestamp": (NOW-timedelta(hours=4)).isoformat()},
            },
        }
        trend = policy.assess_trend(context, now=NOW)
        self.assertTrue(trend["bullish"])
        self.assertEqual(trend["macd_state"], "BULLISH_RISING")

    def test_h_stale_trend_candles_hold(self):
        context = {
            "_snapshot_updated_at": NOW.isoformat(),
            "momentum": {"1h_pct": 0.0, "4h_pct": 0.0},
            "flow": {"1h": {"net_flow": 0, "transactions": 0},
                     "4h": {"net_flow": 0, "transactions": 0}},
            "technical": {
                "1h": {"available": True, "last_timestamp": (NOW-timedelta(hours=3)).isoformat()},
                "4h": {"available": True, "last_timestamp": (NOW-timedelta(hours=4)).isoformat()},
            },
        }
        trend = policy.assess_trend(context, now=NOW)
        self.assertFalse(trend["fresh"])
        self.assertEqual(evaluate(trend=trend)["blocker"], "STALE_TREND")

    def test_trend_snapshot_uses_existing_20_minute_freshness_limit(self):
        context = {
            "_snapshot_updated_at": (NOW-timedelta(minutes=10)).isoformat(),
            "momentum": {"1h_pct": 0.0, "4h_pct": 0.0},
            "flow": {"1h": {"net_flow": 0, "transactions": 0},
                     "4h": {"net_flow": 0, "transactions": 0}},
            "technical": {
                "1h": {"available": True, "last_timestamp": (NOW-timedelta(hours=1)).isoformat()},
                "4h": {"available": True, "last_timestamp": (NOW-timedelta(hours=4)).isoformat()},
            },
        }
        self.assertTrue(policy.assess_trend(context, now=NOW)["fresh"])

    def test_i_stale_price_holds(self):
        self.assertEqual(evaluate(timestamp=(NOW-timedelta(seconds=301)).isoformat())["blocker"],
                         "STALE_PRICE")

    def test_sell_management_disabled_blocks_rule(self):
        self.assertEqual(evaluate(position(sellManagementEnabled=False))["blocker"],
                         "SELL_MANAGEMENT_DISABLED")

    def test_j_partial_disables_rule(self):
        self.assertEqual(evaluate(position(profit_partial_done=True))["blocker"],
                         "PARTIAL_ALREADY_DONE")

    def test_k_trailing_disables_rule(self):
        self.assertEqual(evaluate(position(trailing_armed=True))["blocker"],
                         "TRAILING_ALREADY_ARMED")

    def test_l_restart_keeps_persistent_history(self):
        state = {"real_stagnation_history": {"token:p1": {
            "token": "token", "position_id": "p1",
            "observations": [{"timestamp": (NOW-timedelta(hours=h)).isoformat(),
                              "price": "1", "pnl_pct": p}
                             for h,p in ((24,4),(18,4.1),(12,4.2),(6,4.3),(0,4.3))]
        }}}
        restored = json.loads(json.dumps(state))
        rows = next(iter(restored["real_stagnation_history"].values()))["observations"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(evaluate(history=rows)["decision"], "MAX_HOLD_STAGNANT_PROFIT")

    def test_max_pnl_is_preserved_within_sampling_bucket(self):
        pos = position(position_id="p1")
        state = {"positions": {"token": pos}}
        first = policy.record_observation(
            state, "token", pos, "1.02", 2.5,
            (NOW-timedelta(minutes=1)).isoformat(), now=NOW,
        )
        second = policy.record_observation(
            state, "token", pos, "1.01", 2.0,
            NOW.isoformat(), now=NOW,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[-1]["pnl_pct"], 2.0)
        self.assertEqual(second[-1]["start_pnl_pct"], 2.5)
        self.assertEqual(second[-1]["max_pnl_pct"], 2.5)
        self.assertEqual(second[-1]["price"], "1.01")
        self.assertEqual(second[-1]["timestamp"], (NOW-timedelta(minutes=1)).isoformat())
        self.assertEqual(second[-1]["latest_timestamp"], NOW.isoformat())
        third = policy.record_observation(
            state, "token", pos, "1.03", 2.8,
            (NOW+timedelta(minutes=5)).isoformat(), now=NOW+timedelta(minutes=5),
        )
        self.assertEqual(len(third), 2)

    def test_stale_observation_is_ignored(self):
        state = {"positions": {"token": position(position_id="p1")}}
        rows = policy.record_observation(
            state, "token", position(position_id="p1"), "1", 4.0,
            (NOW-timedelta(seconds=301)).isoformat(), now=NOW,
        )
        self.assertEqual(rows, [])
        self.assertNotIn("real_stagnation_history", state)

    def test_history_registry_is_globally_bounded(self):
        state = {"positions": {}}
        for index in range(55):
            token = f"token-{index}"
            pos = position(position_id=f"p{index}")
            state["positions"][token] = pos
            state.setdefault("real_stagnation_history", {})[f"{token}:p{index}"] = {
                "token": token, "position_id": f"p{index}",
                "observations": [{"timestamp": (NOW-timedelta(minutes=5)).isoformat(),
                                  "price": "1", "pnl_pct": 1.0}],
            }
        policy.record_observation(
            state, "token-0", state["positions"]["token-0"], "1", 1.0,
            NOW.isoformat(), now=NOW,
        )
        self.assertLessEqual(len(state["real_stagnation_history"]), policy.MAX_HISTORY_POSITIONS)

    def test_manual_history_is_separated_by_original_open_time(self):
        first = {"source": "MANUAL", "position_id": "token", "opened_at": (NOW-timedelta(hours=100)).isoformat()}
        second = {"source": "MANUAL", "position_id": "token", "opened_at": (NOW-timedelta(hours=20)).isoformat()}
        first["history_position_id"] = f"MANUAL:{first['opened_at']}"
        second["history_position_id"] = f"MANUAL:{second['opened_at']}"
        state = {"positions": {"token": first}}
        policy.record_observation(state, "token", first, "1", 2.0, PRICE_TIME, now=NOW)
        state["positions"]["token"] = second
        policy.record_observation(
            state, "token", second, "1", 2.0,
            (NOW+timedelta(seconds=1)).isoformat(), now=NOW+timedelta(seconds=1),
        )
        records = state["real_stagnation_history"]
        self.assertEqual(len(records), 1)
        self.assertIn(second["history_position_id"], next(iter(records)))

    def test_m_manual_and_n_auto_use_same_policy(self):
        for source in ("MANUAL", "AUTO"):
            self.assertEqual(evaluate(position(source=source))["decision"],
                             "MAX_HOLD_STAGNANT_PROFIT")

    def test_o_pending_sell_blocks_duplicate(self):
        self.assertEqual(evaluate(pending=True)["blocker"], "PENDING_SELL_INTENT")

    def test_p_successful_decision_is_full_exit_reason(self):
        result = evaluate()
        self.assertEqual(result["decision"], "MAX_HOLD_STAGNANT_PROFIT")
        self.assertIsNone(result["blocker"])


if __name__ == "__main__":
    unittest.main()
