from scripts.rebuild_real_accounting import (
    bot_buy_records_from_classifications,
    bot_closed_trade_records,
)


def test_pine_buy_requires_exact_confirmed_real_ledger_hash():
    rows = [
        {"hash": "0xbotpine", "success": True, "classification": "BUY", "timestamp": "2026-11-01T00:00:00Z",
         "native_value_sda": "30", "token_deltas": [
             {"direction": "IN", "contract": "0xpine", "symbol": "PINE", "quantity": "100"}]},
        {"hash": "0xairdrop", "success": True, "classification": "BUY", "timestamp": "2026-11-01T00:01:00Z",
         "native_value_sda": "0", "token_deltas": [
             {"direction": "IN", "contract": "0xpine", "symbol": "PINE", "quantity": "500"}]},
        {"hash": "0xother", "success": True, "classification": "BUY", "timestamp": "2026-11-01T00:02:00Z",
         "native_value_sda": "10", "token_deltas": [
             {"direction": "IN", "contract": "0xother", "symbol": "OTHER", "quantity": "5"}]},
    ]
    buys = bot_buy_records_from_classifications(rows + [dict(rows[0], hash="0xwrong-contract", token_deltas=[{"direction": "IN", "contract": "0xother", "symbol": "PINE", "quantity": "9"}])], [{"action": "BUY", "tx": "0xbotpine", "token": "0xpine"}], "0xwsda")
    assert [row["tx"] for row in buys] == ["0xbotpine", "0xother"]
    assert buys[0]["symbol"] == "PINE"


def test_pine_sell_requires_exact_real_ledger_sell_hash():
    closed = [
        {"sell_tx": "0xbot-sell", "contract": "0xpine", "token": "PINE"},
        {"sell_tx": "0xairdrop-sell", "contract": "0xpine", "token": "PINE"},
        {"sell_tx": "0xother-sell", "contract": "0xother", "token": "OTHER"},
    ]
    result = bot_closed_trade_records(closed, {"0xpine", "0xother"}, {"0xbot-sell"})
    assert [row["sell_tx"] for row in result] == ["0xbot-sell", "0xother-sell"]
