from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import real_bot_menu


class Dashboard:
    REMOTE_BASE = "https://example.invalid/"


def _holding(address, symbol, amount, value, price):
    return {
        "address": address, "symbol": symbol,
        "amount_raw": int(Decimal(str(amount)) * 10**18),
        "decimals": 18, "amount": Decimal(str(amount)),
        "price_sda": Decimal(str(price)),
        "price_timestamp": "2026-11-08T16:30:00+00:00",
        "price_age_seconds": 60, "price_stale": False,
        "market_value_sda": Decimal(str(value)), "market_error": None,
    }


def _fixture(monkeypatch, positions=None, holdings=None, native=100.0, realized=5.0):
    positions = positions or {
        "0x" + "a" * 40: {"symbol": "AAA", "amount_sda": 20},
        "0x" + "b" * 40: {"symbol": "BBB", "amount_sda": 20},
        "0x" + "c" * 40: {"symbol": "RIDEX", "amount_sda": 20},
    }
    holdings = holdings or [
        _holding("0x" + "a" * 40, "AAA", 100, 22, 0.22),
        _holding("0x" + "b" * 40, "BBB", 100, 18, 0.18),
        _holding(real_bot_menu.cfg.WSDA_ADDRESS.lower(), "WSDA", 10, 10, 1),
        _holding("0x" + "d" * 40, "RESIDUAL", 5, 5, 1),
    ]
    state = {
        "positions": positions,
        "wallet_realized_pnl_sda": realized,
        "trades": [{"action": "SELL", "realized_pnl_sda": realized}],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    snapshot = {
        "wallet_address": real_bot_menu.BOT_WALLET_ADDRESS,
        "native_sda": native, "holdings": holdings,
        "token_balances_loaded": True, "market_complete": True, "errors": [],
    }
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (state, "authoritative", None))
    monkeypatch.setattr(real_bot_menu, "_load_worker_status", lambda _d: {
        "enabled": True, "dry_run": False, "updated_at": datetime.now(timezone.utc).isoformat()
    })
    monkeypatch.setattr(real_bot_menu, "_fetch_wallet_snapshot", lambda: snapshot)
    real_bot_menu._DASHBOARD_CACHE.update({"at": 0, "value": None})
    return state, snapshot


def test_compact_dashboard_portfolio_includes_native_wsda_and_residual_assets(monkeypatch):
    _fixture(monkeypatch)
    report = real_bot_menu.bot_report(Dashboard(), refresh=True)
    assert "Portfolio  155.00 SDA" in report
    assert "Total PnL  +5.00 SDA" in report
    assert "Open PnL  +0.00 SDA" in report
    assert "Realized  +5.00 SDA" in report
    assert "Positions  2" in report


def test_closed_ridex_is_not_rendered_as_open_position(monkeypatch):
    _fixture(monkeypatch)
    report = real_bot_menu.bot_report(Dashboard(), refresh=True)
    assert "RIDEX" not in report
    assert "AAA 22.00 SDA +2.00" in report
    assert "BBB 18.00 SDA -2.00" in report


def test_unavailable_token_price_does_not_break_dashboard(monkeypatch):
    holdings = [
        _holding("0x" + "a" * 40, "AAA", 100, 22, 0.22),
        {"address": "0x" + "b" * 40, "symbol": "BBB", "amount_raw": 10**18,
         "amount": Decimal(1), "price_sda": None, "market_value_sda": None,
         "market_error": "rpc error should not leak"},
    ]
    _fixture(monkeypatch, holdings=holdings)
    report = real_bot_menu.bot_report(Dashboard(), refresh=True)
    assert "AAA 22.00 SDA +2.00" in report
    assert "BBB value unavailable" in report
    assert "rpc error" not in report
    assert "Portfolio  ≥122.00 SDA (1/2)" in report
    assert "Total PnL  unavailable" in report


def test_daily_and_partial_trade_history_are_not_mislabeled_as_wallet_realized_pnl():
    state = {
        "daily_realized_pnl_sda": -0.10,
        "trades": [
            {"action": "SELL", "realized_pnl_sda": 4.5},
            {"action": "SELL", "pnl_sda": 2.0},
        ],
    }
    result = real_bot_menu._fast_ledger_realized_summary(state)
    assert result["verified_pnl_sda"] is None


def test_explicit_cumulative_wallet_realized_field_is_used():
    result = real_bot_menu._fast_ledger_realized_summary({
        "daily_realized_pnl_sda": -0.10,
        "wallet_realized_pnl_sda": 90.0727,
        "trades": [{"action": "SELL", "realized_pnl_sda": 4.5}],
    })
    assert result["verified_pnl_sda"] == 90.0727


def test_wallet_snapshot_does_not_request_executable_sell_quotes(monkeypatch):
    token = "0x" + "a" * 40
    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def get(url, **kwargs):
        if url.endswith("/token-balances"):
            return Response([{"token": {"address_hash": token, "symbol": "AAA", "decimals": "18"},
                              "value": str(2 * 10**18)}])
        return Response({"coin_balance": str(10 * 10**18)})
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: real_bot_menu.BOT_WALLET_ADDRESS)
    monkeypatch.setattr(real_bot_menu.requests, "get", get)
    import real_trader
    monkeypatch.setattr(real_trader, "prepare_sell", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SELL quote called")))
    monkeypatch.setattr(real_bot_menu, "_newest_supabase_price", lambda _token: {
        "price_sda": 0.5, "timestamp": "2026-11-08T16:30:00+00:00",
        "age_seconds": 60, "stale": False, "error": None,
    })
    snapshot = real_bot_menu._fetch_wallet_snapshot()
    assert snapshot["native_sda"] == 10
    assert snapshot["holdings"][0]["market_value_sda"] == Decimal(1)


def test_ledger_failure_still_renders_navigation_safe_dashboard(monkeypatch):
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (None, "unavailable", "offline"))
    monkeypatch.setattr(real_bot_menu, "_load_worker_status", lambda _d: None)
    real_bot_menu._DASHBOARD_CACHE.update({"at": 0, "value": None})
    report = real_bot_menu.bot_report(Dashboard(), refresh=True)
    assert "REAL TRADING" in report
    assert "Portfolio  unavailable" in report
    assert "OPEN POSITIONS" in report


def test_dashboard_cache_is_bypassed_by_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(real_bot_menu, "_build_bot_report", lambda _d: calls.append(1) or "fresh")
    real_bot_menu._DASHBOARD_CACHE.update({"at": __import__("time").monotonic(), "value": "cached"})
    assert real_bot_menu.bot_report(Dashboard()) == "cached"
    assert real_bot_menu.bot_report(Dashboard(), refresh=True) == "fresh"
    assert len(calls) == 1



def test_stale_ledger_does_not_produce_current_open_or_realized_pnl(monkeypatch):
    state, _snapshot = _fixture(monkeypatch)
    state["updated_at"] = "2026-11-08T03:33:39+00:00"
    monkeypatch.setattr(real_bot_menu, "_load_real_state",
                        lambda _d: (state, "authoritative", None))
    report = real_bot_menu.bot_report(Dashboard(), refresh=True)
    assert "Open PnL  unavailable" in report
    assert "Realized  unavailable" in report
    assert "Total PnL  unavailable" in report
    assert "AAA 22.00 SDA PnL unavailable" in report
