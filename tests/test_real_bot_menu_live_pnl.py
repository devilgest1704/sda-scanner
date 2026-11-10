from datetime import datetime, timedelta, timezone
from decimal import Decimal

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


def wallet_holding(token, amount, price, sell_value=None, stale=False, error=None):
    return {
        "address": token,
        "amount_raw": int(Decimal(str(amount)) * 10**18),
        "amount": Decimal(str(amount)),
        "price_sda": price,
        "price_timestamp": datetime.now(timezone.utc).isoformat(),
        "price_age_seconds": 1,
        "price_stale": stale,
        "market_value_sda": Decimal(str(amount * price)) if price is not None else None,
        "market_error": error,
        "sell_value_sda": Decimal(str(sell_value)) if sell_value is not None else None,
        "sellable": sell_value is not None,
        "sell_error": None if sell_value is not None else "STF",
        "sell_quote_timestamp": datetime.now(timezone.utc).isoformat() if sell_value is not None else None,
        "sell_quote_age_seconds": 0.1 if sell_value is not None else None,
    }


def test_18_decimal_token_balance_conversion():
    assert real_bot_menu._human_token_amount("99207598316183000000", 18) == Decimal("99.207598316183")
    assert real_bot_menu._human_token_amount("99207598316183000000", 6) == Decimal("99207598316183")


def test_mark_to_market_and_sell_quote_are_separate(monkeypatch):
    token = "0x" + "a" * 40
    mark = real_bot_menu._position_mark_to_market(
        None,
        token,
        {"symbol": "AAA", "amount_sda": 20, "token_amount_raw": 100 * 10**18},
        wallet_holding(token, 100, 0.1912, 19.12),
    )

    assert mark["market_value_sda"] == pytest.approx(19.12)
    assert mark["market_pnl_sda"] == pytest.approx(-0.88)
    assert mark["market_pnl_pct"] == pytest.approx(-4.4)
    assert mark["sell_value_sda"] == pytest.approx(19.12)
    assert mark["sell_pnl_sda"] == pytest.approx(-0.88)
    assert mark["price_sda"] == pytest.approx(0.1912)
    assert mark["sellable"] is True
    assert mark["sell_error"] is None


def test_unavailable_sell_quote_keeps_market_valuation():
    token = "0x" + "a" * 40
    mark = real_bot_menu._position_mark_to_market(
        None, token, {"symbol": "AAA", "amount_sda": 20},
        wallet_holding(token, 100, 0.187, None, error="Supabase row unavailable"),
    )

    assert mark["market_value_sda"] == pytest.approx(18.7)
    assert mark["market_pnl_sda"] == pytest.approx(-1.3)
    assert mark["market_pnl_pct"] == pytest.approx(-6.5)
    assert mark["sell_value_sda"] is None
    assert mark["sellable"] is False
    assert mark["sell_error"] == "STF"


def test_missing_market_price_does_not_invent_value():
    token = "0x" + "a" * 40
    mark = real_bot_menu._position_mark_to_market(
        None, token, {"symbol": "AAA", "amount_sda": 20},
        wallet_holding(token, 100, None, 18.7, error="no valid Supabase price row"),
    )
    assert mark["market_value_sda"] is None
    assert mark["market_pnl_sda"] is None
    assert mark["sell_value_sda"] == pytest.approx(18.7)


def test_stale_price_is_flagged(monkeypatch):
    token = "0x" + "a" * 40
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    response = type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: [{"token_address": token, "price_in_sda": "0.2",
                               "tx_timestamp": timestamp, "tx_type": "buy"}],
    })()
    monkeypatch.setattr(real_bot_menu.requests, "get", lambda *args, **kwargs: response)
    price = real_bot_menu._newest_supabase_price(token)
    assert price["price_sda"] == pytest.approx(0.2)
    assert price["stale"] is True


def test_zero_token_balance_is_not_valued():
    token = "0x" + "a" * 40
    holding = wallet_holding(token, 0, 0.2, None)
    holding["amount_raw"] = 0
    mark = real_bot_menu._position_mark_to_market(None, token, {"amount_sda": 20}, holding)
    assert mark["amount"] == 0
    assert mark["market_value_sda"] == 0
    assert mark["sellable"] is False
    assert mark["sell_error"] == "zero wallet balance"


def test_wallet_address_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(real_bot_menu, "_wallet_address", lambda: "0x" + "a" * 40)
    monkeypatch.setattr(real_bot_menu.requests, "get", lambda *args, **kwargs: pytest.fail("must not call wallet API"))
    snapshot = real_bot_menu._fetch_wallet_snapshot()
    assert snapshot["holdings"] == []
    assert any("does not match" in error for error in snapshot["errors"])


def test_hashless_manual_sell_is_unverified_and_excluded_from_verified_total(monkeypatch):
    monkeypatch.setattr(real_bot_menu, "_explorer_get", lambda *args: pytest.fail("no hash: do not query explorer"))
    summary = real_bot_menu._realized_summary({"trades": [{
        "action": "SELL", "source": "manual_user_confirmed", "token": "0x" + "a" * 40,
        "pnl_sda": 18.1132, "cost_sda": 20,
    }]}, real_bot_menu.BOT_WALLET_ADDRESS)
    assert summary["verified_pnl_sda"] == 0
    assert summary["unverified_pnl_sda"] == pytest.approx(18.1132)
    assert summary["unverified_trades"][0]["status"] == "UNVERIFIED MANUAL"


def test_sell_transaction_requires_wallet_and_matching_token_wsda_transfers(monkeypatch):
    token = "0x" + "a" * 40
    wallet = real_bot_menu.BOT_WALLET_ADDRESS
    tx_hash = "0x" + "1" * 64
    router = "0x" + "b" * 40
    responses = [
        {"hash": tx_hash, "status": "ok", "from": {"hash": wallet}},
        {"items": [
            {"token": {"address_hash": token, "decimals": "18"}, "from": {"hash": wallet},
             "to": {"hash": router}, "total": {"value": str(100 * 10**18)}},
            {"token": {"address_hash": real_trader.cfg.WSDA_ADDRESS, "decimals": "18"},
             "from": {"hash": router}, "to": {"hash": wallet}, "total": {"value": str(19 * 10**18)}},
        ]},
    ]
    def explorer_response(path):
        return responses[0] if path.startswith("transactions/") and not path.endswith("token-transfers") else responses[1]

    monkeypatch.setattr(real_bot_menu, "_explorer_get", explorer_response)
    result = real_bot_menu._verify_trade({"action": "SELL", "token": token, "tx": tx_hash}, wallet)
    assert result["verified"] is True
    assert result["proceeds_sda"] == pytest.approx(19)
    summary = real_bot_menu._realized_summary({"trades": [{
        "action": "SELL", "token": token, "tx": tx_hash, "cost_sda": 20,
    }]}, wallet)
    assert summary["verified_pnl_sda"] == pytest.approx(-1)
    assert summary["unverified_pnl_sda"] == 0


def test_confirmed_buy_uses_native_sda_value_and_received_token_transfer(monkeypatch):
    token = "0x" + "c" * 40
    wallet = real_bot_menu.BOT_WALLET_ADDRESS
    tx_hash = "0x" + "2" * 64
    responses = [
        {"hash": tx_hash, "status": "ok", "from": {"hash": wallet}, "value": str(20 * 10**18)},
        {"items": [{
            "token": {"address_hash": token, "decimals": "18"},
            "from": {"hash": "0x" + "d" * 40}, "to": {"hash": wallet},
            "total": {"value": str(100 * 10**18)},
        }]},
    ]
    def explorer_response(path):
        return responses[0] if not path.endswith("token-transfers") else responses[1]
    monkeypatch.setattr(real_bot_menu, "_explorer_get", explorer_response)

    result = real_bot_menu._verify_trade({"action": "BUY", "token": token, "tx": tx_hash}, wallet)

    assert result["verified"] is True
    assert result["token_amount"] == Decimal(100)
    assert result["sda_transfer"] == Decimal(20)


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

def test_telegram_wallet_holding_uses_shared_fresh_market_mark(monkeypatch):
    import real_market_price

    token = "0x" + "e" * 40
    wallet = real_bot_menu.BOT_WALLET_ADDRESS
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(real_bot_menu, "_newest_supabase_price",
                        lambda _token: {"price_sda": "0.20", "timestamp": stale})
    quote = {"price_sda": Decimal("0.25"), "timestamp": datetime.now(timezone.utc).isoformat(),
             "source": "PinetSwap 1 SDA BUY-side reference quote",
             "fee_tier": 3000, "quote_amount_sda": 1}
    monkeypatch.setattr(real_market_price, "read_only_pinet_swap_buy_reference_quote",
                        lambda *_args: quote)
    holding = {"address": token, "decimals": 18, "amount": Decimal("100"),
               "price_sda": None, "market_value_sda": None}
    result = real_bot_menu._price_wallet_holding(
        holding, wallet, {"updated_at": stale, "tokens": {}}
    )
    assert result["price_sda"] == Decimal("0.25")
    assert result["market_value_sda"] == Decimal("25")
    assert result["price_source"] == "PinetSwap 1 SDA BUY-side reference quote"
    assert result["price_fee_tier"] == 3000
    assert result["price_age_seconds"] == pytest.approx(0, abs=0.01)


def test_telegram_wallet_holding_prefers_fresh_supabase_trade(monkeypatch):
    import real_market_price

    token = "0x" + "e" * 40
    wallet = real_bot_menu.BOT_WALLET_ADDRESS
    timestamp = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    monkeypatch.setattr(real_bot_menu, "_newest_supabase_price",
                        lambda _token: {"price_sda": "0.20", "timestamp": timestamp})
    monkeypatch.setattr(real_market_price, "read_only_pinet_swap_buy_reference_quote",
                        lambda *_args: pytest.fail("fresh Supabase trade should win"))
    holding = {"address": token, "decimals": 18, "amount": Decimal("100"),
               "price_sda": None, "market_value_sda": None}
    result = real_bot_menu._price_wallet_holding(
        holding, wallet, {"updated_at": "stale", "tokens": {}}
    )
    assert result["price_sda"] == Decimal("0.20")
    assert result["market_value_sda"] == Decimal("20")
    assert result["price_source"] == "supabase_transaction"


def test_portfolio_position_line_includes_concise_pnl_percentage():
    line = real_bot_menu._position_lines("XEN", {
        "market_value_sda": 20.62, "market_pnl_sda": 0.62,
        "market_pnl_pct": 3.1,
    })[0]
    assert "XEN 20.62 SDA +0.62 (+3.1%)" in line


