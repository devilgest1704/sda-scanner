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
    import real_bot_views
    dashboard = _router(monkeypatch)
    calls = []
    def render(_dashboard, view, refresh=False, cache_only=False):
        assert dashboard.events[0] == ("answer", "cb-1", None)
        calls.append((view, refresh, cache_only))
        return "BOT MAIN", real_bot_views._main_keyboard()
    monkeypatch.setattr(real_bot_views, "render_view", render)
    dashboard.handle_update(_update("REAL_BOT"))
    assert calls == [("main", False, False)]
    assert _last_edit(dashboard)["text"] == "BOT MAIN"
    callbacks = [b["callback_data"] for row in _last_edit(dashboard)["reply_markup"]["inline_keyboard"] for b in row]
    assert callbacks == ["REAL_BOT_PORTFOLIO", "REAL_BOT_BUY", "REAL_BOT_SELL", "REAL_BOT_STATS", "REAL_BOT_REFRESH", "MAIN"]


@pytest.mark.parametrize("callback,view", [
    ("REAL_BOT_PORTFOLIO", "portfolio"), ("REAL_BOT_BUY", "buy"),
    ("REAL_BOT_SELL", "sell"), ("REAL_BOT_STATS", "stats"),
])
def test_real_bot_submenus_are_acknowledged_and_rendered(monkeypatch, callback, view):
    import real_bot_views
    dashboard = _router(monkeypatch)
    calls = []
    def render(_dashboard, selected_view, refresh=False, cache_only=False):
        assert dashboard.events[0] == ("answer", "cb-1", None)
        calls.append((selected_view, refresh, cache_only))
        return selected_view.upper(), real_bot_views._submenu_keyboard(selected_view)
    monkeypatch.setattr(real_bot_views, "render_view", render)
    dashboard.handle_update(_update(callback))
    assert calls == [(view, False, True)]
    assert _last_edit(dashboard)["text"] == view.upper()


@pytest.mark.parametrize("callback", ["REAL_BOT_PORTFOLIO", "REAL_BOT_BUY", "REAL_BOT_SELL", "REAL_BOT_STATS"])
def test_back_from_every_submenu_uses_cached_data(monkeypatch, callback):
    import real_bot_views
    dashboard = _router(monkeypatch)
    calls = []
    def render(_dashboard, view, refresh=False, cache_only=False):
        calls.append((view, refresh, cache_only))
        return view.upper(), real_bot_views._main_keyboard() if view == "main" else real_bot_views._submenu_keyboard(view)
    monkeypatch.setattr(real_bot_views, "render_view", render)
    dashboard.handle_update(_update(callback, 10))
    dashboard.handle_update(_update("REAL_BOT_BACK", 11))
    expected = {"REAL_BOT_PORTFOLIO":"portfolio","REAL_BOT_BUY":"buy","REAL_BOT_SELL":"sell","REAL_BOT_STATS":"stats"}[callback]
    assert calls == [(expected, False, True), ("main", False, True)]
    assert _last_edit(dashboard)["text"] == "MAIN"


@pytest.mark.parametrize("callback,view", [
    ("REAL_BOT_REFRESH", "main"), ("REAL_BOT_PORTFOLIO_REFRESH", "portfolio"),
    ("REAL_BOT_STATS_REFRESH", "stats"),
])
def test_real_bot_refresh_forces_fresh_data_after_immediate_ack(monkeypatch, callback, view):
    import real_bot_views
    dashboard = _router(monkeypatch)
    calls = []
    def render(_dashboard, selected_view, refresh=False, cache_only=False):
        assert dashboard.events[0] == ("answer", "cb-1", None)
        calls.append((selected_view, refresh, cache_only))
        return "FRESH", real_bot_views._main_keyboard() if selected_view == "main" else real_bot_views._submenu_keyboard(selected_view)
    monkeypatch.setattr(real_bot_views, "render_view", render)
    dashboard.handle_update(_update(callback))
    assert calls == [(view, True, False)]
    assert _last_edit(dashboard)["text"] == "FRESH"


def test_missing_ledger_or_quote_failure_keeps_real_menu_navigable(monkeypatch):
    import real_bot_views
    def fail(*_args, **_kwargs):
        raise OSError("private Supabase/RPC detail must not be shown")
    monkeypatch.setattr(real_bot_views, "render_view", fail)
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL_BOT"))
    payload = _last_edit(dashboard)
    assert "temporarily unavailable" in payload["text"]
    assert "Supabase" not in payload["text"] and "RPC detail" not in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "REAL_BOT_BACK"


def test_callback_is_acknowledged_before_slow_real_report(monkeypatch):
    import real_bot_views
    dashboard = _router(monkeypatch)
    def slow_report(_dashboard, view, refresh=False, cache_only=False):
        assert dashboard.events[0] == ("answer", "cb-1", None)
        return "REAL REPORT", real_bot_views._main_keyboard()
    monkeypatch.setattr(real_bot_views, "render_view", slow_report)
    dashboard.handle_update(_update("REAL_BOT"))
    assert _last_edit(dashboard)["text"] == "REAL REPORT"


def test_real_bot_menu_has_required_six_actions(monkeypatch):
    import real_bot_views
    monkeypatch.setattr(real_bot_views, "render_view", lambda *_a, **_k: ("REAL", real_bot_views._main_keyboard()))
    dashboard = _router(monkeypatch)
    dashboard.handle_update(_update("REAL_BOT", 23))
    buttons = [button for row in _last_edit(dashboard)["reply_markup"]["inline_keyboard"] for button in row]
    assert [button["callback_data"] for button in buttons] == ["REAL_BOT_PORTFOLIO","REAL_BOT_BUY","REAL_BOT_SELL","REAL_BOT_STATS","REAL_BOT_REFRESH","MAIN"]
