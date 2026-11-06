import real_bot_menu


class Dashboard:
    def __init__(self, state):
        self.state = state

    def load(self, path, default):
        return self.state


def mark(value, cost, sellable=True):
    pnl = value - cost if value is not None and cost is not None else None
    return {
        "value_sda": value,
        "cost_sda": cost,
        "pnl_sda": pnl,
        "pnl_pct": pnl / cost * 100 if pnl is not None and cost else None,
        "price_sda": 1.0 if value is not None else None,
        "quote_age": "quote age 2m",
        "sellable": sellable,
        "sell_error": None if sellable else "STF",
    }


def test_bot_report_includes_position_and_total_unrealized_pnl(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}, "0xbbb": {"symbol": "BBB", "amount_sda": 10, "token_amount_raw": 1}}, "trades": [{"action": "SELL", "realized_pnl_sda": 3.5}], "daily_realized_pnl_sda": 1.0}
    monkeypatch.setattr(real_bot_menu, "_wallet_balance_sda", lambda: (50.0, None))
    monkeypatch.setattr(real_bot_menu, "_position_mark_to_market", lambda dashboard, token, pos: mark(22.0, 20.0) if token == "0xaaa" else mark(9.0, 10.0, False))
    report = real_bot_menu.bot_report(Dashboard(state))
    assert "• AAA" in report
    assert "P/L: +2.00 SDA (+10.00%)" in report
    assert "• BBB" in report
    assert "P/L: -1.00 SDA (-10.00%)" in report
    assert "Estimated open value: 31.00 SDA (valued 2/2)" in report
    assert "Unrealized P/L: +1.00 SDA (valued 2/2)" in report
    assert "Open cost basis: 30.00 SDA" in report
    assert "Realized P/L: +3.50 SDA" in report
    assert "Combined portfolio P/L: +4.50 SDA" in report
    assert "Executable SELL quotes: 1/2" in report
    assert "Sell: ⚠ quote unavailable (STF)" in report


def test_statistics_report_summarizes_realized_and_unrealized(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}}, "trades": [{"action": "BUY", "symbol": "AAA", "amount_sda": 20}, {"action": "SELL", "symbol": "OLD", "amount_sda": 10, "realized_pnl_sda": 4.0}, {"action": "SELL", "symbol": "BAD", "amount_sda": 10, "realized_pnl_sda": -2.0}], "daily_realized_pnl_sda": 2.0, "updated_at": "2026-11-05T12:00:00+00:00"}
    monkeypatch.setattr(real_bot_menu, "_wallet_balance_sda", lambda: (40.0, None))
    monkeypatch.setattr(real_bot_menu, "_position_mark_to_market", lambda dashboard, token, pos: mark(23.0, 20.0))
    report = real_bot_menu.statistics_report(Dashboard(state))
    assert "Realized P/L total: +2.00 SDA" in report
    assert "Unrealized P/L: +3.00 SDA (valued 1/1)" in report
    assert "Combined portfolio P/L: +5.00 SDA" in report
    assert "Win rate: 50.0%" in report
    assert "Profit factor: 2.00" in report
    assert "P/L: +3.00 SDA (+15.00%)" in report


def test_unpriced_positions_are_excluded_from_totals(monkeypatch):
    state = {
        "positions": {
            "0xaaa": {"symbol": "AAA", "amount_sda": 20},
            "0xbbb": {"symbol": "BBB", "amount_sda": 10},
        },
        "trades": [],
    }
    monkeypatch.setattr(real_bot_menu, "_wallet_balance_sda", lambda: (50.0, None))
    monkeypatch.setattr(
        real_bot_menu,
        "_position_mark_to_market",
        lambda dashboard, token, pos: mark(22.0, 20.0) if token == "0xaaa" else mark(None, 10.0, False),
    )

    report = real_bot_menu.bot_report(Dashboard(state))

    assert "Known open value subtotal: 22.00 SDA (valued 1/2)" in report
    assert "Known unrealized P/L subtotal: +2.00 SDA (valued 1/2)" in report
    assert "Combined portfolio P/L: N/A (open valuation or cost basis incomplete)" in report
    assert "Value: UNPRICED" in report
    assert "Executable SELL quotes: 1/2" in report
