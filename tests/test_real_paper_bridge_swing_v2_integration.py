"""Production bridge -> Swing SELL V2 integration tests; never broadcast transactions."""
import json
import os
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import real_market_price as market_prices
import real_paper_bridge as bridge
import real_swing_sell_executor as executor
import real_trader
import real_trading_config as cfg
import swing_sell_v2 as policy

TOKEN = "0x" + "a" * 40
WALLET = "0x" + "c" * 40


def _snapshot(price):
    stamp = datetime.now(timezone.utc).isoformat()
    return {"updated_at": stamp, "tokens": {
        TOKEN: {"price_in_sda": str(price), "last_transaction": stamp}
    }}


def _run_case(price=None, unavailable=False):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paper_file = root / "positions.json"
        bridge_state_file = root / "bridge_state.json"
        events_file = root / "bridge_events.json"
        executor_state_file = root / "real_trade_state.json"
        paper_file.write_text(json.dumps({"positions": {}}), encoding="utf-8")
        bridge_state_file.write_text(json.dumps({
            "initialized": True, "paper_positions": [TOKEN],
            "pending_buy": [], "pending_sell": [],
        }), encoding="utf-8")

        real_state = {
            "sell_strategy_version": policy.STRATEGY_VERSION,
            "positions": {TOKEN: {
                "token": TOKEN, "symbol": "TEST", "buy_tx": "buy-fixture",
                "token_amount_raw": 1000, "remaining_quantity": 1000,
                "amount_sda": 20, "remaining_cost_basis": "20",
                "wallet_available_quantity": 1000,
            }},
            "pending_buys": {}, "pending_sells": {},
            "sell_intents": {}, "sell_intent_counters": {}, "trades": [],
        }
        snapshot = ({"updated_at": "2020-01-01T00:00:00Z", "tokens": {}}
                    if unavailable else _snapshot(price))

        def load_state():
            return real_state

        def save_state(value):
            real_state.clear()
            real_state.update(value)

        send_transaction = Mock(side_effect=AssertionError("transaction broadcast attempted"))
        with ExitStack() as stack:
            stack.enter_context(patch.object(bridge, "PAPER_FILE", paper_file))
            stack.enter_context(patch.object(bridge, "STATE_FILE", bridge_state_file))
            stack.enter_context(patch.object(bridge, "EVENTS_FILE", events_file))
            stack.enter_context(patch.object(market_prices, "load_market_snapshot", return_value=snapshot))
            stack.enter_context(patch.object(market_prices, "load_token_metadata", return_value={}))
            stack.enter_context(patch.object(market_prices, "newest_supabase_price", return_value=None))
            stack.enter_context(patch.object(real_trader, "load_state", side_effect=load_state))
            stack.enter_context(patch.object(real_trader, "save_state", side_effect=save_state))
            stack.enter_context(patch.object(real_trader, "token_decimals", return_value=0))
            stack.enter_context(patch.object(real_trader, "wallet_address", return_value=WALLET))
            stack.enter_context(patch.object(real_trader, "send_transaction", send_transaction))
            stack.enter_context(patch.object(real_trader, "token_balance",
                                             side_effect=AssertionError("unexpected wallet RPC")))
            stack.enter_context(patch.object(executor, "reconcile_position_wallet",
                                             return_value={"status": "UNCHANGED"}))
            stack.enter_context(patch.object(cfg, "REAL_TRADING_ENABLED", True))
            # Bridge enters live mode; executor is separately forced into dry-run.
            stack.enter_context(patch.object(cfg, "REAL_TRADING_DRY_RUN", True))
            stack.enter_context(patch.object(cfg, "STATE_FILE", str(executor_state_file)))
            stack.enter_context(patch.dict(os.environ, {
                "REAL_TRADING_ENABLED": "true",
                "REAL_TRADING_DRY_RUN": "false",
                "SDA_PAPER_BUY_MODE": "temporal_recovery",
                "SDA_PAPER_TP4": "1",
                "GH_TOKEN": "",
            }))
            resolver = stack.enter_context(patch.object(
                market_prices, "resolve_market_price", wraps=market_prices.resolve_market_price))
            evaluate = stack.enter_context(patch.object(
                executor, "evaluate_position", wraps=executor.evaluate_position))
            decide = stack.enter_context(patch.object(
                policy, "decide", wraps=policy.decide))
            result = bridge.reconcile()

        # migrate_if_needed() is not mocked: persisted V2 state makes this a
        # real post-migration reconciliation and allows the SELL loop to run.
        assert real_state["sell_strategy_version"] == policy.STRATEGY_VERSION
        sell_rows = [x for x in result["actions"] if x.get("action") == "SELL_DECISION"]
        assert len(sell_rows) == 1
        assert resolver.call_count == 1
        assert send_transaction.call_count == 0
        return sell_rows[0], real_state, evaluate.call_count, decide.call_count


def test_fresh_normal_pnl_reaches_executor_and_policy_then_holds():
    row, state, evaluate_calls, decide_calls = _run_case("0.0202")
    assert row["decision_price"] == 0.0202
    assert row["action"] == "HOLD"
    assert evaluate_calls == 1
    assert decide_calls == 1
    assert state["pending_sells"] == {}


def test_minus_50_percent_creates_full_sell_decision_without_broadcast():
    row, state, evaluate_calls, decide_calls = _run_case("0.01")
    assert row["status"] == "DRY_RUN"
    assert evaluate_calls == 1
    assert decide_calls == 1
    intent = state["pending_sells"][TOKEN]
    assert intent["sell_reason"] == "STOP_LOSS"
    assert intent["requested_percentage"] == 100.0


def test_plus_25_percent_creates_one_20_percent_partial_without_broadcast():
    row, state, evaluate_calls, decide_calls = _run_case("0.025")
    assert row["status"] == "DRY_RUN"
    assert evaluate_calls == 1
    assert decide_calls == 1
    intent = state["pending_sells"][TOKEN]
    assert intent["sell_reason"] == "PROFIT_PARTIAL_25"
    assert intent["requested_percentage"] == 20.0


def test_plus_50_percent_creates_full_sell_decision_without_broadcast():
    row, state, evaluate_calls, decide_calls = _run_case("0.03")
    assert row["status"] == "DRY_RUN"
    assert evaluate_calls == 1
    assert decide_calls == 1
    intent = state["pending_sells"][TOKEN]
    assert intent["sell_reason"] == "HARD_TP"
    assert intent["requested_percentage"] == 100.0


def test_price_unavailable_holds_without_executor_or_transaction():
    row, state, evaluate_calls, decide_calls = _run_case(unavailable=True)
    assert row["status"] == "PRICE_UNAVAILABLE"
    assert evaluate_calls == 0
    assert decide_calls == 0
    assert state["pending_sells"] == {}
