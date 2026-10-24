import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tools.forward_shadow_agent import default_state
from tools.forward_shadow_optimizer import report, paired_trend_runner_exit_impact


NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
BASE = {"version": "V30-AUTO-CONFIG-1", "generation": 0,
        "filters": {"entry_score": 68, "quality": 52, "impulse": 8,
                    "buy_ratio": 1.1, "trades": 5},
        "previous_filters": None, "promotion": None}


def samples(positive=True):
    state = default_state()
    state["updated_at"] = NOW.isoformat()
    first = NOW - timedelta(days=3)
    for i in range(70):
        date = first + timedelta(hours=i)
        pnl = 2.0 if i < 49 or positive else -4.0
        state["strategies"]["quality"]["trades"].append({
            "address": "0x" + str(i % 9), "opened_at": (date-timedelta(minutes=10)).isoformat(),
            "closed_at": date.isoformat(), "valid_for_learning": True,
            "pnl_sda": pnl
        })
        # A different contender with poor TRAIN must not win solely because
        # its holdout contains enormous lucky trades.
        state["strategies"]["momentum"]["trades"].append({
            "address": "0x" + str(i % 9), "opened_at": (date-timedelta(minutes=10)).isoformat(),
            "closed_at": date.isoformat(), "valid_for_learning": True,
            "pnl_sda": -2.0 if i < 49 else 100.0
        })
    return state


class ForwardOptimizerTests(unittest.TestCase):
    def setUp(self):
        # Legacy numerical Champion tests explicitly exercise baseline mode.
        # Production defaults to temporal_recovery and must not promote those
        # baseline threshold configurations into the active temporal strategy.
        env = patch.dict("os.environ", {"SDA_PAPER_BUY_MODE": "baseline"})
        env.start()
        self.addCleanup(env.stop)

    def test_report_exposes_observed_exit_evidence_without_promotion(self):
        opened = (NOW - timedelta(minutes=4)).isoformat()
        observed = (NOW - timedelta(minutes=3)).isoformat()
        valid = {"status": "CLOSED", "v30_mode": "CLEAN-PUMP-HUNTER-V30.3",
                 "opened_at": opened,
                 "closed_at": (NOW - timedelta(minutes=2, seconds=59)).isoformat(),
                 "entry_price": 1.0, "close_price": 0.96,
                 "close_reason": "V30.3 EMERGENCY/HARD STOP",
                 "pump_price_samples": [{
                     "at": observed,
                     "quote_at": (NOW - timedelta(minutes=3, seconds=30)).isoformat(),
                     "quote_age_seconds": 30, "price_sda": 0.96,
                     "score": 75, "m15": 1, "flow_1h": 250}]}
        invalid = copy.deepcopy(valid)
        invalid["pump_price_samples"][0].pop("quote_at")
        with patch.dict("os.environ", {"SDA_PAPER_BUY_MODE": "temporal_recovery"}):
            result, unchanged = report(samples(), copy.deepcopy(BASE),
                                       {"closed_trades": [valid, invalid]}, NOW)
        audit = result["runtime_parity"]["observed_paper_exit_audit"]
        self.assertEqual(audit["audited"], 2)
        self.assertEqual(audit["matched"], 1)
        self.assertEqual(audit["excluded"], 1)
        self.assertFalse(audit["can_unlock_paper_autopromotion"])
        self.assertFalse(result["runtime_parity"]["verified"])
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(unchanged, BASE)

    def test_temporal_mode_rejects_numeric_champion_promotion(self):
        state = samples()
        state["strategies"]["research_temporal_recovery"]["trades"] = [
            {**r, "pnl_sda": 5.0} for r in
            state["strategies"]["quality"]["trades"][:6]
        ]
        with patch.dict("os.environ", {"SDA_PAPER_BUY_MODE": "temporal_recovery"}):
            with patch("tools.forward_shadow_optimizer.RUNTIME_EXECUTION_PARITY_VERIFIED", True):
                result, unchanged = report(state, copy.deepcopy(BASE),
                                           {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "ACTIVE_TEMPORAL_MODE_RESEARCH_ONLY")
        self.assertEqual(result["active_paper_buy_mode"], "temporal_recovery")
        self.assertEqual(result["incumbent_policy"], "research_temporal_recovery")
        self.assertEqual(result["shadow_comparison_reference"], "baseline")
        self.assertFalse(result["optimizer_targets_active_buy"])
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(result["active_paper_evidence"]["valid_forward_closes"], 6)
        self.assertEqual(unchanged, BASE)

    def test_can_promote_after_positive_forward_holdout(self):
        # A validated parity fixture proves the optimizer math can still
        # promote in isolation. Production does NOT have parity approval.
        with patch("tools.forward_shadow_optimizer.RUNTIME_EXECUTION_PARITY_VERIFIED", True):
            result, conf = report(samples(), copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "PROMOTED_PAPER")
        self.assertTrue(result["promotion_applied"])
        self.assertEqual(conf["generation"], 1)
        self.assertEqual(conf["filters"]["entry_score"], 72)
        self.assertEqual(conf["previous_filters"], BASE["filters"])
        self.assertEqual(result["selected_challenger"]["name"], "quality")
        self.assertFalse(result["real_trading"])

    def test_positive_shadow_results_cannot_promote_unmatched_runtime(self):
        result, conf = report(samples(), copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "RUNTIME_PARITY_UNVERIFIED")
        self.assertFalse(result["promotion_applied"])
        self.assertFalse(result["runtime_parity"]["verified"])
        self.assertEqual(conf, BASE)
        self.assertEqual(result["selected_challenger"]["name"], "quality")

    def test_bad_holdout_rejects_challenger(self):
        result, conf = report(samples(positive=False), copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "HOLDOUT_REJECTED")
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(conf["generation"], 0)

    def test_outcomes_with_no_quote_quality_not_learned(self):
        state = samples()
        for row in state["strategies"]["quality"]["trades"]:
            row["valid_for_learning"] = False
        result, _ = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertNotEqual(result["status"], "PROMOTED_PAPER")

    def test_low_quote_coverage_blocks_paper_promotion(self):
        state = samples()
        for row in state["strategies"]["quality"]["trades"][:12]:
            row["valid_for_learning"] = False
        result, conf = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertFalse(result["promotion_applied"])
        self.assertLess(result["quote_coverage_pct"]["quality"], 90.0)
        self.assertEqual(conf["generation"], 0)

    def test_paired_trend_runner_credits_only_identical_forward_entries(self):
        def row(symbol, opened, pnl):
            return {"address": symbol,
                    "opened_at": opened, "closed_at": opened,
                    "pnl_sda": pnl, "valid_for_learning": True}
        base = [row("0xa", "2026-10-13T07:00:00Z", 3),
                row("0xb", "2026-10-13T08:00:00Z", -4)]
        runner = [row("0xa", "2026-10-13T07:00:00Z", 5),
                  row("0xb", "2026-10-13T09:00:00Z", 30)]
        x = paired_trend_runner_exit_impact(base, runner)
        self.assertEqual(x["paired_closed_trades"], 1)
        self.assertEqual(x["runner_minus_baseline_sda"], 2)
        self.assertEqual(x["unpaired_baseline_valid"], 1)
        self.assertEqual(x["unpaired_runner_valid"], 1)
        self.assertFalse(x["can_promote_champion"])

    def test_paired_comparison_excludes_duplicate_entry_identifiers(self):
        def r(p):
            return {"address": "0xa", "opened_at": "2026-10-13T07:00:00Z",
                    "pnl_sda":p}
        x = paired_trend_runner_exit_impact([r(1), r(2)], [r(999)])
        self.assertEqual(x["paired_closed_trades"], 0)
        self.assertEqual(x["runner_minus_baseline_sda"], 0)

    def test_trend_runner_cannot_promote_even_if_exceptionally_profitable(self):
        state = samples()
        state["strategies"]["research_trend_runner"]["trades"] = [
            {**row, "pnl_sda": 500.0}
            for row in state["strategies"]["quality"]["trades"]
        ]
        with patch("tools.forward_shadow_optimizer.RUNTIME_EXECUTION_PARITY_VERIFIED", True):
            result, updated = report(state, copy.deepcopy(BASE),
                                     {"closed_trades": []}, NOW)
        self.assertEqual(result["selected_challenger"]["name"], "quality")
        self.assertFalse(result["v32_trend_runner"]["may_promote_champion"])
        self.assertEqual(result["v32_trend_runner"]["mode"], "RESEARCH_ONLY")
        self.assertFalse(result["real_trading"])

    def test_v32_breakout_cannot_become_champion_even_if_profitable(self):
        state = samples()
        state["strategies"]["research_breakout_volume_trend"]["trades"] = [
            {**row, "pnl_sda": 999.0}
            for row in state["strategies"]["quality"]["trades"]
        ]
        with patch("tools.forward_shadow_optimizer.RUNTIME_EXECUTION_PARITY_VERIFIED", True):
            result, new_config = report(state, copy.deepcopy(BASE),
                                        {"closed_trades": []}, NOW)
        self.assertEqual(result["selected_challenger"]["name"], "quality")
        self.assertEqual(result["v32_breakout"]["mode"], "RESEARCH_ONLY")
        self.assertFalse(result["v32_breakout"]["may_promote_champion"])
        self.assertNotIn("research_breakout", result["selected_challenger"]["name"])
        self.assertFalse(result["real_trading"])

    def test_v32_macd_arms_cannot_be_promoted_even_if_highly_profitable(self):
        state = samples()
        for arm in ("research_macd_antipump", "research_macd_confirmation"):
            state["strategies"][arm]["trades"] = [
                {**r, "pnl_sda": 999.0}
                for r in state["strategies"]["quality"]["trades"]
            ]
        with patch("tools.forward_shadow_optimizer.RUNTIME_EXECUTION_PARITY_VERIFIED", True):
            result, conf = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["selected_challenger"]["name"], "quality")
        self.assertNotIn("research_macd", result["selected_challenger"]["name"])
        self.assertFalse(result["real_trading"])

    def test_new_research_arms_can_be_added_to_old_journal(self):
        state = samples()
        state["strategies"].pop("research_macd_antipump")
        state["strategies"].pop("research_macd_confirmation")
        result, _ = report(state, copy.deepcopy(BASE),
                           {"closed_trades": []}, NOW)
        self.assertIn("research_macd_antipump", state["strategies"])
        self.assertIn("research_macd_confirmation", state["strategies"])
        self.assertEqual(result["v32_macd"]["mode"], "RESEARCH_ONLY")

    def test_research_watch_cannot_become_champion(self):
        state = samples()
        state["strategies"]["research_watch"]["trades"] = [
            {**r, "pnl_sda": 100.0} for r in state["strategies"]["quality"]["trades"]
        ]
        for row in state["strategies"]["quality"]["trades"]:
            row["pnl_sda"] = -2.0
        result, conf = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(conf["generation"], 0)

    def test_stale_scanner_refuses_promotion(self):
        state = samples()
        state["updated_at"] = (NOW - timedelta(hours=1)).isoformat()
        result, _ = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "WAITING_FOR_FRESH_SCANNER")

    def test_rollback_on_new_actual_paper_losses(self):
        config = copy.deepcopy(BASE)
        config.update({
            "generation": 1, "filters": {"entry_score": 72, "quality": 56, "impulse": 8,
                                         "buy_ratio": 1.2, "trades": 8},
            "previous_filters": BASE["filters"],
            "promotion": {"at": (NOW-timedelta(hours=3)).isoformat()}
        })
        trades = []
        for i in range(8):
            at = NOW-timedelta(hours=2, minutes=50-i)
            trades.append({"status": "CLOSED", "opened_at": at.isoformat(),
                           "closed_at": (at+timedelta(minutes=5)).isoformat(),
                           "address": "0x"+str(i), "closed_profit_sda": -4})
        result, new = report(samples(), config, {"closed_trades": trades}, NOW)
        self.assertEqual(result["status"], "ROLLED_BACK_PAPER")
        self.assertTrue(result["rollback_applied"])
        self.assertEqual(new["filters"], BASE["filters"])
        self.assertEqual(new["generation"], 2)

    def test_challenger_must_beat_current_champion_not_obsolete_baseline(self):
        state = samples()
        quality = state["strategies"]["quality"]["trades"]
        for row in quality:
            row["pnl_sda"] = 6.0
        state["strategies"]["score"]["trades"] = [
            {**r, "pnl_sda": 4.0} for r in quality
        ]
        state["strategies"]["baseline"]["trades"] = [
            {**r, "pnl_sda": -3.0} for r in quality
        ]
        config = copy.deepcopy(BASE)
        config["filters"] = state["strategies"]["quality"]["filters"].copy()
        result, updated = report(state, config, {"closed_trades": []}, NOW)
        self.assertEqual(result["incumbent_policy"], "quality")
        self.assertEqual(result["selected_challenger"]["name"], "score")
        self.assertEqual(result["status"], "HOLDOUT_REJECTED")
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(updated["filters"], config["filters"])

    def test_unrecognized_current_champion_fails_closed(self):
        state = samples()
        config = copy.deepcopy(BASE)
        config["filters"]["entry_score"] = 71
        result, updated = report(state, config, {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "UNKNOWN_INCUMBENT_POLICY")
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(updated, config)

    def test_diagnostics_explain_excluded_forward_evidence(self):
        state = default_state()
        state["updated_at"] = NOW.isoformat()
        state["strategies"]["baseline"]["trades"] = [
            {"valid_for_learning": False, "learning_exclusion_reasons":
             ["quote_gap_over_30m", "holding_time_over_4_5h"]},
            {"valid_for_learning": False, "reason": "STALE_TIMEOUT"},
            {"valid_for_learning": False},
        ]
        result, unchanged = report(state, copy.deepcopy(BASE),
                                   {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "COLLECTING_FORWARD_TRADES")
        q = result["learning_data_quality"]["baseline"]
        self.assertEqual(q["total_closed"], 3)
        self.assertEqual(q["excluded_closed"], 3)
        self.assertEqual(q["excluded_by_reason"]["quote_gap_over_30m"], 1)
        self.assertEqual(q["excluded_by_reason"]["holding_time_over_4_5h"], 1)
        self.assertEqual(q["excluded_by_reason"]["stale_timeout_no_observed_exit_quote"], 1)
        self.assertEqual(q["excluded_by_reason"]["legacy_unknown_quote_quality"], 1)
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(unchanged["generation"], 0)

    def test_insufficient_events_no_autopromotion(self):
        state = default_state()
        state["updated_at"] = NOW.isoformat()
        result, conf = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "COLLECTING_FORWARD_TRADES")
        self.assertEqual(conf["generation"], 0)


if __name__ == "__main__":
    unittest.main()
