from datetime import datetime, timezone

import pytest

import real_bot_menu
import real_trader


class MarketDashboard:
    def __init__(self, token, price):
        self.market = {"tokens": {token: {"analysis": {
            "price_in_sda": price,
            "last_transaction": datetime.now(timezone.utc).isoformat(),
        }}}}

    def load(self, path, default):
        return self.market if path == "market_data.json" else default


def test_mark_to_market_uses_market_price_and_separate_live_sell_quote(monkeypatch):
    token = "0x" + "a" * 40
    dashboard = MarketDashboard(token, 0.1912)
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "b" * 40)
    monkeypatch.setattr(real_trader, "token_balance", lambda token, wallet: 100 * 10**18)
    monkeypatch.setattr(real_trader, "token_decimals", lambda token: 18)
    monkeypatch.setattr(
        real_trader,
        "prepare_sell",
        lambda token, symbol, amount: {"quoted_wsda_raw": 19_120_000_000_000_000_000},
    )

    mark = real_bot_menu._position_mark_to_market(
        dashboard,
        token,
        {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 100 * 10**18},
    )

    assert mark["value_sda"] == pytest.approx(19.12)
    assert mark["pnl_sda"] == pytest.approx(-0.88)
    assert mark["pnl_pct"] == pytest.approx(-4.4)
    assert mark["price_sda"] == pytest.approx(0.1912)
    assert mark["sellable"] is True
    assert mark["sell_error"] is None


def test_failed_sell_quote_does_not_remove_market_valuation(monkeypatch):
    token = "0x" + "a" * 40
    dashboard = MarketDashboard(token, 0.187)
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "b" * 40)
    monkeypatch.setattr(real_trader, "token_balance", lambda token, wallet: 100 * 10**18)
    monkeypatch.setattr(real_trader, "token_decimals", lambda token: 18)

    def failed_sell_quote(*args):
        raise RuntimeError("No live PinetSwap quote: {'message': 'execution reverted: STF', 'data': '0xsecret-calldata'}")

    monkeypatch.setattr(
        real_trader,
        "prepare_sell",
        failed_sell_quote,
    )

    mark = real_bot_menu._position_mark_to_market(
        dashboard, token, {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 100 * 10**18}
    )

    assert mark["value_sda"] == pytest.approx(18.7)
    assert mark["pnl_sda"] == pytest.approx(-1.3)
    assert mark["pnl_pct"] == pytest.approx(-6.5)
    assert mark["sellable"] is False
    assert mark["sell_error"] == "STF"


def test_market_value_can_be_shown_without_cost_basis(monkeypatch):
    token = "0x" + "a" * 40
    dashboard = MarketDashboard(token, 0.2)
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "b" * 40)
    monkeypatch.setattr(real_trader, "token_balance", lambda token, wallet: 100 * 10**18)
    monkeypatch.setattr(real_trader, "token_decimals", lambda token: 18)
    monkeypatch.setattr(real_trader, "prepare_sell", lambda *args: {"quoted_wsda_raw": 20 * 10**18})

    mark = real_bot_menu._position_mark_to_market(token=token, pos={}, dashboard=dashboard)

    assert mark["value_sda"] == pytest.approx(20)
    assert mark["pnl_sda"] is None
    assert mark["sellable"] is True


def test_fee_tier_rejects_fallback_fee_source(monkeypatch):
    monkeypatch.setattr(
        real_trader,
        "load_json",
        lambda path, default: {
            "quotes_50_sda": {
                "0x" + "a" * 40: {
                    "fee": 10000,
                    "quote_status": "VALIDATED_READ_ONLY",
                    "source": "fallback_fee",
                }
            }
        },
    )
    with pytest.raises(RuntimeError, match="Untrusted fee tier source"):
        real_trader.fee_tier_for("0x" + "a" * 40)


def test_fee_tier_accepts_token_specific_historical_fee(monkeypatch):
    monkeypatch.setattr(
        real_trader,
        "load_json",
        lambda path, default: {
            "quotes_50_sda": {
                "0x" + "a" * 40: {
                    "fee": 3000,
                    "quote_status": "VALIDATED_READ_ONLY",
                    "source": "historical_token",
                }
            }
        },
    )
    assert real_trader.fee_tier_for("0x" + "a" * 40) == 3000


def test_live_quote_probes_fee_tiers_when_token_has_no_trusted_fee(monkeypatch):
    seen = []

    def no_trusted_fee(token):
        raise RuntimeError("Untrusted fee tier source")

    def fake_quote(token_in, token_out, fee, recipient, amount_in, value_wei=0):
        seen.append(fee)
        if fee in (3000, 10000):
            return (300 if fee == 3000 else 250), f"calldata-{fee}", 123
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(real_trader, "fee_tier_for", no_trusted_fee)
    monkeypatch.setattr(real_trader, "quote_exact_input_single", fake_quote)

    fee, amount_out, calldata, deadline = real_trader.quote_best_live_fee(
        "token-in", "token-out", "token", "wallet", 10**18
    )

    assert seen == [100, 500, 3000, 10000]
    assert fee == 3000
    assert amount_out == 300
    assert calldata == "calldata-3000"
    assert deadline == 123
