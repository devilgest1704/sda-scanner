import real_bot_menu


class Dashboard:
    def __init__(self, state):
        self.state = state

    def load(self, path, default):
        return self.state


def test_bot_report_includes_position_and_total_unrealized_pnl(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}, "0xbbb": {"symbol": "BBB", "amount_sda": 10, "token_amount_raw": 1}}, "trades": [{"action": "SELL", "realized_pnl_sda": 3.5}], "daily_realized_pnl_sda": 1.0}
    monkeypatch.setattr(real_bot_menu, "_wallet_balance_sda", lambda: (50.0, None))
    monkeypatch.setattr(real_bot_menu, "_position_mark_to_market", lambda token, pos: (22.0, 2.0, None) if token == "0xaaa" else (9.0, -1.0, None))
    report = real_bot_menu.bot_report(Dashboard(state))
    assert "AAA • +2.00 SDA (+10.00%)" in report
    assert "BBB • -1.00 SDA (-10.00%)" in report
    assert "📈 Unrealized P/L: +1.00 SDA" in report
    assert "💼 Open cost: 30.00 SDA" in report


def test_statistics_report_summarizes_realized_and_unrealized(monkeypatch):
    state = {"positions": {"0xaaa": {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 1}}, "trades": [{"action": "BUY", "symbol": "AAA", "amount_sda": 20}, {"action": "SELL", "symbol": "OLD", "amount_sda": 10, "realized_pnl_sda": 4.0}, {"action": "SELL", "symbol": "BAD", "amount_sda": 10, "realized_pnl_sda": -2.0}], "daily_realized_pnl_sda": 2.0, "updated_at": "2026-11-05T12:00:00+00:00"}
    monkeypatch.setattr(real_bot_menu, "_wallet_balance_sda", lambda: (40.0, None))
    monkeypatch.setattr(real_bot_menu, "_position_mark_to_market", lambda token, pos: (23.0, 3.0, None))
    report = real_bot_menu.statistics_report(Dashboard(state))
    assert "Realized P/L total: +2.00 SDA" in report
    assert "Unrealized P/L: +3.00 SDA" in report
    assert "Combined P/L: +5.00 SDA" in report
    assert "Win rate: 50.0%" in report
    assert "Profit factor: 2.00" in report
    assert "AAA: +3.00 SDA (+15.00%)" in report
