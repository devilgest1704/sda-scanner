import fcntl
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
        "validation": {"all_pagination_exhausted": True, "detail_fetch_errors": {},
                       "cash_reconciliation_zero": True, "ledger_loaded": True,
                       "open_positions_valued": True},
        "blockchain_range": {"latest_timestamp": at.isoformat()},
        "buy_transactions": [{
            "tx": TX, "contract": TOKEN, "symbol": "RIDE",
            "at": at.isoformat(), "invested_sda": "20",
            "quantity": "1000",
        }],
        "open_positions": [{
            "token": TOKEN, "symbol": "RIDE", "buy_tx": TX,
            "at": at.isoformat(), "quantity": "1000", "decimals": 18,
            "cost_basis_sda": "20.00087134388836",
            "value_sda": "19.0",
        }],
    }


def test_manual_buy_discovery_and_cost_basis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = snapshot()
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(source))
    trader = FakeTrader()
    result = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
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
    first = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    second = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert len(trader.state["positions"]) == 1
    assert len([x for x in trader.state["trades"] if x["action"] == "MANUAL_BUY"]) == 1


def test_stale_snapshot_blocks_import_and_reports_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot(old)))
    trader = FakeTrader()
    result = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    assert result["ok"] is False
    assert result["syncStatus"] == "STALE"
    assert "stale blockchain snapshot" in result["syncError"]
    assert trader.state["positions"] == {}


def test_incomplete_pagination_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = snapshot()
    data["pagination_exhausted"] = False
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(data))
    result = reconciliation.reconcile_manual_buys(FakeTrader(), snapshot_path=tmp_path / "real_accounting_state.json")
    assert result["ok"] is False
    assert "pagination incomplete" in result["syncError"]


def test_wallet_balance_must_cover_snapshot_open_lot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    trader.token_balance = lambda *_: 1
    result = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    assert result["ok"] is False
    assert "wallet balance below" in result["syncError"]
    assert trader.state["positions"] == {}


def test_stale_snapshot_recovers_without_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "real_accounting_state.json"
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    path.write_text(json.dumps(snapshot(old)))
    trader = FakeTrader()
    stale = reconciliation.reconcile_manual_buys(trader, snapshot_path=path)
    assert stale["ok"] is False
    path.write_text(json.dumps(snapshot()))
    fresh = reconciliation.reconcile_manual_buys(trader, snapshot_path=path)
    assert fresh["ok"] is True
    assert len(trader.state["positions"]) == 1


def test_parallel_wallet_sync_is_rejected_by_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(reconciliation, "LOCK_FILE", tmp_path / ".real_wallet_reconciliation.lock")
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    with (tmp_path / ".real_wallet_reconciliation.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = reconciliation.reconcile_manual_buys(FakeTrader(), snapshot_path=tmp_path / "real_accounting_state.json")
        assert result["ok"] is False
        assert result["syncStatus"] == "IN_PROGRESS"
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)



def test_missing_runtime_position_for_known_auto_buy_stays_bot_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    trader.state["trades"] = [{
        "action": "BUY", "source": "BOT", "token": TOKEN, "tx": TX,
        "amount_sda": 20,
    }]
    result = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    assert result["ok"] is True
    assert trader.state["positions"][TOKEN]["source"] == "BOT"
    assert trader.state["positions"][TOKEN]["sellManagementEnabled"] is True
    assert len(trader.state["trades"]) == 1
    assert trader.state["manual_imports"] == []


def test_confirmed_pending_auto_buy_is_recovered_and_pending_intent_cleared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real_accounting_state.json").write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    trader.state["pending_buys"][TOKEN] = {"tx_hash": TX, "stage": "broadcasting"}
    result = reconciliation.reconcile_manual_buys(trader, snapshot_path=tmp_path / "real_accounting_state.json")
    assert result["ok"] is True
    assert trader.state["positions"][TOKEN]["source"] == "BOT"
    assert TOKEN not in trader.state["pending_buys"]
    assert trader.state["trades"][0]["action"] == "BUY_RECONCILED"



def test_default_snapshot_fetch_reads_latest_github_state(monkeypatch):
    source = snapshot()
    seen = {}
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return source
    def get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return Response()
    monkeypatch.setattr(reconciliation.requests, "get", get)
    monkeypatch.setattr(reconciliation, "LOCK_FILE", __import__("pathlib").Path("/tmp/test-real-wallet-reconciliation-remote.lock"))
    trader = FakeTrader()
    result = reconciliation.reconcile_manual_buys(trader)
    assert result["ok"] is True
    assert seen["url"] == reconciliation.ACCOUNTING_URL
    assert seen["kwargs"]["headers"]["Cache-Control"] == "no-cache"
    assert len(trader.state["positions"]) == 1



def test_rpc_timeout_fails_closed_and_retries_on_next_worker_cycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "real_accounting_state.json"
    path.write_text(json.dumps(snapshot()))
    trader = FakeTrader()
    trader.rpc = lambda *_: (_ for _ in ()).throw(TimeoutError("RPC timeout"))
    failed = reconciliation.reconcile_manual_buys(trader, snapshot_path=path)
    assert failed["ok"] is False
    assert "RPC timeout" in failed["syncError"]
    assert trader.state["positions"] == {}
    trader.rpc = lambda *_: {"status": "0x1", "gasUsed": "0x5208",
                             "effectiveGasPrice": "0x3b9aca00"}
    recovered = reconciliation.reconcile_manual_buys(trader, snapshot_path=path)
    assert recovered["ok"] is True
    assert len(trader.state["positions"]) == 1


def test_restart_reloads_manual_buy_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "real_accounting_state.json"
    state_path = tmp_path / "real_trade_state.json"
    path.write_text(json.dumps(snapshot()))

    class FileBackedTrader(FakeTrader):
        def __init__(self):
            super().__init__()
            if state_path.exists():
                self.state = json.loads(state_path.read_text())
        def save_state(self, state):
            self.state = state
            state_path.write_text(json.dumps(state))
            self.saved += 1

    first_worker = FileBackedTrader()
    first = reconciliation.reconcile_manual_buys(first_worker, snapshot_path=path)
    restarted_worker = FileBackedTrader()
    second = reconciliation.reconcile_manual_buys(restarted_worker, snapshot_path=path)
    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert len(restarted_worker.state["positions"]) == 1
    assert len(restarted_worker.state["trades"]) == 1
