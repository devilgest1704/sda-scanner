import real_bot_menu


class Dashboard:
    def __init__(self, state):
        self.state = state

    def load(self, path, default):
        return self.state


def mark(value, cost, sell_value=None, sellable=True):
    pnl = value - cost if value is not None and cost is not None else None
    sell_pnl = sell_value - cost if sell_value is not None and cost is not None else None
    return {
        "amount": 10.0,
        "market_value_sda": value,
        "cost_sda": cost,
        "market_pnl_sda": pnl,
        "market_pnl_pct": pnl / cost * 100 if pnl is not None and cost else None,
        "price_sda": 1.0 if value is not None else None,
        "price_timestamp": "2026-11-06T15:00:00+00:00",
        "price_age_seconds": 120,
        "price_stale": False,
        "sell_value_sda": sell_value,
        "sell_pnl_sda": sell_pnl,
        "sell_pnl_pct": sell_pnl / cost * 100 if sell_pnl is not None and cost else None,
        "sellable": sellable,
        "sell_error": None if sellable else "STF",
        "sell_quote_timestamp": "2026-11-06T15:02:00+00:00" if sellable else None,
        "sell_quote_age_seconds": 0.1 if sellable else None,
    }


def wallet_snapshot(holdings, native=50.0):
    return {
        "wallet_address": real_bot_menu.BOT_WALLET_ADDRESS,
        "native_sda": native,
        "holdings": holdings,
        "errors": [],
    }


def realized_summary(verified=0.0, manual=0.0, unverified_count=0):
    return {
        "verified_pnl_sda": verified,
        "unverified_pnl_sda": manual,
        "verified_count": 1 if verified else 0,
        "unverified_count": unverified_count,
        "unverified_trades": [],
        "verified_trades": [],
        "verified_buys": [],
        "records": [],
    }


def test_bot_report_includes_position_and_total_unrealized_pnl(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}, "0xbbb": {"symbol": "BBB", "amount_sda": 10, "token_amount_raw": 1}}, "trades": [{"action": "SELL", "realized_pnl_sda": 3.5}], "daily_realized_pnl_sda": 1.0}
    marks = [("AAA", mark(22.0, 20.0, 21.5)), ("BBB", mark(9.0, 10.0, None, False))]
    snapshot = wallet_snapshot([
        {"market_value_sda": 22.0, "sell_value_sda": 21.5},
        {"market_value_sda": 9.0, "sell_value_sda": None},
    ])
    monkeypatch.setattr(real_bot_menu, "_position_marks", lambda dashboard, positions: (marks, snapshot))
    monkeypatch.setattr(real_bot_menu, "_realized_summary", lambda state, wallet: realized_summary(verified=4.0, manual=3.5, unverified_count=1))
    report = real_bot_menu.bot_report(Dashboard(state))
    assert "• AAA" in report
    assert "Market unrealized P/L: +2.0000 SDA (+10.00%)" in report
    assert "• BBB" in report
    assert "Market unrealized P/L: -1.0000 SDA (-10.00%)" in report
    assert "Open-position market value: 31.0000 SDA (priced 2/2)" in report
    assert "Open-position estimated sell value: 21.5000 SDA (quoted 1/2)" in report
    assert "Open cost basis: 30.0000 SDA" in report
    assert "Verified realized P/L: +4.0000 SDA" in report
    assert "Unverified/manual realized P/L: +3.5000 SDA" in report
    assert "Total wallet market value: 81.0000 SDA" in report
    assert "Known wallet liquidation value subtotal: 71.5000 SDA" in report
    assert "Sell: ⚠ quote unavailable (STF)" in report


def test_statistics_report_summarizes_realized_and_unrealized(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}}, "trades": [{"action": "BUY", "symbol": "AAA", "amount_sda": 20}, {"action": "SELL", "symbol": "OLD", "amount_sda": 10, "realized_pnl_sda": 4.0}, {"action": "SELL", "symbol": "BAD", "amount_sda": 10, "realized_pnl_sda": -2.0}], "daily_realized_pnl_sda": 2.0, "updated_at": "2026-11-05T12:00:00+00:00"}
    marks = [("AAA", mark(23.0, 20.0, 22.0))]
    snapshot = wallet_snapshot([{"market_value_sda": 23.0, "sell_value_sda": 22.0}], native=40)
    summary = realized_summary(verified=2.0, manual=0.0)
    summary["verified_trades"] = [{"realized_pnl_sda": 4.0}, {"realized_pnl_sda": -2.0}]
    summary["verified_count"] = 2
    summary["records"] = []
    monkeypatch.setattr(real_bot_menu, "_position_marks", lambda dashboard, positions: (marks, snapshot))
    monkeypatch.setattr(real_bot_menu, "_realized_summary", lambda state, wallet: summary)
    report = real_bot_menu.statistics_report(Dashboard(state))
    assert "Verified realized P/L: +2.0000 SDA" in report
    assert "Total unrealized market P/L: +3.0000 SDA" in report
    assert "Total executable P/L: +2.0000 SDA" in report
    assert "Verified win rate: 50.0%" in report
    assert "Profit factor: 2.00" in report
    assert "Market unrealized P/L: +3.0000 SDA (+15.00%)" in report


def test_unpriced_positions_are_excluded_from_totals(monkeypatch):
    state = {
        "positions": {
            "0xaaa": {"symbol": "AAA", "amount_sda": 20},
            "0xbbb": {"symbol": "BBB", "amount_sda": 10},
        },
        "trades": [],
    }
    marks = [("AAA", mark(22.0, 20.0, 21.0)), ("BBB", mark(None, 10.0, None, False))]
    snapshot = wallet_snapshot([
        {"market_value_sda": 22.0, "sell_value_sda": 21.0},
        {"market_value_sda": None, "sell_value_sda": None},
    ])
    monkeypatch.setattr(real_bot_menu, "_position_marks", lambda dashboard, positions: (marks, snapshot))
    monkeypatch.setattr(real_bot_menu, "_realized_summary", lambda state, wallet: realized_summary())
    monkeypatch.setenv(real_bot_menu.EXTERNAL_REFERENCE_ENV, "70.0")

    report = real_bot_menu.bot_report(Dashboard(state))

    assert "Known wallet market value subtotal: 72.0000 SDA (priced 1/2 token balances)" in report
    assert "Open-position market value: 22.0000 SDA (priced 1/2)" in report
    assert "Known unrealized market P/L: +2.0000 SDA (priced 1/2)" in report
    assert "Known wallet liquidation value subtotal: 71.0000 SDA (quoted 1/2 token balances)" in report
    assert "External reference total: 70.0000 SDA" in report
    assert "Difference (project market − reference): +2.0000 SDA" in report
    assert "Market value: UNPRICED" in report
