import pytest

import real_bot_menu
import real_trader


def test_mark_to_market_uses_actual_wallet_balance_and_live_sell_quote(monkeypatch):
    token = "0x" + "a" * 40
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "b" * 40)
    monkeypatch.setattr(real_trader, "token_balance", lambda token, wallet: 100 * 10**18)
    monkeypatch.setattr(real_trader, "token_decimals", lambda token: 18)
    monkeypatch.setattr(
        real_trader,
        "prepare_sell",
        lambda token, symbol, amount: {"quoted_wsda_raw": 19_120_000_000_000_000_000},
    )

    value, pnl, source, prices = real_bot_menu._position_mark_to_market(
        token,
        {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 100 * 10**18},
    )

    assert value == pytest.approx(19.12)
    assert pnl == pytest.approx(-0.88)
    assert "LIVE PinetSwap SELL quote" in source
    assert prices[0] == pytest.approx(0.2)
    assert prices[1] == pytest.approx(0.1912)


def test_mark_to_market_never_uses_quote_without_confirmed_buy_amount(monkeypatch):
    token = "0x" + "a" * 40
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "b" * 40)
    monkeypatch.setattr(real_trader, "token_balance", lambda token, wallet: 100 * 10**18)
    monkeypatch.setattr(real_trader, "token_decimals", lambda token: 18)
    monkeypatch.setattr(
        real_trader,
        "prepare_sell",
        lambda token, symbol, amount: {"quoted_wsda_raw": 20 * 10**18},
    )

    value, pnl, reason, prices = real_bot_menu._position_mark_to_market(
        token, {"symbol": "AAA", "amount_sda": 20, "quoted_out_raw": 100 * 10**18}
    )

    assert value is None
    assert pnl is None
    assert "confirmed BUY cost basis" in reason
    assert prices is None


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
