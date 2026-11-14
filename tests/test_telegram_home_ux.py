"""Telegram UX cleanup: concise home, BUY Scanner, no automatic dashboard push, read-only navigation."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import api.hourly as hourly_api
import api.index as telegram_api
import engine_legacy
import real_bot_menu as rb
import real_bot_views as views
import real_swing_sell_executor
import real_trader
import telegram_home

ROOT = Path(__file__).resolve().parents[1]
BOT_TOKEN = "0x" + "5" * 40
PINE = "0x" + "8" * 40


def _accounting():
    return {
        "schema_version": 1, "bot_reconciliation_status": "COMPLETE", "bot_unresolved_transactions": [],
        "realized_pnl_sda": "4.25",
        "buy_transactions": [{"tx": "0xbuy", "at": "2026-11-09T08:00:00Z", "symbol": "BOT", "contract": BOT_TOKEN,
                              "invested_sda": "20", "quantity": "100"}],
        "sell_transactions": [{"tx": "0xsell", "at": "2026-11-10T08:00:00Z", "symbol": "BOT", "contract": BOT_TOKEN,
                               "net_pnl_sda": "4.25", "proceeds_sda": "24.25", "cost_basis_sda": "20",
                               "quantity": "100", "buy_tx": ["0xbuy"]}],
        "open_positions": [{"token": BOT_TOKEN, "symbol": "BOT", "quantity": "100", "value_sda": "21.5",
                            "cost_basis_sda": "20", "open_pnl_sda": "1.5"}],
        "excluded_external_activity": {"unmatched_sells": [{"sell_tx": "0xpine", "token": "PINE"}],
                                       "unknown_basis_assets": [{"symbol": "PINE", "contract": PINE}]},
        "statistics": {"confirmed_buys_count": 1, "matched_sells_count": 1, "wins": 1, "losses": 0},
    }


@pytest.fixture
def real_data(monkeypatch):
    accounting = _accounting()
    source = SimpleNamespace(load=lambda path, default: accounting if path == "real_accounting_state.json" else default)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: {
        "enabled": True, "dry_run": False, "syncStatus": "FRESH",
        "lastSuccessfulWalletSync": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "real_max_open_positions": 10})
    monkeypatch.setattr(rb, "_status", lambda _w: "🟢 LIVE")
    snapshot = {"native_sda": 100.0, "wallet_address": rb.BOT_WALLET_ADDRESS, "token_balances_loaded": True,
                "holdings": [{"address": BOT_TOKEN, "symbol": "BOT", "market_value_sda": "21.5"}],
                "tracked_expected_count": 1, "tracked_missing_contracts": [], "market_complete": True}
    monkeypatch.setattr(views, "_tracked_wallet_snapshot", lambda _a, _d=None: snapshot)
    data = views._build_dashboard_data(source)
    views.clear_cache()
    monkeypatch.setattr(views, "_build_dashboard_data", lambda _d: data)
    yield data
    views.clear_cache()


def _candidate(symbol, status, score, m15, flow, blocked=True, phase="WATCH"):
    return {"address": "0x" + symbol.lower().ljust(40, "0")[:40], "symbol": symbol, "status": status,
            "decision": {"pump_score": score, "m15": m15, "net_1h": flow, "paper_buy_blocked": blocked,
                         "pump_phase": phase, "m1h": 1.0, "volume_1h": 900, "trades_1h": 12, "buy_ratio": 1.4,
                         "paper_buy_block_reason": "waiting for flow confirmation"}}


def _rows(*rows, age_seconds=30):
    result = telegram_home.ScannerRows(rows)
    result.updated_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    return result


def _real(positions=8, max_positions=10, deployed=160.0, cap=300.0, next_buy=30.0, pending=0,
          held=(), paper_held=(), paper_full=False):
    return {"positions": positions, "max_positions": max_positions, "deployed": deployed, "cap": cap,
            "available": max(0.0, cap - deployed), "next_buy": next_buy, "pending": pending,
            "held": set(held), "paper_held": set(paper_held), "paper_full": paper_full}


@pytest.fixture
def ui(monkeypatch, real_data):
    monkeypatch.delenv("CHAT_ID", raising=False)
    dashboard = telegram_api.dashboard
    original_load = dashboard.load
    now = datetime.now(timezone.utc).isoformat()
    states = {
        "whale_state.json": {"updated_at": now, "last_successful_whale_at": now,
                             "whale_source_health": "HEALTHY"},
        "market_source_state.json": {"source_health": "HEALTHY",
                                     "last_successful_market_at": now},
    }
    monkeypatch.setattr(dashboard, "load", lambda path, default: (
        states[path] if path in states else original_load(path, default)))
    rows = _rows(_candidate("ACV", "WATCH", 89, 16.6, 1879), _candidate("GMAI", "WATCH", 65, 14.8, 4))
    monkeypatch.setattr(telegram_home, "scanner_candidates", lambda _d: rows)
    monkeypatch.setattr(telegram_home, "real_capacity", lambda _d: _real())
    events = []
    monkeypatch.setattr(dashboard, "api", lambda method, payload: events.append((method, payload)) or {"ok": True})
    monkeypatch.setattr(dashboard, "answer_callback", lambda *a, **k: events.append(("answer", a)))
    forbidden = Mock(side_effect=AssertionError("trading side effect from Telegram"))
    for name in ("send_transaction", "private_account", "quote_best_live_fee", "execute_buy",
                 "persist_real_intent_before_broadcast"):
        monkeypatch.setattr(real_trader, name, forbidden)
    monkeypatch.setattr(real_swing_sell_executor, "evaluate_position", forbidden)
    monkeypatch.setattr(real_swing_sell_executor, "reconcile_position", forbidden)
    return SimpleNamespace(dashboard=dashboard, events=events, forbidden=forbidden, counter=[100])


def press(ui, data):
    ui.counter[0] += 1
    ui.dashboard.handle_update({"update_id": ui.counter[0], "callback_query": {
        "id": f"cb{ui.counter[0]}", "data": data, "message": {"message_id": 1, "chat": {"id": 7}}}})
    edits = [payload for method, payload in ui.events if method == "editMessageText"]
    assert edits, f"no screen rendered for {data}"
    return edits[-1]


def callbacks(screen):
    return [button["callback_data"] for row in screen["reply_markup"]["inline_keyboard"] for button in row]


def labels(screen):
    return [button["text"] for row in screen["reply_markup"]["inline_keyboard"] for button in row]


def test_automatic_dashboard_push_disabled(monkeypatch):
    text = (ROOT / ".github/workflows/telegram_hourly.yml").read_text(encoding="utf-8")
    triggers = text.split("\npermissions:", 1)[0]
    assert "schedule:" not in triggers and "cron:" not in triggers
    assert '"$REPORT_REASON" = "Cronjob hourly"' in text and "skip=true" in text
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=AssertionError("dispatch attempted")))
    for app in (telegram_api.app, hourly_api.app):
        response = TestClient(app).get("/api/hourly")
        assert response.status_code == 200
        assert "disabled" in response.text


def test_interactive_home_is_concise(ui):
    screen = press(ui, "MAIN")
    text = screen["text"]
    assert text.startswith("🤖 SDA TRADING BOT")
    assert "🟢 REAL Trading LIVE" in text
    for expected in ("💰 Portfolio", "📈 Bot PnL", "+5.75 SDA", "📊 Open PnL", "+1.50 SDA",
                     "💵 Realized", "+4.25 SDA", "📦 Positions", "🔥 REAL BUY SCANNER",
                     "🟢 BUY-ready   0", "🟡 WATCH       2", "📦 Positions   1/10",
                     "💰 Available   140 SDA", "💵 Next BUY    30 SDA", "🟡 REAL BUY: NO SIGNAL"):
        assert expected in text
    assert "🧪 PAPER" not in text
    for removed in ("TOP BUY", "POSITION ACTION", "REAL WALLET", "Historical realized P/L", "TOTAL WALLET VALUE",
                    "P/L SUMMARY", "Shadow READY", "blocked:", "ACV", "PINE", "🎯"):
        assert removed not in text
    assert callbacks(screen) == ["REAL_BOT", "BUY_SCANNER", "REAL_BOT_PNL", "PAPER", "OPTIMIZER", "REFRESH"]
    assert "🧪 PAPER" in labels(screen)
    assert not any("Debug" in label or "Real Wallet" in label for label in labels(screen))
    assert "DEBUG" not in callbacks(screen) and "REAL" not in callbacks(screen)


def test_webhook_main_renders_home(ui):
    response = TestClient(telegram_api.app).post("/api/telegram", json={"update_id": 1, "callback_query": {
        "id": "cb", "data": "MAIN", "message": {"message_id": 1, "chat": {"id": 7}}}})
    assert response.status_code == 200
    edits = [p for m, p in ui.events if m == "editMessageText"]
    assert edits[-1]["text"].startswith("🤖 SDA TRADING BOT")


def test_real_trading_menu_and_all_submenus_with_back(ui):
    real = press(ui, "REAL_BOT")
    assert real["text"].startswith("🤖 REAL TRADING 🟢")
    assert callbacks(real) == ["REAL_BOT_PORTFOLIO", "REAL_BOT_SELL_V2", "REAL_BOT_BUY", "REAL_BOT_SELL",
                               "REAL_BOT_STATS", "REAL_BOT_PNL", "REAL_BOT_REFRESH", "MAIN"]
    checks = {"REAL_BOT_PORTFOLIO": "📊 PORTFOLIO", "REAL_BOT_BUY": "🟢 CONFIRMED BOT BUY HISTORY",
              "REAL_BOT_SELL": "🔴 MATCHED BOT SELL HISTORY", "REAL_BOT_STATS": "📈 REAL STATISTICS",
              "REAL_BOT_PNL": "📈 REAL PNL", "REAL_BOT_PNL_DAILY": "📅 DAILY PNL",
              "REAL_BOT_PNL_WEEKLY": "📆 WEEKLY PNL", "REAL_BOT_PNL_MONTHLY": "🗓 MONTHLY PNL"}
    for callback, title in checks.items():
        screen = press(ui, callback)
        assert screen["text"].startswith(title), callback
        assert "REAL_BOT_BACK" in callbacks(screen) or "REAL_BOT_PNL" in callbacks(screen)
        back = press(ui, "REAL_BOT_BACK")
        assert back["text"].startswith("🤖 REAL TRADING")
    assert press(ui, "MAIN")["text"].startswith("🤖 SDA TRADING BOT")


def test_sell_v2_view_is_read_only(ui, monkeypatch):
    monkeypatch.setattr(views, "_sell_v2_text", lambda _d: "🎯 SELL V2 • READ ONLY\n\nBOT HOLD")
    screen = press(ui, "REAL_BOT_SELL_V2")
    assert screen["text"].startswith("🎯 SELL V2")
    assert "REAL_BOT_BACK" in callbacks(screen)


def test_portfolio_shows_only_bot_positions(ui):
    press(ui, "REAL_BOT")
    text = press(ui, "REAL_BOT_PORTFOLIO")["text"]
    assert "BOT" in text and "PINE" not in text
    assert "POSITION ACTION" not in text


def test_buy_scanner_short_list_detail_and_back(ui):
    screen = press(ui, "BUY_SCANNER")
    text = screen["text"]
    assert text.startswith("🔥 REAL BUY SCANNER")
    assert "BUY-ready: 0" in text and "WATCH: 2" in text
    for expected in ("SHARED SIGNAL", "REAL\nAvailable: 140 SDA", "Next BUY: 30 SDA",
                     "Status: NO SIGNAL", "Production admission: PAPER-coupled"):
        assert expected in text
    assert "1. ACV" in text and "89/100" in text and "15m +16.6% | Flow +1879 SDA" in text
    assert "waiting for flow confirmation" not in text and "REAL: READY" not in text
    assert callbacks(screen) == ["BUY_SCANNER_DETAIL", "BUY_SCANNER", "MAIN"]
    detail = press(ui, "BUY_SCANNER_DETAIL")
    assert detail["text"].startswith("🔍 REAL BUY SCANNER • DETAIL")
    assert "waiting for flow confirmation" in detail["text"]
    assert "MAIN" in callbacks(detail)
    assert press(ui, "MAIN")["text"].startswith("🤖 SDA TRADING BOT")


def test_refresh_rerenders_home_without_report_dispatch(ui, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=AssertionError("hourly report dispatched")))
    screen = press(ui, "REFRESH")
    assert screen["text"].startswith("🤖 SDA TRADING BOT")
    assert "Hourly Report" not in screen["text"]
    assert press(ui, "REAL_BOT_REFRESH")["text"].startswith("🤖 REAL TRADING")


def test_legacy_callbacks_from_old_messages_do_not_break(ui):
    assert press(ui, "REAL")["text"].startswith("🤖 REAL TRADING")
    debug = press(ui, "DEBUG")
    assert "MAIN" in callbacks(debug)


def test_navigation_never_triggers_trading(ui):
    for data in ("MAIN", "REAL_BOT", "REAL_BOT_PORTFOLIO", "REAL_BOT_BUY", "REAL_BOT_SELL", "REAL_BOT_STATS",
                 "REAL_BOT_PNL", "REAL_BOT_PNL_DAILY", "BUY_SCANNER", "BUY_SCANNER_DETAIL", "REFRESH",
                 "REAL_BOT_REFRESH", "REAL_BOT_BACK"):
        press(ui, data)
    ui.forbidden.assert_not_called()


def test_legacy_renderer_kept_for_debugging():
    assert callable(telegram_api.dashboard.legacy_main_dashboard)
    assert callable(telegram_api.dashboard.market_debug_report)


def test_real_and_error_alert_paths_are_not_disabled(monkeypatch):
    posts = []
    monkeypatch.setattr(engine_legacy, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(engine_legacy, "CHAT_ID", "7")
    monkeypatch.setattr(engine_legacy.requests, "post", lambda url, json=None, timeout=None: posts.append(json))
    engine_legacy.send("PAPER TRADING ENGINE ERROR")
    assert posts == [{"chat_id": "7", "text": "PAPER TRADING ENGINE ERROR"}]
    whale = (ROOT / "whale_scanner.py").read_text(encoding="utf-8")
    assert "WHALE SCANNER ERROR" in whale and "sendMessage" in whale
    scanner_workflow = (ROOT / ".github/workflows/scanner_v18.yml").read_text(encoding="utf-8")
    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in scanner_workflow
