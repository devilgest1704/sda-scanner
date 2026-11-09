"""Live, idempotent Swing SELL V2 execution adapter.

BUY is intentionally not imported or modified here. All transaction paths are
SELL-only and are guarded by a per-token process lock. No transaction is sent
unless REAL trading is enabled and dry-run is false.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import real_trader as trader
import real_trading_config as cfg
import swing_sell_v2 as policy

ACTIVE_STAGES = {
    "DECIDED", "APPROVAL_SUBMITTED", "SELL_READY",
    "SWAP_SUBMITTED", "SWAP_CONFIRMED", "UNWRAP_SUBMITTED",
}


@contextmanager
def _token_lock(token):
    path = Path(str(cfg.STATE_FILE) + ".sell." + trader.addr(token) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def migrate_if_needed():
    """One-way migration; archives every pre-v2 pending SELL and never replays it."""
    state = trader.load_state()
    if state.get("sell_strategy_version") == policy.STRATEGY_VERSION:
        return False
    migrated_at = trader.now()
    migrated = policy.migrate_state(state, migrated_at)
    migrated["sell_migration_observation_started_at"] = migrated_at
    migrated["sell_migration_historical_transactions"] = 0
    migrated["sell_intents"] = migrated.get("sell_intents", {})
    migrated["sell_intent_counters"] = migrated.get("sell_intent_counters", {})
    trader.save_state(migrated)
    return True


def _record_manual_disposal(state, token, position, previous_qty, wallet_qty):
    removed = max(0, int(previous_qty) - int(wallet_qty))
    if removed <= 0:
        return
    basis = Decimal(str(position.get("remaining_cost_basis",
                                     position.get("amount_sda", position.get("cost_sda", 0))) or 0))
    allocated = basis * Decimal(removed) / Decimal(previous_qty) if previous_qty else Decimal(0)
    state.setdefault("trades", []).append({
        "action": "MANUAL_SELL", "source": "wallet_reconciliation",
        "symbol": position.get("symbol"), "token": token,
        "sold_quantity_raw": removed, "cost_basis_sda": float(allocated),
        "proceeds_sda": None, "realized_pnl_sda": None,
        "accounting_status": "PROCEEDS_UNKNOWN", "at": trader.now(),
    })


def reconcile_position_wallet(token):
    """Wallet balance is authoritative; manual disposals never create fake proceeds."""
    token = trader.addr(token)
    state = trader.load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        return {"status": "NO_POSITION"}
    owner = trader.wallet_address()
    if not owner:
        raise RuntimeError("REAL wallet address unavailable")
    wallet_qty = int(trader.token_balance(token, owner))
    recorded = int(position.get("remaining_quantity",
                                position.get("token_amount_raw", 0)) or 0)
    if wallet_qty == recorded:
        position["wallet_available_quantity"] = wallet_qty
        return {"status": "UNCHANGED", "wallet_quantity": wallet_qty}
    if wallet_qty < recorded:
        _record_manual_disposal(state, token, position, recorded, wallet_qty)
        adjusted = policy.reconcile_wallet_quantity(position, wallet_qty)
        if wallet_qty == 0:
            state["positions"].pop(token, None)
            adjusted["status"] = "CLOSED_MANUAL_RECONCILED"
            state.setdefault("manual_disposals", []).append({
                "token": token, "quantity_raw": recorded,
                "proceeds_sda": None, "realized_pnl_sda": None,
                "status": "PROCEEDS_UNKNOWN", "at": trader.now(),
            })
        else:
            adjusted["wallet_available_quantity"] = wallet_qty
            state["positions"][token] = adjusted
        state["updated_at"] = trader.now()
        trader.save_state(state)
        return {"status": "CLOSED_MANUAL_RECONCILED" if wallet_qty == 0 else "MANUAL_RECONCILED",
                "wallet_quantity": wallet_qty, "previous_quantity": recorded}
    # External deposits are not silently added to the bot's costed position.
    position["wallet_available_quantity"] = wallet_qty
    trader.save_state(state)
    return {"status": "WALLET_EXCESS_IGNORED", "wallet_quantity": wallet_qty,
            "tracked_quantity": recorded}


def _position_defaults(position):
    pos = dict(position)
    qty = int(pos.get("remaining_quantity", pos.get("token_amount_raw", 0)) or 0)
    cost = Decimal(str(pos.get("remaining_cost_basis",
                               pos.get("amount_sda", pos.get("cost_sda", 0))) or 0))
    pos.setdefault("position_id", pos.get("buy_tx") or pos.get("token"))
    pos.setdefault("initial_quantity", pos.get("token_amount_raw", qty))
    pos.setdefault("remaining_quantity", qty)
    pos.setdefault("confirmed_sold_quantity", 0)
    pos.setdefault("initial_cost_basis", str(pos.get("amount_sda", cost)))
    pos.setdefault("remaining_cost_basis", str(cost))
    pos.setdefault("sold_cost_basis", "0")
    pos.setdefault("realized_proceeds", "0")
    pos.setdefault("realized_pnl", "0")
    pos.setdefault("profit_partial_done", False)
    pos.setdefault("trailing_armed", False)
    return pos


def _intent_id(position, reason, attempt):
    identity = str(position.get("position_id") or position.get("buy_tx") or position.get("token"))
    raw = f"{identity}:{reason}:{attempt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persist_intent(intent):
    state = trader.load_state()
    token = intent["token"]
    state.setdefault("pending_sells", {})[token] = dict(intent)
    state.setdefault("sell_intents", {})[intent["sell_intent_id"]] = dict(intent)
    state["updated_at"] = trader.now()
    trader.save_state(state)


def _update_intent(token, intent):
    state = trader.load_state()
    state.setdefault("pending_sells", {})[token] = dict(intent)
    state.setdefault("sell_intents", {})[intent["sell_intent_id"]] = dict(intent)
    state["updated_at"] = trader.now()
    trader.save_state(state)


def _finish_intent(token, intent, sold_raw, proceeds_raw):
    state = trader.load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        raise RuntimeError("Position disappeared before confirmed SELL accounting")
    proceeds_sda = Decimal(int(proceeds_raw)) / Decimal(10**18)
    updated = policy.apply_confirmed_partial(
        _position_defaults(position), int(sold_raw), proceeds_sda, trader.now()
    )
    reason = intent["sell_reason"]
    if reason != "PROFIT_PARTIAL_25":
        updated["trailing_armed"] = bool(position.get("trailing_armed"))
        updated["profit_partial_done"] = bool(position.get("profit_partial_done"))
    updated["amount_sda"] = float(Decimal(updated["remaining_cost_basis"]))
    updated["wallet_available_quantity"] = int(updated["remaining_quantity"])
    if int(updated["remaining_quantity"]) <= 0:
        state["positions"].pop(token, None)
    else:
        updated["status"] = "OPEN"
        state["positions"][token] = updated

    allocated_cost = Decimal(updated["sold_cost_basis"]) - Decimal(
        position.get("sold_cost_basis", "0") or "0"
    )
    realized_increment = proceeds_sda - allocated_cost
    state["daily_realized_pnl_sda"] = (
        float(state.get("daily_realized_pnl_sda") or 0) + float(realized_increment)
    )
    intent.update({
        "status": "RECONCILED", "stage": "RECONCILED",
        "confirmed_sold_quantity_raw": int(sold_raw),
        "confirmed_proceeds_raw": int(proceeds_raw),
        "proceeds_sda": float(proceeds_sda),
        "allocated_cost_basis_sda": float(allocated_cost),
        "realized_pnl_sda": float(realized_increment),
        "confirmed_at": trader.now(),
    })
    state.setdefault("sell_intents", {})[intent["sell_intent_id"]] = dict(intent)
    state.setdefault("pending_sells", {}).pop(token, None)
    state.setdefault("trades", []).append({
        "action": "SELL", "symbol": intent["symbol"], "token": token,
        "tx": intent.get("swap_tx"), "unwrap_tx": intent.get("unwrap_tx"),
        "sell_intent_id": intent["sell_intent_id"],
        "sell_reason": reason, "sold_quantity_raw": int(sold_raw),
        "amount_sda": float(allocated_cost), "proceeds_sda": float(proceeds_sda),
        "realized_pnl_sda": float(realized_increment), "at": trader.now(),
    })
    state["updated_at"] = trader.now()
    trader.save_state(state)
    return {"action": "SELL", "status": "CONFIRMED", "token": token,
            "tx": intent.get("swap_tx"), "unwrap_tx": intent.get("unwrap_tx"),
            "sold_quantity_raw": int(sold_raw), "proceeds_sda": float(proceeds_sda),
            "realized_pnl_sda": float(realized_increment)}


def _resolve_existing(token, intent):
    """Resolve persisted tx hashes. Never resubmit an UNKNOWN transaction."""
    stage = intent.get("stage")
    tx_hash = intent.get("tx_hash")
    if stage in {"APPROVAL_SUBMITTED", "SWAP_SUBMITTED", "UNWRAP_SUBMITTED"}:
        if not tx_hash:
            intent["stage"] = "UNKNOWN"
            intent["status"] = "UNKNOWN"
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNKNOWN", "token": token}
        try:
            receipt = trader.rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx_hash}
        if not receipt:
            return {"action": "SELL", "status": "PENDING_CONFIRMATION", "token": token, "tx": tx_hash}
        ok = int(receipt.get("status", "0x0"), 16) == 1
        if not ok:
            if stage == "APPROVAL_SUBMITTED":
                intent["stage"] = "FAILED"
                intent["status"] = "FAILED"
                _update_intent(token, intent)
                return {"action": "SELL", "status": "APPROVAL_FAILED", "token": token, "tx": tx_hash}
            if stage == "SWAP_SUBMITTED":
                intent["stage"] = "FAILED"
                intent["status"] = "FAILED"
                _update_intent(token, intent)
                state = trader.load_state()
                state.setdefault("pending_sells", {}).pop(token, None)
                trader.save_state(state)
                return {"action": "SELL", "status": "SWAP_FAILED", "token": token, "tx": tx_hash}
            # A reverted unwrap does not undo the already-confirmed swap.
            intent.pop("tx_hash", None)
            intent["stage"] = "SWAP_CONFIRMED"
            _update_intent(token, intent)
        elif stage == "APPROVAL_SUBMITTED":
            intent.pop("tx_hash", None)
            intent["approval_tx"] = tx_hash
            owner = trader.wallet_address()
            if int(trader.allowance(token, owner, cfg.ROUTER_ADDRESS)) < int(intent["requested_quantity_raw"]):
                intent["stage"] = "FAILED"
                intent["status"] = "FAILED"
                _update_intent(token, intent)
                state = trader.load_state()
                state.setdefault("pending_sells", {}).pop(token, None)
                trader.save_state(state)
                return {"action": "SELL", "status": "ALLOWANCE_NOT_CONFIRMED", "token": token}
            intent["stage"] = "SELL_READY"
            _update_intent(token, intent)
        elif stage == "SWAP_SUBMITTED":
            intent.pop("tx_hash", None)
            intent["swap_tx"] = tx_hash
            intent["stage"] = "SWAP_CONFIRMED"
            try:
                owner = trader.wallet_address()
                token_after = trader.token_balance(token, owner)
                wsda_after = trader.token_balance(cfg.WSDA_ADDRESS, owner)
            except Exception:
                intent["status"] = "UNKNOWN"
                _update_intent(token, intent)
                return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx_hash}
            intent["confirmed_token_balance_after"] = token_after
            intent["confirmed_wsda_balance_after"] = wsda_after
            intent["confirmed_sold_quantity_raw"] = max(
                0, int(intent["token_balance_before"]) - int(token_after)
            )
            intent["confirmed_proceeds_raw"] = max(
                0, int(wsda_after) - int(intent["wsda_balance_before"])
            )
            _update_intent(token, intent)
        elif stage == "UNWRAP_SUBMITTED":
            intent.pop("tx_hash", None)
            intent["unwrap_tx"] = tx_hash
            intent["stage"] = "RECONCILED_READY"
            _update_intent(token, intent)
    if intent.get("stage") == "SWAP_CONFIRMED":
        sold = int(intent.get("confirmed_sold_quantity_raw") or 0)
        proceeds = int(intent.get("confirmed_proceeds_raw") or 0)
        if sold <= 0 or proceeds <= 0:
            try:
                owner = trader.wallet_address()
                token_after = trader.token_balance(token, owner)
                wsda_after = trader.token_balance(cfg.WSDA_ADDRESS, owner)
                sold = max(0, int(intent["token_balance_before"]) - int(token_after))
                proceeds = max(0, int(wsda_after) - int(intent["wsda_balance_before"]))
                intent["confirmed_token_balance_after"] = token_after
                intent["confirmed_wsda_balance_after"] = wsda_after
                intent["confirmed_sold_quantity_raw"] = sold
                intent["confirmed_proceeds_raw"] = proceeds
                _update_intent(token, intent)
            except Exception:
                return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
        if sold <= 0 or proceeds <= 0:
            return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
        # Unwrap the exact confirmed WSDA received, not the quote/minimum.
        account = trader.private_account()
        unwrap_data = cfg.UNWRAP_SELECTOR + trader.word(proceeds)
        def persist_unwrap(signed):
            intent.update({"stage": "UNWRAP_SUBMITTED", "status": "SUBMITTED",
                           "tx_hash": signed["tx_hash"], "unwrap_tx": signed["tx_hash"],
                           "nonce": signed["nonce"], "gas": signed["gas"],
                           "gas_price": signed["gas_price"]})
            _update_intent(token, intent)
        try:
            tx, _, _ = trader.send_transaction(
                account, cfg.WSDA_ADDRESS, unwrap_data, 0, 100000,
                before_broadcast=persist_unwrap,
            )
        except Exception:
            # Hash was durably persisted before broadcast; next evaluation resolves it.
            return {"action": "SELL", "status": "UNKNOWN", "token": token,
                    "tx": intent.get("tx_hash")}
        intent.update({"stage": "UNWRAP_SUBMITTED", "status": "SUBMITTED",
                       "tx_hash": tx, "unwrap_tx": tx})
        _update_intent(token, intent)
        try:
            receipt = trader.rpc("eth_getTransactionReceipt", [tx])
        except Exception:
            return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx}
        if not receipt:
            return {"action": "SELL", "status": "PENDING_CONFIRMATION", "token": token, "tx": tx}
        if int(receipt.get("status", "0x0"), 16) != 1:
            intent.pop("tx_hash", None)
            intent["stage"] = "SWAP_CONFIRMED"
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNWRAP_FAILED", "token": token, "tx": tx}
        intent.pop("tx_hash", None)
        intent["stage"] = "RECONCILED_READY"
        _update_intent(token, intent)
    if intent.get("stage") == "RECONCILED_READY":
        return _finish_intent(token, intent,
                              int(intent.get("confirmed_sold_quantity_raw") or 0),
                              int(intent.get("confirmed_proceeds_raw") or 0))
    if intent.get("stage") == "SELL_READY":
        return _submit_swap(token, intent)
    if intent.get("stage") == "DECIDED":
        # No signed hash was persisted, so the transaction was not broadcast.
        intent["stage"] = "FAILED"
        intent["status"] = "FAILED"
        _update_intent(token, intent)
        state = trader.load_state()
        state.setdefault("pending_sells", {}).pop(token, None)
        trader.save_state(state)
        return {"action": "SELL", "status": "PRE_BROADCAST_ABORTED", "token": token}
    return {"action": "SELL", "status": intent.get("status", "UNKNOWN"), "token": token}


def _submit_swap(token, intent):
    account = trader.private_account()
    owner = account.address
    reconciliation = reconcile_position_wallet(token)
    if reconciliation.get("status") == "CLOSED_MANUAL_RECONCILED":
        return {"action": "SELL", "status": "MANUAL_CLOSED", "token": token}
    state = trader.load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        return {"action": "SELL", "status": "NO_POSITION", "token": token}
    wallet_qty = int(reconciliation.get("wallet_quantity") or 0)
    tracked_qty = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
    fraction = Decimal("0.20") if intent.get("sell_reason") == "PROFIT_PARTIAL_25" else Decimal("1")
    requested = int((Decimal(tracked_qty) * fraction).to_integral_value(rounding=ROUND_DOWN))
    actual = min(requested, wallet_qty)
    if actual <= 0:
        return {"action": "SELL", "status": "NO_WALLET_BALANCE", "token": token}
    # Refresh both baselines immediately before the swap. A manual disposal
    # during approval has already been cost-allocated by wallet reconciliation.
    intent["token_balance_before"] = wallet_qty
    intent["wsda_balance_before"] = int(trader.token_balance(cfg.WSDA_ADDRESS, owner))
    _update_intent(token, intent)
    # Fresh executable quote is intentionally obtained only AFTER approval.
    fee, quoted, _, deadline = trader.quote_best_live_fee(
        token, cfg.WSDA_ADDRESS, token, owner, actual, value_wei=0
    )
    minimum = trader.min_out(quoted)
    calldata = trader.build_exact_input_single(
        token, cfg.WSDA_ADDRESS, fee, owner, deadline, actual, minimum
    )
    intent.update({
        "requested_quantity_raw": actual, "fee_tier": fee,
        "quoted_wsda_raw": int(quoted), "minimum_wsda_raw": int(minimum),
        "reference_price": intent.get("reference_price"),
        "stage": "DECIDED", "status": "DECIDED",
    })
    _update_intent(token, intent)
    def persist_swap(signed):
        intent.update({"stage": "SWAP_SUBMITTED", "status": "SUBMITTED",
                       "tx_hash": signed["tx_hash"], "swap_tx": signed["tx_hash"],
                       "nonce": signed["nonce"], "gas": signed["gas"],
                       "gas_price": signed["gas_price"]})
        _update_intent(token, intent)
    try:
        tx, _, _ = trader.send_transaction(
            account, cfg.ROUTER_ADDRESS, calldata, 0, cfg.GAS_LIMIT,
            before_broadcast=persist_swap,
        )
    except Exception:
        return {"action": "SELL", "status": "UNKNOWN", "token": token,
                "tx": intent.get("tx_hash")}
    intent.update({"stage": "SWAP_SUBMITTED", "status": "SUBMITTED",
                   "tx_hash": tx, "swap_tx": tx})
    _update_intent(token, intent)
    try:
        receipt = trader.rpc("eth_getTransactionReceipt", [tx])
    except Exception:
        return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx}
    if not receipt:
        return {"action": "SELL", "status": "PENDING_CONFIRMATION", "token": token, "tx": tx}
    if int(receipt.get("status", "0x0"), 16) != 1:
        intent["stage"] = "FAILED"
        intent["status"] = "FAILED"
        _update_intent(token, intent)
        state = trader.load_state()
        state.setdefault("pending_sells", {}).pop(token, None)
        trader.save_state(state)
        return {"action": "SELL", "status": "SWAP_FAILED", "token": token, "tx": tx}
    intent["stage"] = "SWAP_CONFIRMED"
    intent.pop("tx_hash", None)
    intent["swap_tx"] = tx
    try:
        token_after = trader.token_balance(token, owner)
        wsda_after = trader.token_balance(cfg.WSDA_ADDRESS, owner)
    except Exception:
        intent["status"] = "UNKNOWN"
        _update_intent(token, intent)
        return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
    intent["confirmed_token_balance_after"] = token_after
    intent["confirmed_wsda_balance_after"] = wsda_after
    intent["confirmed_sold_quantity_raw"] = max(
        0, int(intent["token_balance_before"]) - int(token_after)
    )
    intent["confirmed_proceeds_raw"] = max(
        0, int(wsda_after) - int(intent["wsda_balance_before"])
    )
    _update_intent(token, intent)
    return _resolve_existing(token, intent)


def _new_intent(token, symbol, position, decision, market_price, price_timestamp):
    reason_map = {
        "FIRST_PROFIT_PARTIAL": "PROFIT_PARTIAL_25",
        "LOSS_LIMIT": "STOP_LOSS",
        "HARD_TP": "HARD_TP",
        "TRAILING_STOP": "TRAILING_STOP",
    }
    reason = reason_map[decision["reason"]]
    state = trader.load_state()
    counters = state.setdefault("sell_intent_counters", {})
    counter_key = f"{position.get('position_id') or position.get('buy_tx') or token}:{reason}"
    attempt = int(counters.get(counter_key, 0))
    intent_id = _intent_id(position, reason, attempt)
    existing = state.setdefault("sell_intents", {}).get(intent_id)
    if existing and existing.get("status") in {"SUBMITTED", "UNKNOWN", "DECIDED"}:
        return None, existing
    counters[counter_key] = attempt + 1
    quantity = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
    fraction = Decimal("0.20") if decision["action"] == "PARTIAL_SELL" else Decimal("1")
    requested = int((Decimal(quantity) * fraction).to_integral_value(rounding=ROUND_DOWN))
    if requested <= 0:
        return None, None
    intent = {
        "sell_intent_id": intent_id, "idempotency_key": intent_id,
        "token": token, "asset": token,
        "position_id": str(position.get("position_id") or position.get("buy_tx") or token),
        "symbol": symbol, "sell_reason": reason,
        "requested_quantity_raw": requested,
        "requested_percentage": float(fraction * 100),
        "reference_price": str(market_price),
        "price_timestamp": price_timestamp,
        "timestamp": trader.now(), "created_at": trader.now(),
        "status": "DECIDED", "stage": "DECIDED",
    }
    state.setdefault("pending_sells", {})[token] = intent
    state.setdefault("sell_intents", {})[intent_id] = intent
    state["updated_at"] = trader.now()
    trader.save_state(state)
    return intent, None


def evaluate_position(token, symbol, market_price, price_timestamp=None):
    token = trader.addr(token)
    with _token_lock(token):
        migrated = migrate_if_needed()
        state = trader.load_state()
        existing = state.setdefault("pending_sells", {}).get(token)
        if isinstance(existing, dict):
            return _resolve_existing(token, existing)

        reconciled = reconcile_position_wallet(token)
        if reconciled["status"] == "CLOSED_MANUAL_RECONCILED":
            return {"action": "SELL", "status": "MANUAL_CLOSED", "token": token}
        state = trader.load_state()
        position = state.get("positions", {}).get(token)
        if not isinstance(position, dict):
            return {"action": "HOLD", "status": "NO_POSITION", "token": token}
        if migrated:
            return {"action": "HOLD", "status": "MIGRATION_BASELINE", "token": token}
        price = Decimal(str(market_price))
        if price <= 0:
            return {"action": "HOLD", "status": "PRICE_UNAVAILABLE", "token": token}
        position = _position_defaults(position)
        decimals = trader.token_decimals(token)
        quantity_human = Decimal(int(position["remaining_quantity"])) / (Decimal(10) ** decimals)
        cost = Decimal(str(position["remaining_cost_basis"]))
        if quantity_human <= 0 or cost <= 0:
            return {"action": "HOLD", "status": "INVALID_POSITION_BASIS", "token": token}
        entry_price = cost / quantity_human
        pnl_fraction = price / entry_price - Decimal(1)
        position["last_market_price"] = str(price)
        if position.get("trailing_armed"):
            position = policy.update_trailing_peak(position, price)
        decision = policy.decide(position, pnl_fraction, price)
        # Persist HWM and normalized fields on every fresh observation.
        state = trader.load_state()
        current = state.get("positions", {}).get(token)
        if not isinstance(current, dict):
            return {"action": "HOLD", "status": "NO_POSITION", "token": token}
        current.update(position)
        state["positions"][token] = current
        trader.save_state(state)
        if decision["action"] == "HOLD":
            return {"action": "HOLD", "status": decision["reason"], "token": token,
                    "pnl_pct": float(pnl_fraction * 100)}
        intent, existing_intent = _new_intent(
            token, symbol, current, decision, price, price_timestamp
        )
        if intent is None:
            return {"action": "SELL", "status": "INTENT_ALREADY_EXISTS", "token": token}
        _persist_intent(intent)
        if not cfg.REAL_TRADING_ENABLED or cfg.REAL_TRADING_DRY_RUN:
            return {"action": "SELL", "status": "DRY_RUN", "token": token,
                    "sell_intent_id": intent["sell_intent_id"]}
        account = trader.private_account()
        if account.address.lower() != trader.wallet_address().lower():
            raise RuntimeError("Trading wallet mismatch")
        if trader.chain_id() != cfg.CHAIN_ID:
            raise RuntimeError("Wrong Sidra Chain ID")
        wallet_qty = int(trader.token_balance(token, account.address))
        actual_qty = min(int(intent["requested_quantity_raw"]), wallet_qty)
        if actual_qty <= 0:
            return {"action": "SELL", "status": "NO_WALLET_BALANCE", "token": token}
        intent["requested_quantity_raw"] = actual_qty
        intent["token_balance_before"] = wallet_qty
        intent["wsda_balance_before"] = int(trader.token_balance(cfg.WSDA_ADDRESS, account.address))
        _update_intent(token, intent)
        current_allowance = int(trader.allowance(token, account.address, cfg.ROUTER_ADDRESS))
        if current_allowance < actual_qty:
            approval_data = cfg.APPROVE_SELECTOR + trader.address_word(cfg.ROUTER_ADDRESS) + trader.word((1 << 256) - 1)
            def persist_approval(signed):
                intent.update({"stage": "APPROVAL_SUBMITTED", "status": "SUBMITTED",
                               "tx_hash": signed["tx_hash"], "approval_tx": signed["tx_hash"],
                               "nonce": signed["nonce"], "gas": signed["gas"],
                               "gas_price": signed["gas_price"]})
                _update_intent(token, intent)
            try:
                tx, _, _ = trader.send_transaction(
                    account, token, approval_data, 0, 100000,
                    before_broadcast=persist_approval,
                )
            except Exception:
                return {"action": "SELL", "status": "UNKNOWN", "token": token,
                        "tx": intent.get("tx_hash")}
            intent.update({"stage": "APPROVAL_SUBMITTED", "status": "SUBMITTED",
                           "tx_hash": tx, "approval_tx": tx})
            _update_intent(token, intent)
            try:
                receipt = trader.rpc("eth_getTransactionReceipt", [tx])
            except Exception:
                return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx}
            if not receipt:
                return {"action": "SELL", "status": "PENDING_CONFIRMATION", "token": token, "tx": tx}
            if int(receipt.get("status", "0x0"), 16) != 1:
                intent["stage"] = "FAILED"
                intent["status"] = "FAILED"
                _update_intent(token, intent)
                state = trader.load_state()
                state.setdefault("pending_sells", {}).pop(token, None)
                trader.save_state(state)
                return {"action": "SELL", "status": "APPROVAL_FAILED", "token": token}
            if int(trader.allowance(token, account.address, cfg.ROUTER_ADDRESS)) < actual_qty:
                return {"action": "SELL", "status": "ALLOWANCE_NOT_CONFIRMED", "token": token}
            intent.pop("tx_hash", None)
            intent["stage"] = "SELL_READY"
            intent["status"] = "DECIDED"
            _update_intent(token, intent)
        else:
            intent["stage"] = "SELL_READY"
            _update_intent(token, intent)
        return _submit_swap(token, intent)


def startup_recovery():
    """Inspect all current v2 intents; no historical intent is ever replayed."""
    migrate_if_needed()
    state = trader.load_state()
    results = []
    for token, intent in list(state.get("pending_sells", {}).items()):
        if isinstance(intent, dict):
            with _token_lock(token):
                results.append(_resolve_existing(token, intent))
    return results
