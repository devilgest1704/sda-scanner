"""REAL BOT PnL statistics: one model, Prague periods, exclusions and read-only navigation."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import real_bot_menu as rb
import real_bot_views as views
import real_pnl_stats as pnl
import real_trader
import telegram_callback_router

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
BOT_BUYS = [{"tx": f"0xbuy{i}", "at": "2026-08-01T00:00:00Z", "symbol": "T", "contract": f"0x{i:040x}",
             "invested_sda": "20", "quantity": "1"} for i in range(10)]


def sell(tx, at, net, symbol="JSFT", contract="0x" + "1" * 40, buy_tx=("0xbuy1",), **extra):
    return {"tx": tx, "at": at, "symbol": symbol, "contract": contract, "net_pnl_sda": str(net),
            "proceeds_sda": str(20 + float(net)), "cost_basis_sda": "20", "quantity": "1",
            "buy_tx": list(buy_tx), **extra}


def accounting(sells, **extra):
    total = sum((Decimal(str(s["net_pnl_sda"])) for s in sells), Decimal(0))
    state = {"schema_version": 1, "bot_reconciliation_status": "COMPLETE",
             "bot_unresolved_transactions": [], "realized_pnl_sda": str(total),
             "buy_transactions": BOT_BUYS, "sell_transactions": sells,
             "excluded_external_activity": {"unmatched_sells": [], "unknown_basis_assets": []},
             "open_positions": [], "statistics": {}}
    state.update(extra)
    return state


def model(sells, runtime=None, open_pnl=None, **extra):
    return pnl.build_pnl_model(accounting(sells, **extra), runtime or {}, open_pnl, True, now=NOW)


def test_daily_weekly_monthly_grouping_and_cumulative():
    m = model([
        sell("0xa", "2026-11-10T08:00:00Z", 4.25),
        sell("0xb", "2026-11-09T08:00:00Z", -1.32),
        sell("0xc", "2026-11-09T10:00:00Z", 0.32),
        sell("0xd", "2026-10-20T10:00:00Z", 7.0),
    ])
    assert m["daily"]["2026-11-10"]["pnl"] == Decimal("4.25")
    assert m["daily"]["2026-11-09"]["pnl"] == Decimal("-1.00")
    assert m["daily"]["2026-11-09"]["sells"] == 2
    assert m["daily"]["2026-11-10"]["cumulative"] == Decimal("10.25")
    assert m["daily"]["2026-11-09"]["cumulative"] == Decimal("6.00")
    assert m["weekly"]["2026-W46"]["pnl"] == Decimal("3.25")
    assert m["weekly"]["2026-W43"]["pnl"] == Decimal("7.0")
    assert m["weekly"]["2026-W46"]["period_delta"] == Decimal("3.25")
    assert m["weekly"]["2026-W46"]["cumulative_sum"] == Decimal("10.25")
    assert m["monthly"]["2026-11"]["pnl"] == Decimal("3.25")
    assert m["monthly"]["2026-10"]["pnl"] == Decimal("7.0")
    assert m["monthly"]["2026-11"]["period_delta"] == Decimal("3.25")
    assert m["monthly"]["2026-11"]["cumulative_sum"] == Decimal("10.25")
    assert m["realized"] == Decimal("10.25")
    assert m["today"]["pnl"] == Decimal("4.25")
    assert m["this_week"]["pnl"] == Decimal("3.25")
    assert m["this_month"]["pnl"] == Decimal("3.25")


def test_prague_midnight_week_and_month_boundaries():
    m = model([
        sell("0xwinter", "2026-11-09T23:30:00Z", 1),   # 00:30 CET on 10.11
        sell("0xsunday", "2026-11-08T22:30:00Z", 2),   # Sunday 23:30 CET -> W45
        sell("0xmonday", "2026-11-08T23:30:00Z", 3),   # Monday 00:30 CET -> W46
        sell("0xmonth", "2026-10-31T23:30:00Z", 4),    # 1.11 00:30 CET -> November
        sell("0xsummer", "2026-07-01T22:30:00Z", 5),   # 2.7 00:30 CEST
    ])
    days = {e["tx"]: e["local_date"] for e in m["events"]}
    weeks = {e["tx"]: e["iso_week"] for e in m["events"]}
    months = {e["tx"]: e["month"] for e in m["events"]}
    assert days["0xwinter"] == "2026-11-10"
    assert weeks["0xsunday"] == "2026-W45" and weeks["0xmonday"] == "2026-W46"
    assert months["0xmonth"] == "2026-11"
    assert days["0xsummer"] == "2026-07-02"


def test_prague_rule_fallback_matches_zoneinfo(monkeypatch):
    stamps = ["2026-03-29T00:59:00+00:00", "2026-03-29T01:00:00+00:00",
              "2026-10-25T00:59:00+00:00", "2026-10-25T01:00:00+00:00", "2026-11-09T23:30:00+00:00"]
    expected = [pnl.to_prague(datetime.fromisoformat(s)).isoformat() for s in stamps]
    monkeypatch.setattr(pnl, "_PRAGUE", None)
    assert [pnl.to_prague(datetime.fromisoformat(s)).isoformat() for s in stamps] == expected


def test_realized_profit_loss_and_partial_sell_only_sold_portion():
    m = model([sell("0xprofit", "2026-11-10T08:00:00Z", "1.022410857988342624"),
               sell("0xloss", "2026-11-10T09:00:00Z", -0.5)])
    assert m["realized"] == Decimal("1.022410857988342624") + Decimal("-0.5")
    assert m["daily"]["2026-11-10"]["wins"] == 1 and m["daily"]["2026-11-10"]["losses"] == 1


def test_confirmed_automatic_sell_counted_exactly_once():
    duplicate = sell("0xswap", "2026-11-10T08:00:00Z", 1)
    m = pnl.build_pnl_model(accounting([duplicate, dict(duplicate)]), {}, None, True, now=NOW)
    assert m["realized"] == Decimal("1")
    assert len(m["events"]) == 1
    assert m["excluded"][0]["reason"] == "DUPLICATE"


def test_phantom_manual_sell_is_not_double_counted_and_not_partial():
    runtime = {"trades": [
        {"action": "MANUAL_SELL", "source": "wallet_reconciliation", "token": "0x" + "1" * 40,
         "sold_quantity_raw": 20, "cost_basis_sda": 4.0, "proceeds_sda": None,
         "superseded_by": "intent", "accounting_status": "SUPERSEDED_BY_AUTOMATIC_SELL",
         "at": "2026-11-10T12:38:31+00:00"},
        {"action": "SELL", "tx": "0xswap", "sell_intent_id": "intent", "realized_pnl_sda": 1.02,
         "at": "2026-11-10T12:38:33+00:00"},
    ]}
    m = model([sell("0xswap", "2026-11-10T12:37:37Z", 1.01)], runtime=runtime)
    assert m["realized"] == Decimal("1.01")
    assert m["realized_partial"] is False
    assert m["today"]["partial"] is False


def test_open_pnl_not_assigned_to_history_and_total_is_realized_plus_open():
    m = model([sell("0xa", "2026-11-09T08:00:00Z", 2)], open_pnl=5.5)
    assert m["today"]["pnl"] == Decimal(0)
    assert m["daily"]["2026-11-09"]["pnl"] == Decimal("2")
    assert "2026-11-10" not in m["daily"]
    assert m["total"] == m["realized"] + m["open"] == Decimal("7.5")


@pytest.mark.parametrize("row", [
    sell("0xpine", "2026-11-10T08:00:00Z", 67.9, symbol="PINE"),
    sell("0xairdrop", "2026-11-10T08:00:00Z", 3, contract="0x" + "9" * 40),
    sell("0xdeposit", "2026-11-10T08:00:00Z", 3, buy_tx=("0xexternal-deposit",)),
    sell("0xwithdrawal", "2026-11-10T08:00:00Z", 3),
    sell("0xpaper", "2026-11-10T08:00:00Z", 3, source="PAPER"),
])
def test_non_bot_activity_is_excluded(row):
    external = {"unmatched_sells": [{"sell_tx": "0xwithdrawal", "token": "X"}],
                "unknown_basis_assets": [{"symbol": "AIR", "contract": "0x" + "9" * 40}]}
    m = pnl.build_pnl_model(accounting([sell("0xbot", "2026-11-10T07:00:00Z", 1), row],
                                       excluded_external_activity=external), {}, None, True, now=NOW)
    assert [e["tx"] for e in m["events"]] == ["0xbot"]
    assert m["realized"] == Decimal("1")


def test_unresolved_bot_trade_marks_only_its_period_partial():
    runtime = {"trades": [{"action": "BUY", "tx": "0xunresolved", "at": "2026-11-09T10:00:00+00:00"}]}
    m = model([sell("0xa", "2026-11-10T08:00:00Z", 1)], runtime=runtime,
              bot_unresolved_transactions=["0xunresolved"], bot_reconciliation_status="INCOMPLETE")
    assert m["daily"]["2026-11-09"]["partial"] is True
    assert m["daily"]["2026-11-10"]["partial"] is False
    assert m["weekly"]["2026-W46"]["partial"] is True
    assert m["realized_partial"] is True


def test_runtime_automatic_sell_not_yet_in_fifo_marks_day_partial():
    runtime = {"trades": [{"action": "SELL", "tx": "0xfresh", "sell_intent_id": "i",
                           "at": "2026-11-10T11:00:00+00:00"}]}
    m = model([sell("0xa", "2026-11-09T08:00:00Z", 1)], runtime=runtime)
    assert m["today"]["partial"] is True
    assert m["pending_runtime_sells"] == ["0xfresh"]


def test_external_wallet_assets_do_not_mark_bot_pnl_partial():
    external = {"unmatched_sells": [{"sell_tx": "0xpine", "token": "PINE"}],
                "unknown_basis_assets": [{"symbol": "PINE", "contract": "0x" + "8" * 40}]}
    m = pnl.build_pnl_model(accounting([sell("0xa", "2026-11-10T08:00:00Z", 1)],
                                       excluded_external_activity=external,
                                       wallet_valuation={"unpriced_assets": [{"token": "JOINIT"}]},
                                       wallet_value_complete=False), {}, 0.5, True, now=NOW)
    assert m["realized_partial"] is False
    assert not any(row["partial"] for row in m["daily"].values())


def _built(monkeypatch, sells, open_rows=((10.0, 1.5),)):
    state = accounting(sells)
    positions = [{"token": "0x" + f"{i+50:040x}", "symbol": f"P{i}", "quantity": "1",
                  "value_sda": str(v), "cost_basis_sda": str(v - p), "open_pnl_sda": str(p)}
                 for i, (v, p) in enumerate(open_rows)]
    state["open_positions"] = positions
    dashboard = SimpleNamespace(load=lambda path, default: state if path == "real_accounting_state.json" else default)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: {"enabled": True, "dry_run": False})
    monkeypatch.setattr(rb, "_status", lambda _w: "🟢 LIVE")
    snapshot = {"native_sda": 100.0, "wallet_address": rb.BOT_WALLET_ADDRESS, "token_balances_loaded": True,
                "holdings": [{"address": p["token"], "symbol": p["symbol"], "market_value_sda": p["value_sda"]}
                             for p in positions],
                "tracked_expected_count": len(positions), "tracked_missing_contracts": [], "market_complete": True}
    monkeypatch.setattr(views, "_tracked_wallet_snapshot", lambda _a, _d=None: snapshot)
    return dashboard, views._build_dashboard_data(dashboard)


def test_main_menu_pnl_statistics_and_sell_history_reconcile(monkeypatch):
    sells = [sell("0xa", "2026-11-10T08:00:00Z", 4.25), sell("0xb", "2026-11-09T08:00:00Z", -1.32)]
    _dashboard, data = _built(monkeypatch, sells)
    m = data["pnl_model"]
    assert data["realized_pnl"] == pytest.approx(float(m["realized"])) == pytest.approx(2.93)
    assert data["open_pnl"] == pytest.approx(1.5)
    assert data["total_pnl"] == pytest.approx(data["realized_pnl"] + data["open_pnl"])
    assert sum(r["pnl_sda"] for r in data["sell_records"]) == pytest.approx(data["realized_pnl"])
    main = views._main_text(data)
    stats = views._pnl_text(data)
    assert "Realized +2.93 SDA" in main and "Realized      +2.93 SDA" in stats
    assert "Open PnL +1.50 SDA" in main and "Open          +1.50 SDA" in stats
    assert "Bot PnL +4.43 SDA" in main and "Total         +4.43 SDA" in stats
    assert "Dnes Δ" in stats and "Tento týden Δ" in stats and "Tento měsíc Δ" in stats
    assert "Realized: +2.93 SDA" in views._sell_text(data) or "Realized PnL: +2.93 SDA" in views._sell_text(data)


def test_period_screens_render_actual_values(monkeypatch):
    sells = [sell("0xa", "2026-11-10T08:00:00Z", 4.25), sell("0xb", "2026-11-09T08:00:00Z", -1.32),
             sell("0xc", "2026-10-09T08:00:00Z", 2.0)]
    _dashboard, data = _built(monkeypatch, sells)
    daily = views._pnl_daily_text(data)
    weekly = views._pnl_weekly_text(data)
    monthly = views._pnl_monthly_text(data)
    assert "📅 DAILY PNL" in daily and "09.11  Δ -1.32 SDA" in daily and "Σ +4.93" in daily
    assert "Green days" in daily and "Red days" in daily and "7D PnL" in daily
    assert "📆 WEEKLY PNL" in weekly and "2026-W46  Δ +2.93 SDA" in weekly and "Σ +4.93" in weekly and "Win rate" in weekly
    assert "🗓 MONTHLY PNL" in monthly and "Oct 2026  Δ +2.00 SDA" in monthly and "Σ +2.00" in monthly and "Σ +4.93" in monthly
    assert "Total realized BOT PnL +4.93 SDA" in monthly


class RouterDashboard:
    def __init__(self):
        self.events = []
        self.handle_update = lambda update, state=None: state or {"offset": 0}

    def answer_callback(self, callback_id, text=None):
        self.events.append(("answer", callback_id))

    def api(self, method, payload):
        self.events.append((method, payload))
        return {"ok": True}

    def menu_keyboard(self):
        return {"inline_keyboard": []}


def _press(dashboard, data, update_id):
    dashboard.handle_update({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "message": {"message_id": 1, "chat": {"id": 7}}}})
    edits = [e[1] for e in dashboard.events if e[0] == "editMessageText"]
    return edits[-1]


def _callbacks(payload):
    return [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]


def test_telegram_pnl_navigation_is_read_only(monkeypatch):
    monkeypatch.delenv("CHAT_ID", raising=False)
    sells = [sell("0xa", "2026-11-10T08:00:00Z", 4.25)]
    _dashboard, data = _built(monkeypatch, sells)
    views.clear_cache()
    monkeypatch.setattr(views, "_build_dashboard_data", lambda _d: data)
    forbidden = Mock(side_effect=AssertionError("trading side effect"))
    for name in ("send_transaction", "private_account", "quote_best_live_fee", "execute_buy",
                 "persist_real_intent_before_broadcast"):
        monkeypatch.setattr(real_trader, name, forbidden)
    dashboard = RouterDashboard()
    telegram_callback_router.patch_dashboard(dashboard)
    main = _press(dashboard, "REAL_BOT", 1)
    assert "REAL_BOT_PNL" in _callbacks(main)
    menu = _press(dashboard, "REAL_BOT_PNL", 2)
    assert menu["text"].startswith("📈 REAL PNL")
    assert _callbacks(menu) == ["REAL_BOT_PNL_DAILY", "REAL_BOT_PNL_WEEKLY", "REAL_BOT_PNL_MONTHLY", "REAL_BOT_BACK"]
    for update_id, (callback, title) in enumerate((("REAL_BOT_PNL_DAILY", "📅 DAILY PNL"),
                                                    ("REAL_BOT_PNL_WEEKLY", "📆 WEEKLY PNL"),
                                                    ("REAL_BOT_PNL_MONTHLY", "🗓 MONTHLY PNL")), start=3):
        screen = _press(dashboard, callback, update_id)
        assert screen["text"].startswith(title)
        assert _callbacks(screen) == ["REAL_BOT_PNL", "REAL_BOT_BACK"]
        back = _press(dashboard, "REAL_BOT_PNL", update_id + 10)
        assert back["text"].startswith("📈 REAL PNL")
    real_menu = _press(dashboard, "REAL_BOT_BACK", 30)
    assert real_menu["text"].startswith("🤖 REAL TRADING")
    forbidden.assert_not_called()
    views.clear_cache()
