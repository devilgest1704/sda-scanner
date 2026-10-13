"""Regression tests for the read-only optimizer Telegram menu."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import optimizer_dashboard as od
import telegram_callback_router


AUTO = {
    "updated_at": "2026-10-11T07:42:00+00:00",
    "source_last_scan_at": "2026-10-11T07:40:00+00:00",
    "status": "COLLECTING_FORWARD_TRADES",
    "reason": "Not enough chronological outcomes",
    "champion": {"generation": 0, "filters": {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.1, "trades": 5}},
    "strategies": {
        "baseline": {"all": {"closed": 5, "net_sda": -4.50, "win_rate_pct": 20, "profit_factor": 0.39}},
        "momentum": {"all": {"closed": 2, "net_sda": 0.52, "win_rate_pct": 50, "profit_factor": 1.2}},
        "research_watch": {"all": {"closed": 0, "net_sda": 0}},
    },
    "learning_data_quality": {"baseline": {"valid_closed": 5, "excluded_closed": 4},
                              "momentum": {"valid_closed": 2, "excluded_closed": 3}},
    "promotion_applied": False, "rollback_applied": False,
}
CONFIG = {"generation": 0, "filters": AUTO["champion"]["filters"]}
RESEARCH = {
    "generated_at": "2026-10-11T05:42:00+00:00", "status": "READY", "candidates_tested": 3000,
    "v30_compatible_rows": 281,
    "champion": {"test": {"pnl_sda": -95.50, "closed": 57, "profit_factor": 0.38, "win_rate_pct": 24.56}},
    "challenger": {"test": {"pnl_sda": -50.87, "closed": 25, "profit_factor": 0.33, "win_rate_pct": 24},
                   "safe_runtime_filters": {"entry_score": 76, "quality": 52, "impulse": 8, "buy_ratio": 1.1, "trades": 15},
                   "recommend_promotion": False},
    "promotion_applied": False,
}


class OptimizerDashboardTests(unittest.TestCase):
    def test_overview_separates_shadow_from_actual_and_no_autopromotion(self):
        result = od.overview_report(AUTO, CONFIG)
        self.assertIn("5", result)
        self.assertIn("-4.50 SDA", result)
        self.assertIn("generace 0", result)
        self.assertIn("Nový Champion: NE", result)
        self.assertIn("PAPER ONLY", result)
        self.assertLess(len(result), 3900)

    def test_strategy_list_shows_each_policy_and_data_quality(self):
        result = od.strategies_report(AUTO)
        self.assertIn("baseline", result)
        self.assertIn("momentum", result)
        self.assertIn("research_watch (výzkum)", result)
        self.assertIn("vyřazeno 4", result)
        self.assertLess(len(result), 3900)

    def test_breakout_strategy_is_labeled_research_only(self):
        from copy import deepcopy
        data = deepcopy(AUTO)
        data["strategies"]["research_breakout_volume_trend"] = {
            "all": {"closed": 2, "net_sda": 3.21, "win_rate_pct": 50,
                    "profit_factor": 1.4}
        }
        txt = od.strategies_report(data)
        self.assertIn("research_breakout_volume_trend (V32 Breakout výzkum)", txt)
        self.assertIn("automaticky nenasazují", txt)
        self.assertIn("+3.21 SDA", txt)

    def test_macd_shadow_variants_are_labeled_research_only(self):
        from copy import deepcopy
        data = deepcopy(AUTO)
        data["strategies"]["research_macd_antipump"] = {
            "all": {"closed": 0, "net_sda": 0}
        }
        data["strategies"]["research_macd_confirmation"] = {
            "all": {"closed": 2, "net_sda": -2.50, "win_rate_pct": 0}
        }
        report = od.strategies_report(data)
        self.assertIn("research_macd_antipump (V32 MACD výzkum)", report)
        self.assertIn("research_macd_confirmation (V32 MACD výzkum)", report)
        self.assertIn("automaticky nenasazují", report)

    def test_trend_runner_shown_as_nonpromotable_research(self):
        from copy import deepcopy
        data = deepcopy(AUTO)
        data["strategies"]["research_trend_runner"] = {
            "all": {"closed": 2, "net_sda": 4.75, "win_rate_pct": 50,
                    "profit_factor": 1.31}
        }
        result = od.strategies_report(data)
        self.assertIn("research_trend_runner (V32 Trend Runner výzkum)",result)
        self.assertIn("+4.75 SDA",result)
        self.assertIn("se automaticky nenasazují",result)

    def test_research_is_labeled_non_promoting(self):
        result = od.research_report(RESEARCH)
        self.assertIn("3 000" if False else "3000", result)
        self.assertIn("-50.87 SDA", result)
        self.assertIn("Doporučení k nasazení: NE", result)
        self.assertIn("automaticky NENASAZUJE", result)

    def test_missing_live_data_never_looks_like_zero_profit(self):
        text = od.overview_report(None, None)
        self.assertIn("nelze načíst", text)
        self.assertNotIn("+0.00 SDA", text)
        self.assertIn("nedostupný", od._time("invalid"))
        with self.assertRaises(ValueError):
            od.load_report("../../private")

    def test_stale_warning_has_clear_timestamp(self):
        from datetime import datetime, timezone
        note = od._stale_notice("2026-10-11T06:00:00+00:00",
                                datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc))
        self.assertIn("starší", note)


class OptimizerMenuCallbackTests(unittest.TestCase):
    def _setup_dashboard(self, authorized="321"):
        actions = []
        def api(method, payload):
            actions.append((method, payload))
            return {"ok": True}
        d = SimpleNamespace(
            api=api,
            answer_callback=lambda ident, text=None: actions.append(("answer", ident)),
            optimizer_overview_report=lambda: "overview report",
            optimizer_strategies_report=lambda: "strategy report",
            optimizer_research_report=lambda: "research report",
            optimizer_menu_keyboard=lambda: {"inline_keyboard": [[{"text": "Back", "callback_data": "MAIN"}]]},
            main_dashboard=lambda: "main",
            menu_keyboard=lambda: {"inline_keyboard": []},
            back_keyboard=lambda: {"inline_keyboard": []},
            handle_update=lambda update, state: state,
        )
        telegram_callback_router.patch_dashboard(d)
        return d, actions

    def _click(self, d, callback, chat="321"):
        update = {"update_id": 9, "callback_query": {
            "id": "test", "data": callback,
            "message": {"chat": {"id": chat}, "message_id": 12}}}
        return d.handle_update(update, {"offset": 0})

    def test_optimizer_buttons_return_reports_not_trades(self):
        d, actions = self._setup_dashboard()
        with patch.dict(os.environ, {"CHAT_ID": "321"}):
            for data, expected in (("OPTIMIZER", "overview report"),
                                   ("OPTIMIZER_STRATEGIES", "strategy report"),
                                   ("OPTIMIZER_V30", "research report")):
                actions.clear()
                self.assertEqual(self._click(d, data)["offset"], 10)
                edits = [payload for method, payload in actions if method == "editMessageText"]
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0]["text"], expected)
                self.assertEqual(edits[0]["message_id"], 12)
                self.assertIn("reply_markup", edits[0])
                self.assertFalse(any(m == "sendMessage" for m, _ in actions))

    def test_unauthorized_optimizer_click_is_ignored(self):
        d, actions = self._setup_dashboard()
        with patch.dict(os.environ, {"CHAT_ID": "321"}):
            self._click(d, "OPTIMIZER", chat="999")
        self.assertFalse(any(m == "editMessageText" for m, _ in actions))


if __name__ == "__main__":
    unittest.main()
