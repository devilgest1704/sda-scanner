import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import real_wallet_reconciliation as reconciliation


WALLET = "0x9e3643f2ac15c91ee83aa2814015f15b18d573f2"
TOKEN = "0xe91cac00c1092611eb598b284a87a623a35af9b7"
TX = "0x" + "a" * 64


class FakeTrader:
    def __init__(self):
        self.state = {"positions": {}, "trades": [], "pending_buys": {}}
        self.saved = 0

    def wallet_address(self):
        return WALLET

    def chain_id(self):
        return 97453

    def load_state(self):
        return self.state

    def save_state(self, state):
        self.state = state
        self.saved += 1

    def token_decimals(self, token):
        assert token == TOKEN
        return 18

    def token_balance(self, token, owner):
        assert token == TOKEN and owner == WALLET
        return 1_250 * 10**18

    def rpc(self, method, params):
        assert method == "eth_getTransactionReceipt"
        assert params == [TX]
        return {"status": "0x1", "gasUsed": "0x5208",
                "effectiveGasPrice": "0x3b9aca00"}


def snapshot(at=None):
    at = at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": at.isoformat(),
        "chain_id": 97453,
        "wallet": WALLET,
        "pagination_exhausted": True,
        "bot_reconciliation_status": "COMPLETE",
        "bot_unresolved_transactions": [],
        "blockchain_range": {"latest_timestamp": at.isoformat()},
        "buy_transactions": [{
            "tx": TX, "contract": TOKEN, "symbol": "RIDE",
            "at": at.isoformat(), "invested_sda": "20",
            "quantity": "1000",
        }],
        "open_positions": [{
            "token": TOKEN, "symbol": "RIDE", "buy_tx": TX,
            "at": at.isoformat(), "quantity": "1000",
            "cost_basis_sda": "20.00087134388836",
            "value_sda": "19.0",
        }],
    }


def test_manual_buy_discovery_and_cost_basis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = snapshot()
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(source))
    trader = FakeTrader()
    result = reconciliation.reconcile_manual_buys(trader)
    assert result["ok"] is True
    assert len(result["imported"]) == 1
    position = trader.state["positions"][TOKEN]
    assert position["source"] == "MANUAL"
    assert position["sellManagementEnabled"] is True
    assert position["buy_tx"] == TX
    assert position["remaining_quantity"] == 1000 * 10**18
    assert Decimal(position["remaining_cost_basis"]) == Decimal("20.00087134388836")
    assert trader.state["trades"][0]["action"] == "MANUAL_BUY"
    assert trader.state["trades"][0]["network_fee_sda"] == "0.000021"
    assert trader.saved == 1


def test_repeated_reconciliation_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    first = reconciliation.reconcile_manual_buys(trader)
    second = reconciliation.reconcile_manual_buys(trader)
    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert len(trader.state["positions"]) == 1
    assert len([x for x in trader.state["trades"] if x["action"] == "MANUAL_BUY"]) == 1


def test_stale_snapshot_blocks_import_and_reports_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot(old)))
    trader = FakeTrader()
    result = reconciliation.reconcile_manual_buys(trader)
    assert result["ok"] is False
    assert result["syncStatus"] == "STALE"
    assert "stale blockchain snapshot" in result["syncError"]
    assert trader.state["positions"] == {}


def test_incomplete_pagination_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = snapshot()
    data["pagination_exhausted"] = False
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(data))
    result = reconciliation.reconcile_manual_buys(FakeTrader())
    assert result["ok"] is False
    assert "pagination incomplete" in result["syncError"]


def test_wallet_balance_must_cover_snapshot_open_lot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    trader.token_balance = lambda *_: 1
    result = reconciliation.reconcile_manual_buys(trader)
    assert result["ok"] is False
    assert "wallet balance below" in result["syncError"]
    assert trader.state["positions"] == {}
