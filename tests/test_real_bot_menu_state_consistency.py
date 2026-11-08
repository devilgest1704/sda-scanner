from datetime import datetime, timedelta, timezone
import json

import real_bot_menu


class Dashboard:
    REMOTE_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def _worker_state(enabled=True, dry_run=False, updated_at=None):
    return {"enabled": enabled, "dry_run": dry_run,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat()}


def _ledger(count=10, amount=12.5, pnl=-3.75):
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "positions": {f"token-{i}": {"symbol": f"T{i}", "amount_sda": amount} for i in range(count)},
        "daily_realized_pnl_sda": pnl, "trades": [],
    }


def _report_fakes(monkeypatch, ledger):
    marks = [(p["symbol"], {"cost_sda": float(p["amount_sda"]), "market_value_sda": None,
             "sell_value_sda": None, "market_pnl_sda": None, "sell_pnl_sda": None})
             for p in ledger["positions"].values()]
    monkeypatch.setattr(real_bot_menu, "_load_worker_status", lambda _d: _worker_state())
    monkeypatch.setattr(real_bot_menu, "_position_marks", lambda _d, _p: (marks, {"holdings": [], "native_sda": None}))
    monkeypatch.setattr(real_bot_menu, "_position_lines", lambda _s, _m: [])
    monkeypatch.setattr(real_bot_menu, "_realized_summary", lambda _s, _w: {
        "verified_pnl_sda": ledger["daily_realized_pnl_sda"], "verified_count": 0,
        "unverified_pnl_sda": 0.0, "unverified_count": 0, "unverified_trades": [],
        "verified_trades": [], "verified_buys": [], "records": [],
    })


def test_a_github_authoritative_ledger_is_preferred_over_local_snapshot(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_trade_state.json").write_text(json.dumps(_ledger(count=1)))
    authoritative = _ledger(count=10)
    monkeypatch.setattr(real_bot_menu.requests, "get", lambda *a, **k: Response(authoritative))
    state, source, _ = real_bot_menu._load_real_state(Dashboard())
    assert source == "authoritative" and len(state["positions"]) == 10


def test_b_current_authoritative_ledger_displays_ten_positions(monkeypatch):
    ledger = _ledger()
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (ledger, "authoritative", None))
    _report_fakes(monkeypatch, ledger)
    assert "Open positions: 10/10" in real_bot_menu.bot_report(Dashboard())


def test_c_open_cost_follows_authoritative_ledger(monkeypatch):
    ledger = _ledger(amount=12.5)
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (ledger, "authoritative", None))
    _report_fakes(monkeypatch, ledger)
    assert "Open cost basis: 125.0000 SDA" in real_bot_menu.bot_report(Dashboard())


def test_d_realized_pnl_follows_authoritative_ledger(monkeypatch):
    ledger = _ledger(pnl=-7.125)
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (ledger, "authoritative", None))
    _report_fakes(monkeypatch, ledger)
    assert "Verified realized P/L: -7.1250 SDA" in real_bot_menu.bot_report(Dashboard())


def test_e_remote_failure_never_promotes_local_snapshot_to_current(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_trade_state.json").write_text(json.dumps(_ledger(count=6)))
    monkeypatch.setattr(real_bot_menu.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    state, source, _ = real_bot_menu._load_real_state(Dashboard())
    assert source == "last_known" and len(state["positions"]) == 6


def test_f_local_fallback_is_explicitly_marked_last_known(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    local = _ledger(count=6)
    (tmp_path / "real_trade_state.json").write_text(json.dumps(local))
    monkeypatch.setattr(real_bot_menu, "_load_worker_status", lambda _d: None)
    monkeypatch.setattr(real_bot_menu, "_load_real_state", lambda _d: (local, "last_known", "offline"))
    _report_fakes(monkeypatch, local)
    report = real_bot_menu.bot_report(Dashboard())
    assert "LAST-KNOWN snapshot only" in report and "LAST-KNOWN OPEN BOT POSITIONS" in report


def test_g_ledger_older_than_900_seconds_is_marked_stale():
    old = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    warning = real_bot_menu._ledger_freshness_warning(
        {"updated_at": datetime.now(timezone.utc).isoformat()}, {"updated_at": old})
    assert warning and "REAL ledger stale" in warning


def test_h_live_worker_configuration_displays_live():
    assert real_bot_menu._status(_worker_state(True, False)) == "🟢 LIVE"


def test_i_dry_run_worker_configuration_displays_dry_run():
    assert real_bot_menu._status(_worker_state(True, True)) == "🟡 DRY RUN"


def test_j_disabled_worker_displays_disabled():
    assert real_bot_menu._status(_worker_state(False, False)) == "🔴 REAL TRADING DISABLED"


def test_k_unknown_worker_state_never_guesses_live_or_dry_run():
    assert real_bot_menu._status(None) == "⚪ STATUS UNKNOWN"
    assert real_bot_menu._status({"enabled": None, "dry_run": None}) == "⚪ STATUS UNKNOWN"
