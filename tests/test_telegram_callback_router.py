import os
from types import SimpleNamespace

import pytest

import telegram_callback_router


class FakeDashboard:
    def __init__(self):
        self.events = []
        self.handle_update = lambda update, state=None: state or {"offset": 0}

    def answer_callback(self, callback_id, text=None):
        self.events.append(("answer", callback_id, text))

    def api(self, method, payload):
        self.events.append((method, payload))
        return {"ok": True, "result": {"message_id": 99}}

    def main_dashboard(self):
        self.events.append(("render_main",))
        return "MAIN"

    def menu_keyboard(self):
        return {"inline_keyboard": [[{"text": "REAL Trading", "callback_data": "REAL_BOT"}]]}

    def real_trading_report(self):
        self.events.append(("render_real",))
        return "REAL"

    def real_statistics_report(self):
        self.events.append(("render_real_stats",))
        return "REAL STATS"

    def back_keyboard(self):
        return {"inline_keyboard": [[{"text": "Back", "callback_data": "MAIN"}]]}

    def optimizer_menu_keyboard(self):
        return self.back_keyboard()

    def optimizer_overview_report(self):
        return "OPTIMIZER"

    def optimizer_strategies_report(self):
        return "STRATEGIES"

    def optimizer_research_report(self):
        return "RESEARCH"


def _update(data, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": data,
            "message": {"message_id": 10, "chat": {"id": 12345}},
        },
    }


def _router(monkeypatch):
    monkeypatch.delenv("CHAT_ID", raising=False)
    dashboard = FakeDashboard()
    telegram_callback_router.patch_dashboard(dashboard)
    return dashboard


def _last_edit(dashboard):
    edits = [event for event in dashboard.events if event[0] == "editMessageText"]
    assert edits
    return edits[-1][1]


def test_main_menu_callback_is_acknowledged_and_rendered(monkeypatch):
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("MAIN"))
    assert dashboard.events[0] == ("answer", "cb-1", None)
    assert _last_edit(dashboard)["text"] == "MAIN"


def test_real_trading_wallet_menu_callback_is_reachable(monkeypatch):
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL"))
    assert dashboard.events[0][0] == "answer"
    assert ("render_real",) in dashboard.events
    assert _last_edit(dashboard)["text"] == "REAL"


def test_real_trading_bot_button_is_explicitly_routed(monkeypatch):
    import real_bot_menu
    monkeypatch.setattr(real_bot_menu, "bot_report", lambda _d: "BOT REPORT")
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL_BOT"))
    assert dashboard.events[0][0] == "answer"
    payload = _last_edit(dashboard)
    assert payload["text"] == "BOT REPORT"
    callbacks = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert callbacks == ["REAL_BOT_REFRESH", "MAIN"]


def test_back_from_real_bot_returns_to_main_menu(monkeypatch):
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("MAIN", 2))
    assert _last_edit(dashboard)["text"] == "MAIN"
    assert dashboard.events[0] == ("answer", "cb-2", None)


def test_refresh_callback_is_acknowledged_exactly_once(monkeypatch):
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REFRESH"))
    assert len([event for event in dashboard.events if event[0] == "answer"]) == 1
    assert _last_edit(dashboard)["reply_markup"] == dashboard.menu_keyboard()


def test_missing_ledger_or_quote_failure_keeps_real_menu_navigable(monkeypatch):
    import real_bot_menu
    monkeypatch.setattr(real_bot_menu, "bot_report", lambda _d: (_ for _ in ()).throw(OSError("ledger/quote unavailable")))
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL_BOT"))
    payload = _last_edit(dashboard)
    assert "temporarily unavailable" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "MAIN"


@pytest.mark.parametrize("warning", [
    "⚠ REAL ledger unavailable",
    "⚠ REAL ledger stale",
    "⚪ Worker status unknown",
    "⚪ STATUS UNKNOWN",
    "⚠ Sell quote unavailable",
])
def test_missing_stale_worker_and_valuation_warnings_do_not_remove_buttons(monkeypatch, warning):
    import real_bot_menu
    monkeypatch.setattr(real_bot_menu, "bot_report", lambda _d: warning)
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL_BOT"))
    payload = _last_edit(dashboard)
    assert warning in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"]


def test_callback_is_acknowledged_before_slow_real_report(monkeypatch):
    import real_bot_menu

    dashboard = _router(monkeypatch)

    def slow_report(_dashboard):
        assert dashboard.events[0] == ("answer", "cb-1", None)
        return "REAL REPORT"

    monkeypatch.setattr(real_bot_menu, "bot_report", slow_report)
    dashboard.handle_update(_update("REAL_BOT"))
    assert _last_edit(dashboard)["text"] == "REAL REPORT"


def test_failed_edit_falls_back_to_send_message(monkeypatch):
    dashboard = _router(monkeypatch)
    calls = []

    def api(method, payload):
        calls.append(method)
        return {"ok": method == "sendMessage"}

    dashboard.api = api
    dashboard.handle_update(_update("MAIN"))
    assert calls == ["editMessageText", "sendMessage"]
