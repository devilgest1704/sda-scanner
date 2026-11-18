from collections import deque
from decimal import Decimal

from scripts.rebuild_real_accounting import (
    allocate_open_lot_value,
    consume_fifo_lots,
    native_cash_expected,
    wsda_sell_proceeds,
)


def _lot(buy_tx, quantity):
    value = Decimal(quantity)
    return {
        "buy_hash": buy_tx,
        "qty": value,
        "remaining": value,
        "cost": Decimal("20"),
        "buy_gas": Decimal("0.01"),
    }


def test_fifo_token_transfer_out_removes_oldest_lots_and_leaves_actual_balance():
    lots = deque([
        _lot("older-partial", "33.086381458051294933"),
        _lot("unresolved-buy", "83.958235908240956719"),
        _lot("next-buy", "60.289268908555882608"),
        _lot("current-buy", "82.3738641465379469"),
    ])
    transferred = Decimal("177.333886274848134260")
    matches, unmatched = consume_fifo_lots(lots, transferred)

    assert unmatched == 0
    assert [row["lot"]["buy_hash"] for row in matches] == [
        "older-partial", "unresolved-buy", "next-buy"
    ]
    assert len(lots) == 1
    assert lots[0]["buy_hash"] == "current-buy"
    assert lots[0]["remaining"] == Decimal("82.3738641465379469")


def test_fifo_transfer_out_remains_unresolved_when_it_exceeds_confirmed_lots():
    lots = deque([_lot("known-buy", "5")])
    matches, unmatched = consume_fifo_lots(lots, Decimal("7"))

    assert len(matches) == 1
    assert unmatched == Decimal("2")
    assert not lots


def test_native_cash_reconciliation_includes_opening_balance_and_internal_flows():
    expected = native_cash_expected(
        opening_balance=Decimal("100"),
        opening_wsda=Decimal("10"),
        native_in=Decimal("20"),
        native_out=Decimal("5"),
        router_buy_spend=Decimal("30"),
        wsda_external_in=Decimal("3"),
        wsda_external_out=Decimal("2"),
        wsda_router_spend=Decimal("4"),
        sell_proceeds=Decimal("12"),
        residual_wsda=Decimal("4"),
        wallet_fees=Decimal("0.5"),
    )
    assert expected == Decimal("99.5")


def test_whole_wallet_quote_is_allocated_pro_rata_across_fifo_lots():
    first = allocate_open_lot_value("30", "40", "100")
    second = allocate_open_lot_value("30", "60", "100")

    assert first == Decimal("12")
    assert second == Decimal("18")
    assert first + second == Decimal("30")


def test_wsda_sell_proceeds_counts_router_hop_only_once():
    flows = [
        ("0xpool", "0xrouter", Decimal("20")),
        ("0xrouter", "0xwallet", Decimal("20")),
    ]
    assert wsda_sell_proceeds(flows, "0xwallet", "0xzero") == Decimal("20")


def test_wsda_sell_proceeds_does_not_count_router_intermediate_without_wallet_receipt():
    flows = [("0xpool", "0xrouter", Decimal("7.5"))]
    assert wsda_sell_proceeds(flows, "0xwallet", "0xzero") == Decimal("0")
