"""HOME REAL BUY SCANNER: shared signal counts vs current REAL (PAPER-coupled) admission.

Presentation only; every chain/trading entry point is forbidden.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import real_bot_menu as rb
import real_trader
import real_trading_config as cfg
import telegram_home

ROOT = Path(__file__).resolve().parents[1]
XEN = "0x53167ff5810ff1886fc554e672994c232126383e"


def _row(symbol, status, score=70, blocked=True, phase="WATCH", address=None):
    return {"address": address or "0x" + symbol.lower().ljust(40, "0")[:40], "symbol": symbol, "status": status,
            "decision": {"pump_score": score, "m15": 16.6, "net_1h": 1879, "paper_buy_blocked": blocked,
                         "pump_phase": phase, "paper_buy_block_reason": "debug gate text"}}


def _rows(*rows, age_seconds=30):
    result = telegram_home.ScannerRows(rows)
    result.updated_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    return result


def _position(cost, raw=10**20):
    return {"remaining_quantity": raw, "remaining_cost_basis": str(cost), "amount_sda": float(cost)}


def _state(count=8, cost=20, extra=None, pending=None):
    positions = {"0x" + f"{i + 1:040x}": _position(cost) for i in range(count)}
    positions.update(extra or {})
    return {"positions": positions, "pending_buys": pending or {}}


def _set_state(env, state):
    env.state = state
    rows = []
    for token, position in state.get("positions", {}).items():
        try:
            raw = Decimal(str(position.get("remaining_quantity", position.get("token_amount_raw", 0))))
            cost = Decimal(str(position.get("remaining_cost_basis", position.get("amount_sda", 0))))
            if raw <= 0 or cost <= Decimal("0.1"):
                continue
            rows.append({"token": token, "symbol": token[:8],
                         "quantity": str(raw / Decimal(10**18)),
                         "cost_basis_sda": str(cost), "value_sda": str(cost)})
        except Exception:
            continue
    env.files["real_accounting_state.json"] = {
        "schema_version": 1, "bot_reconciliation_status": "COMPLETE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "native_balance_sda": "1000", "open_positions": rows,
    }


@pytest.fixture
def env(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("blockchain/trading call from Telegram"))
    for name in ("send_transaction", "private_account", "quote_best_live_fee", "execute_buy", "rpc",
                 "persist_real_intent_before_broadcast"):
        monkeypatch.setattr(real_trader, name, forbidden)
    files = {"token_metadata.json": {XEN: {"symbol": "XEN", "decimals": 18}},
             "real_accounting_state.json": {
                 "schema_version": 1, "bot_reconciliation_status": "COMPLETE",
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "native_balance_sda": "1000", "open_positions": []},
             "positions.json": {"positions": {}},
             "market_source_state.json": {
                 "source_health": "HEALTHY",
                 "last_successful_market_at": datetime.now(timezone.utc).isoformat()},
             "whale_state.json": {"updated_at": datetime.now(timezone.utc).isoformat(),
                                  "last_successful_whale_at": datetime.now(timezone.utc).isoformat(),
                                  "whale_source_health": "HEALTHY"}}
    holder = SimpleNamespace(state=_state(), worker={"enabled": True, "dry_run": False,
                             "syncStatus": "FRESH",
                             "lastSuccessfulWalletSync": datetime.now(timezone.utc).isoformat(),
                             "updated_at": datetime.now(timezone.utc).isoformat()}, files=files,
                             forbidden=forbidden)
    monkeypatch.setattr(rb, "_state", lambda _d: holder.state)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: holder.worker)
    holder.dashboard = SimpleNamespace(load=lambda path, default: files.get(path, default))
    _set_state(holder, holder.state)
    return holder


def _home(env, monkeypatch, rows):
    monkeypatch.setattr(telegram_home, "scanner_candidates", lambda _d: rows)
    monkeypatch.setattr(telegram_home.real_bot_views, "_get_data", lambda _d: None)
    text = telegram_home.home_text(env.dashboard)
    env.forbidden.assert_not_called()
    return text


def test_capacity_counts_only_material_positions_and_actual_basis(env):
    dust = {"remaining_quantity": 55649068659320, "remaining_cost_basis": "0.0000035", "last_market_price": "0.068"}
    closed = {"remaining_quantity": 0, "remaining_cost_basis": "0"}
    _set_state(env, _state(count=6, cost=20, extra={XEN: dust, "0x" + "c" * 40: closed,
                                               "0x" + "d" * 40: _position(30), "0x" + "e" * 40: _position(24)}))
    real = telegram_home.real_capacity(env.dashboard)
    assert real["positions"] == 8
    assert real["deployed"] == pytest.approx(174.0)
    assert real["available"] == pytest.approx(126.0)
    assert real["cap"] == cfg.TRADING_WALLET_CAPITAL_SDA


def test_next_buy_and_cap_follow_running_worker_config_not_hardcoded(env, monkeypatch):
    env.worker = {**env.worker, "real_buy_size_sda": 25.0, "real_capital_cap_sda": 250.0,
                  "real_max_open_positions": 9}
    real = telegram_home.real_capacity(env.dashboard)
    assert (real["next_buy"], real["cap"], real["max_positions"]) == (25.0, 250.0, 9)
    env.worker = {**env.worker, "enabled": True, "dry_run": False, "real_buy_size_sda": None}
    monkeypatch.setattr(cfg, "POSITION_DEFAULT_SDA", 20.0)
    monkeypatch.setattr(cfg, "POSITION_MAX_SDA", 20.0)
    assert telegram_home.real_capacity(env.dashboard)["next_buy"] == 20.0
    source = (ROOT / "telegram_home.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(30|300|20|200)(\.0)?\b", source.replace("V30", ""))


def test_home_shows_real_buy_scanner_and_no_paper_summary(env, monkeypatch):
    text = _home(env, monkeypatch, _rows(_row("IDM", "WATCH", 77), _row("AGFU", "NO", 52)))
    assert "🔥 REAL BUY SCANNER" in text
    assert "🟢 BUY-ready   0" in text and "🟡 WATCH       1" in text
    assert "💰 Available   140 SDA" in text
    assert "💵 Next BUY    30 SDA" in text
    assert "🟡 REAL BUY: NO SIGNAL" in text
    assert "🧪 PAPER" not in text and "🎯" not in text and "IDM" not in text
    assert "PAPER" in [b["callback_data"] for row in telegram_home.home_keyboard()["inline_keyboard"] for b in row]


def test_ready_signal_with_capacity_shows_best_candidate(env, monkeypatch):
    rows = _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION"), _row("IDM", "WATCH", 77))
    text = _home(env, monkeypatch, rows)
    assert "🟢 BUY-ready   1" in text and "🟡 WATCH       1" in text
    assert "🟢 REAL BUY: READY" in text
    assert "🎯 ACV  89/100" in text and "CONFIRMATION | 15m +16.6% | Flow +1879 SDA" in text
    assert "debug gate text" not in text


def test_ready_signal_blocked_by_position_limit(env, monkeypatch):
    _set_state(env, _state(count=10, cost=1))
    text = _home(env, monkeypatch, _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION")))
    assert "🔴 REAL BUY: BLOCKED\nPosition limit" in text
    assert telegram_home.real_capacity(env.dashboard)["positions"] == 10


def test_ready_signal_blocked_by_capital(env, monkeypatch):
    _set_state(env, _state(count=8, cost=35))  # 280 + 30 > 300
    text = _home(env, monkeypatch, _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION")))
    assert "🔴 REAL BUY: BLOCKED\nCapital limit" in text
    assert "💰 Available   20 SDA" in text


def test_ready_signal_on_held_token_and_paper_capacity_are_blocked(env, monkeypatch):
    held = "0x" + "f" * 40
    _set_state(env, _state(count=7, extra={held: _position(20)}))
    rows = _rows(_row("HELD", "BUY", 90, blocked=False, phase="IGNITION", address=held))
    assert "Already held" in _home(env, monkeypatch, rows)
    _set_state(env, _state(count=7))
    env.files["positions.json"] = {"positions": {f"p{i}": {} for i in range(10)}}
    assert "PAPER capacity" in _home(env, monkeypatch, rows)


def test_pending_buy_is_pending(env, monkeypatch):
    _set_state(env, _state(count=8, pending={"0x" + "a" * 40: {"position_sda": 30.0}}))
    text = _home(env, monkeypatch, _rows(_row("IDM", "WATCH", 77)))
    assert "🟡 REAL BUY: PENDING" in text
    assert "💰 Available   110 SDA" in text
    capacity = telegram_home.real_capacity(env.dashboard)
    assert capacity["positions"] == 8
    assert capacity["reserved_positions"] == 9


def test_stale_scanner_shows_data_stale_without_counts_or_candidate(env, monkeypatch):
    rows = _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION"), age_seconds=3600)
    text = _home(env, monkeypatch, rows)
    assert "🔥 REAL BUY SCANNER\n⚪ Data stale" in text
    assert "BUY-ready" not in text and "WATCH" not in text and "ACV" not in text
    assert "⚪ REAL BUY: DATA STALE" in text
    unknown = telegram_home.ScannerRows([_row("ACV", "BUY", 89, blocked=False)])
    assert telegram_home._scanner_stale(unknown)


def test_whale_source_failure_fails_closed_and_recovers_automatically(env, monkeypatch):
    rows = _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION"))
    env.files["whale_state.json"] = {
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "last_successful_whale_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "whale_source_health": "SOURCE_UNAVAILABLE",
        "last_error_http_status": 521,
    }
    degraded = _home(env, monkeypatch, rows)
    assert "⚪ Data stale" in degraded
    assert "REAL BUY: DATA STALE" in degraded
    assert "BUY-ready" not in degraded

    env.files["whale_state.json"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_successful_whale_at": datetime.now(timezone.utc).isoformat(),
        "whale_source_health": "HEALTHY",
    }
    recovered = _home(env, monkeypatch, rows)
    assert "⚪ Data stale" not in recovered
    assert "REAL BUY: READY" in recovered

    env.files.pop("market_source_state.json")
    env.worker = {**env.worker, "market_source_health": "STALE"}
    worker_degraded = _home(env, monkeypatch, rows)
    assert "⚪ Data stale" in worker_degraded
    assert "REAL BUY: DATA STALE" in worker_degraded


def test_market_source_failure_fails_closed_and_recovers_automatically(env, monkeypatch):
    rows = _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION"))
    env.files["market_source_state.json"] = {
        "source_health": "SOURCE_UNAVAILABLE",
        "last_successful_market_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    degraded = _home(env, monkeypatch, rows)
    assert "⚪ Data stale" in degraded
    assert "REAL BUY: DATA STALE" in degraded
    assert "BUY-ready" not in degraded

    env.files["market_source_state.json"] = {
        "source_health": "HEALTHY",
        "last_successful_market_at": datetime.now(timezone.utc).isoformat(),
    }
    recovered = _home(env, monkeypatch, rows)
    assert "⚪ Data stale" not in recovered
    assert "REAL BUY: READY" in recovered


def test_watch_is_never_counted_or_labelled_buy_ready(env, monkeypatch):
    rows = _rows(_row("IDM", "WATCH", 99, blocked=True, phase="WATCH"))
    assert telegram_home._counts(rows) == (0, 1)
    detail = (monkeypatch.setattr(telegram_home, "scanner_candidates", lambda _d: rows)
              or telegram_home.buy_scanner_text(env.dashboard))
    assert "1. IDM    WATCH 99/100" in detail and "🟢 BUY" not in detail and "REAL: " not in detail


def test_status_mirrors_shared_engine_buy_decision():
    assert telegram_home._status({"paper_buy_blocked": False, "pump_phase": "WATCH"}) == "BUY"
    assert telegram_home._status({"paper_buy_blocked": True, "pump_phase": "CONFIRMATION"}) == "NO"
    assert telegram_home._status({"paper_buy_blocked": True, "pump_phase": "WATCH"}) == "WATCH"


def test_buy_scanner_detail_shows_real_gate_per_ready_signal(env, monkeypatch):
    _set_state(env, _state(count=8, cost=35))
    rows = _rows(_row("ACV", "BUY", 89, blocked=False, phase="CONFIRMATION"), _row("IDM", "WATCH", 77))
    monkeypatch.setattr(telegram_home, "scanner_candidates", lambda _d: rows)
    text = telegram_home.buy_scanner_text(env.dashboard)
    assert "1. ACV    🟢 BUY 89/100" in text
    assert "REAL: BLOCKED — CAPITAL LIMIT" in text
    assert "Status: BLOCKED — Capital limit" in text
    assert "Production admission: PAPER-coupled" in text
    detail = telegram_home.buy_scanner_text(env.dashboard, detail=True)
    assert detail.startswith("🔍 REAL BUY SCANNER • DETAIL") and "debug gate text" in detail
    env.forbidden.assert_not_called()


def test_trading_strategy_modules_unchanged_by_presentation():
    import swing_sell_v2 as policy
    from decimal import Decimal
    assert (policy.LOSS_EXIT_PNL, policy.PARTIAL_TRIGGER_PNL, policy.HARD_TP_PNL) == (
        Decimal("-0.50"), Decimal("0.25"), Decimal("0.50"))
    source = (ROOT / "telegram_home.py").read_text(encoding="utf-8")
    for forbidden in ("execute_buy", "send_transaction", "evaluate_position", "paper_champion_config"):
        assert forbidden not in source



def test_stale_worker_heartbeat_blocks_buy_even_with_fresh_wallet_snapshot(env):
    env.worker["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    _set_state(env, _state(count=8))
    _rows, real, stale = telegram_home._scanner_inputs(env.dashboard)
    assert stale is True
    assert real is None
    assert telegram_home.real_capacity(env.dashboard) is None
