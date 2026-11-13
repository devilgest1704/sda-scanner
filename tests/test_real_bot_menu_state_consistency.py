from datetime import datetime, timedelta, timezone
import json

import real_bot_menu


class Dashboard:
    REMOTE_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"


class Response:
    def __init__(self, value): self.value = value
    def raise_for_status(self): pass
    def json(self): return self.value


def _ledger(count=10):
    return {"updated_at": datetime.now(timezone.utc).isoformat(),
            "positions": {f"token-{i}": {"symbol": f"T{i}", "amount_sda": 20}
                          for i in range(count)}, "trades": []}


def test_github_ledger_is_authoritative(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_bot_menu.requests, "get", lambda *a, **k: Response(_ledger()))
    state, source, _ = real_bot_menu._load_real_state(Dashboard())
    assert source == "authoritative" and len(state["positions"]) == 10


def test_remote_failure_is_marked_last_known_not_current(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_trade_state.json").write_text(json.dumps(_ledger(6)))
    monkeypatch.setattr(real_bot_menu.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    state, source, _ = real_bot_menu._load_real_state(Dashboard())
    assert source == "last_known" and len(state["positions"]) == 6


def test_ledger_staleness_is_independent_of_worker_heartbeat():
    old = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    warning = real_bot_menu._ledger_freshness_warning(
        {"updated_at": old}, {"updated_at": fresh, "ledger_updated_at": old})
    assert warning and "REAL ledger stale" in warning


def test_worker_status_is_live_dry_run_disabled_or_unknown():
    now = datetime.now(timezone.utc).isoformat()
    assert real_bot_menu._status({"enabled": True, "dry_run": False, "updated_at": now}) == "🟢 LIVE"
    assert real_bot_menu._status({"enabled": True, "dry_run": True, "updated_at": now}) == "🟡 DRY RUN"
    assert real_bot_menu._status({"enabled": False, "dry_run": False, "updated_at": now}) == "🔴 REAL TRADING DISABLED"
    assert real_bot_menu._status(None) == "⚪ STATUS UNKNOWN"


def test_worker_status_distinguishes_live_buy_data_degradation():
    now = datetime.now(timezone.utc).isoformat()
    status = {"enabled": True, "dry_run": False, "updated_at": now,
              "worker_health": "BUY_DATA_DEGRADED",
              "whale_source_health": "SOURCE_UNAVAILABLE"}
    assert real_bot_menu._status(status) == (
        "🟠 LIVE — BUY DATA DEGRADED — WHALE SOURCE UNAVAILABLE")
