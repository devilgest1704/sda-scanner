"""Live, idempotent Swing SELL V2 execution adapter.

BUY is intentionally not imported or modified here. All transaction paths are
SELL-only and are guarded by a per-token process lock. No transaction is sent
unless REAL trading is enabled and dry-run is false.

Intent lifecycle (persisted in ``pending_sells`` / ``sell_intents``):

CREATED -> APPROVAL_PENDING -> APPROVAL_SUBMITTED -> APPROVAL_CONFIRMED
-> READY_TO_SWAP -> SWAP_SUBMITTED -> SWAP_CONFIRMED -> UNWRAP_SUBMITTED
-> UNWRAP_CONFIRMED -> CONFIRMED, with terminal FAILED / DROPPED / QUARANTINED
(and CANCELLED when the position disappears before broadcast).

A confirmed swap is applied to the position exactly once (``accounting_applied``)
before any wallet reconciliation may compare balances again.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import real_trader as trader
import real_trading_config as cfg
import swing_sell_v2 as policy

PRE_SWAP_STAGES = {
    "CREATED", "APPROVAL_PENDING", "APPROVAL_SUBMITTED", "APPROVAL_CONFIRMED", "READY_TO_SWAP",
}
POST_SWAP_STAGES = {
    "SWAP_SUBMITTED", "SWAP_CONFIRMED", "UNWRAP_SUBMITTED", "UNWRAP_CONFIRMED", "UNKNOWN",
}
ACTIVE_STAGES = PRE_SWAP_STAGES | POST_SWAP_STAGES
TERMINAL_STAGES = {"CONFIRMED", "FAILED", "DROPPED", "QUARANTINED", "CANCELLED"}
LEGACY_STAGE_ALIASES = {
    "DECIDED": "CREATED", "SELL_READY": "READY_TO_SWAP",
    "RECONCILED_READY": "UNWRAP_CONFIRMED", "RECONCILED": "CONFIRMED",
}
SUBMITTED_KIND = {
    "APPROVAL_SUBMITTED": "APPROVAL", "SWAP_SUBMITTED": "SWAP", "UNWRAP_SUBMITTED": "UNWRAP",
}

RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600)
MAX_TRANSIENT_FAILURES = 10
MAX_SWAP_REVERTS = 4
MAX_UNWRAP_FAILURES = 3
MAX_ACCOUNTING_CHECKS = 10
QUARANTINE_REVIEW_SECONDS = 24 * 3600
TX_ABSENCE_GRACE_SECONDS = 120
NON_SWAP_DROP_SECONDS = 900
SWAP_DEADLINE_MARGIN_SECONDS = 60

DETERMINISTIC_MARKERS = (
    "STF", "SPL", "EXECUTION REVERTED", "NO LIVE PINETSWAP QUOTE", "NO VALID ROUTE",
    "TRANSFER_FAILED", "TRANSFER FAILED", "ALLOWANCE",
)
TRANSIENT_MARKERS = (
    "TIMEOUT", "TIMED OUT", "CONNECTION", "TEMPORAR", "502", "503", "504", "522",
    "MAX RETRIES", "RATE LIMIT", "429", "UNAVAILABLE", "RESET BY PEER",
)

REASON_MAP = {
    "FIRST_PROFIT_PARTIAL": "PROFIT_PARTIAL_25",
    "LOSS_LIMIT": "STOP_LOSS",
    "HARD_TP": "HARD_TP",
    "TRAILING_STOP": "TRAILING_STOP",
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _confirmed_transfer_deltas(receipt, token, wallet):
    """Extract exact wallet token outflow and WSDA inflow from this tx receipt."""
    sold = 0
    proceeds = 0
    wallet = trader.addr(wallet)
    token = trader.addr(token)
    for log in (receipt or {}).get("logs", []):
        try:
            if str(log.get("topics", [""])[0]).lower() != TRANSFER_TOPIC:
                continue
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            sender = "0x" + str(topics[1])[-40:].lower()
            recipient = "0x" + str(topics[2])[-40:].lower()
            amount = int(str(log.get("data") or "0x0"), 16)
            contract = trader.addr(log.get("address"))
            if contract == token and sender == wallet:
                sold += amount
            if contract == trader.addr(cfg.WSDA_ADDRESS) and recipient == wallet:
                proceeds += amount
        except (TypeError, ValueError, IndexError):
            continue
    return sold, proceeds


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


def _parse_time(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else None


def _now_dt():
    return _parse_time(trader.now()) or datetime.now(timezone.utc)


def _stage(intent):
    stage = str((intent or {}).get("stage") or "").upper()
    return LEGACY_STAGE_ALIASES.get(stage, stage)


def _normalize(intent):
    stage = _stage(intent)
    if stage and intent.get("stage") != stage:
        intent["legacy_stage"] = intent.get("stage")
        intent["stage"] = stage
    return intent


def blocks_wallet_reconciliation(intent):
    """True while a known automatic SELL may already explain a wallet reduction."""
    if not isinstance(intent, dict) or intent.get("accounting_applied"):
        return False
    stage = _stage(intent)
    return stage in POST_SWAP_STAGES


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


def _dust_decimals(token, position, quantity_raw):
    """Decimals when the BOT residual is negligible dust under the shared policy, else None."""
    try:
        decimals = trader.token_decimals(token)
        cost = position.get("remaining_cost_basis", position.get("amount_sda", position.get("cost_sda")))
        if cfg.is_dust_quantity(quantity_raw, decimals, cost, position.get("last_market_price")):
            return decimals
    except Exception:
        pass
    return None


def _close_dust_position(state, token, position, wallet_qty, tracked_qty, decimals):
    """Close the BOT position; the residual stays in the wallet and is only audited."""
    state["positions"].pop(token, None)
    position_id = str(position.get("position_id") or position.get("buy_tx") or token)
    records = state.setdefault("dust_reconciliations", [])
    if any(isinstance(item, dict) and item.get("position_id") == position_id for item in records):
        return
    records.append({
        "action": "DUST_RECONCILIATION", "token": token, "symbol": position.get("symbol"),
        "position_id": position_id, "wallet_quantity_raw": int(wallet_qty),
        "wallet_quantity": str(Decimal(int(wallet_qty)) / (Decimal(10) ** decimals)),
        "closed_bot_quantity_raw": int(tracked_qty),
        "remaining_cost_basis": str(position.get("remaining_cost_basis",
                                                 position.get("amount_sda", 0)) or 0),
        "dust_threshold_token_quantity": str(cfg.DUST_TOKEN_QUANTITY),
        "reason": "NEGLIGIBLE_RESIDUAL_AFTER_DISPOSAL", "at": trader.now(),
    })


def reconcile_position_wallet(token):
    """Wallet balance is authoritative; manual disposals never create fake proceeds.

    A reduction that an unfinished automatic SELL may explain is never
    classified as manual: callers must first resolve that intent. A remaining
    negligible residual (shared dust policy) closes the BOT position without a SELL.
    """
    token = trader.addr(token)
    state = trader.load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        return {"status": "NO_POSITION"}
    pending = state.get("pending_sells", {}).get(token)
    if blocks_wallet_reconciliation(pending):
        return {"status": "SELL_INTENT_ACTIVE", "stage": _stage(pending),
                "sell_intent_id": pending.get("sell_intent_id")}
    owner = trader.wallet_address()
    if not owner:
        raise RuntimeError("REAL wallet address unavailable")
    wallet_qty = int(trader.token_balance(token, owner))
    recorded = int(position.get("remaining_quantity",
                                position.get("token_amount_raw", 0)) or 0)
    if wallet_qty < recorded:
        _record_manual_disposal(state, token, position, recorded, wallet_qty)
        adjusted = policy.reconcile_wallet_quantity(position, wallet_qty)
        dust_decimals = _dust_decimals(token, adjusted, wallet_qty) if wallet_qty > 0 else None
        if wallet_qty == 0:
            state["positions"].pop(token, None)
            adjusted["status"] = "CLOSED_MANUAL_RECONCILED"
            state.setdefault("manual_disposals", []).append({
                "token": token, "quantity_raw": recorded,
                "proceeds_sda": None, "realized_pnl_sda": None,
                "status": "PROCEEDS_UNKNOWN", "at": trader.now(),
            })
        elif dust_decimals is not None:
            _close_dust_position(state, token, adjusted, wallet_qty, wallet_qty, dust_decimals)
            state.setdefault("manual_disposals", []).append({
                "token": token, "quantity_raw": recorded - wallet_qty,
                "proceeds_sda": None, "realized_pnl_sda": None,
                "status": "PROCEEDS_UNKNOWN", "at": trader.now(),
            })
        else:
            adjusted["wallet_available_quantity"] = wallet_qty
            state["positions"][token] = adjusted
        state["accounting_refresh_pending"] = True
        state["updated_at"] = trader.now()
        trader.save_state(state)
        _request_accounting_refresh(state)
        if dust_decimals is not None:
            return {"status": "DUST_RECONCILED", "manual_disposal": True,
                    "wallet_quantity": wallet_qty, "previous_quantity": recorded}
        return {"status": "CLOSED_MANUAL_RECONCILED" if wallet_qty == 0 else "MANUAL_RECONCILED",
                "wallet_quantity": wallet_qty, "previous_quantity": recorded}
    # Residual left by an earlier disposal (manual or automatic); external excess is not BOT quantity.
    dust_decimals = _dust_decimals(token, position, recorded)
    if dust_decimals is not None:
        _close_dust_position(state, token, position, wallet_qty, recorded, dust_decimals)
        state["updated_at"] = trader.now()
        trader.save_state(state)
        return {"status": "DUST_RECONCILED", "manual_disposal": False,
                "wallet_quantity": wallet_qty, "previous_quantity": recorded}
    if wallet_qty == recorded:
        position["wallet_available_quantity"] = wallet_qty
        return {"status": "UNCHANGED", "wallet_quantity": wallet_qty}
    # External deposits are not silently added to the bot's costed position.
    position["wallet_available_quantity"] = wallet_qty
    trader.save_state(state)
    return {"status": "WALLET_EXCESS_IGNORED", "wallet_quantity": wallet_qty,
            "tracked_quantity": recorded}


def reconcile_position(token):
    """Bridge entry: finish any post-swap automatic SELL first, then compare the wallet."""
    token = trader.addr(token)
    with _token_lock(token):
        intent_result = None
        state = trader.load_state()
        pending = state.get("pending_sells", {}).get(token)
        if isinstance(pending, dict) and _stage(pending) not in PRE_SWAP_STAGES:
            intent_result = _resolve_existing(token, pending)
        wallet = reconcile_position_wallet(token)
        return {"intent": intent_result, "wallet": wallet}


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


def _close_intent(token, intent, stage, status):
    """Persist a terminal stage and release the token for future decisions."""
    intent["stage"] = stage
    intent["status"] = status
    intent["closed_at"] = trader.now()
    intent.pop("tx_hash", None)
    state = trader.load_state()
    state.setdefault("sell_intents", {})[intent["sell_intent_id"]] = dict(intent)
    pending = state.setdefault("pending_sells", {})
    if (pending.get(token) or {}).get("sell_intent_id") == intent["sell_intent_id"]:
        pending.pop(token, None)
    state["updated_at"] = trader.now()
    trader.save_state(state)


def _cancel_intent(token, intent, status):
    _close_intent(token, intent, "CANCELLED", status)


def _retry_key(position_id, reason):
    return f"{position_id}:{reason}"


def classify_failure(detail):
    """Return True for deterministic (route/token) failures, False for transient ones."""
    text = str(detail or "").upper()
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return False
    return any(marker in text for marker in DETERMINISTIC_MARKERS)


def _record_failure(token, intent, code, detail, deterministic):
    """Persist bounded backoff; reproducible deterministic failures quarantine the intent."""
    now = _now_dt()
    state = trader.load_state()
    retry = state.setdefault("sell_retry", {})
    key = _retry_key(intent.get("position_id") or token, intent.get("sell_reason"))
    record = dict(retry.get(key) or {})
    record.update({"token": token, "position_id": intent.get("position_id"),
                   "sell_reason": intent.get("sell_reason")})
    record["consecutive_failures"] = int(record.get("consecutive_failures") or 0) + 1
    if code == "SWAP_REVERTED":
        record["swap_reverts"] = int(record.get("swap_reverts") or 0) + 1
    quarantine = False
    if deterministic:
        quarantine = record.get("last_deterministic_code") == code
        record["last_deterministic_code"] = code
    else:
        record["last_deterministic_code"] = None
    if record["consecutive_failures"] >= MAX_TRANSIENT_FAILURES:
        quarantine = True
    if int(record.get("swap_reverts") or 0) >= MAX_SWAP_REVERTS:
        quarantine = True
    if code == "ALLOWANCE_NOT_CONFIRMED":
        quarantine = True
    delay = RETRY_BACKOFF_SECONDS[min(record["consecutive_failures"], len(RETRY_BACKOFF_SECONDS)) - 1]
    record.update({
        "last_error_code": code, "last_error": str(detail or "")[:300],
        "classification": "DETERMINISTIC" if deterministic else "TRANSIENT",
        "last_failure_at": now.isoformat(), "last_intent_id": intent.get("sell_intent_id"),
        "next_retry_at": (now + timedelta(seconds=delay)).isoformat(),
        "quarantined": quarantine,
    })
    if quarantine:
        record["quarantined_at"] = now.isoformat()
        record["review_after"] = (now + timedelta(seconds=QUARANTINE_REVIEW_SECONDS)).isoformat()
    retry[key] = record
    trader.save_state(state)
    return record


def _fail(token, intent, code, detail="", deterministic=False, stage="FAILED"):
    intent["failure_code"] = code
    intent["failure_reason"] = str(detail or code)[:300]
    if cfg.REAL_TRADING_ENABLED and not cfg.REAL_TRADING_DRY_RUN:
        record = _record_failure(token, intent, code, detail, deterministic)
    else:
        record = {"quarantined": False}
    if record.get("quarantined"):
        _close_intent(token, intent, "QUARANTINED", "QUARANTINED")
        return {"action": "SELL", "status": "QUARANTINED", "token": token,
                "failure_code": code, "sell_intent_id": intent.get("sell_intent_id")}
    _close_intent(token, intent, stage, stage)
    return {"action": "SELL", "status": code, "token": token, "stage": stage,
            "next_retry_at": record.get("next_retry_at"),
            "sell_intent_id": intent.get("sell_intent_id")}


def _clear_retry(state, position_id):
    retry = state.get("sell_retry")
    if not isinstance(retry, dict):
        return
    for key in [k for k in retry if str(k).startswith(f"{position_id}:")]:
        retry.pop(key, None)


def _retry_gate(state, position_id, reason):
    """Return a blocking status while a SELL is in backoff or quarantine."""
    record = (state.get("sell_retry") or {}).get(_retry_key(position_id, reason))
    if not isinstance(record, dict):
        return None
    now = _now_dt()
    if record.get("quarantined"):
        review = _parse_time(record.get("review_after"))
        if review is None or now < review:
            return {"status": "SELL_QUARANTINED", "review_after": record.get("review_after"),
                    "failure_code": record.get("last_error_code")}
        return None
    retry_at = _parse_time(record.get("next_retry_at"))
    if retry_at is not None and now < retry_at:
        return {"status": "SELL_BACKOFF", "next_retry_at": record.get("next_retry_at"),
                "failure_code": record.get("last_error_code")}
    return None


def _request_accounting_refresh(state):
    """Dispatch the existing read-only FIFO accounting rebuild after real disposals."""
    if not state.get("accounting_refresh_pending"):
        return "NOT_PENDING"
    now = trader.now()
    previous = state.get("accounting_refresh_last_attempt_at")
    if previous:
        try:
            age = (datetime.fromisoformat(now) -
                   datetime.fromisoformat(str(previous).replace("Z", "+00:00"))).total_seconds()
            if 0 <= age < 60:
                return "THROTTLED"
        except Exception:
            pass
    token = os.getenv("GH_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        state["accounting_refresh_last_error"] = "GH_TOKEN or GITHUB_REPOSITORY unavailable"
        trader.save_state(state)
        return "NOT_CONFIGURED"
    state["accounting_refresh_last_attempt_at"] = now
    try:
        response = trader.requests.post(
            f"https://api.github.com/repos/{repository}/actions/workflows/real-accounting-readonly.yml/dispatches",
            json={"ref": "main"},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=(3, 10),
        )
        if int(response.status_code) not in (200, 201, 204):
            raise RuntimeError(f"workflow dispatch HTTP {response.status_code}")
        state["accounting_refresh_pending"] = False
        state["accounting_refresh_last_dispatched_at"] = now
        state.pop("accounting_refresh_last_error", None)
        for item in state.get("sell_intents", {}).values():
            if isinstance(item, dict) and item.get("accounting_applied"):
                item["accounting_refresh_status"] = "DISPATCHED"
        for item in state.get("manual_disposals", []):
            if isinstance(item, dict):
                item["accounting_refresh_status"] = "DISPATCHED"
        trader.save_state(state)
        return "DISPATCHED"
    except Exception as exc:
        state["accounting_refresh_last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        trader.save_state(state)
        return "FAILED"


def _tx_status(intent, kind):
    """Classify a persisted hash: MINED, PENDING, DROPPED or UNCERTAIN (never guesses)."""
    tx_hash = intent.get("tx_hash")
    try:
        receipt = trader.rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as exc:
        return "UNCERTAIN", f"receipt {type(exc).__name__}: {str(exc)[:120]}"
    if receipt:
        return "MINED", receipt
    try:
        seen = trader.rpc("eth_getTransactionByHash", [tx_hash])
    except Exception:
        return "PENDING", None
    if seen:
        return "PENDING", None
    now = _now_dt()
    submitted = _parse_time(intent.get("submitted_at") or intent.get("timestamp")
                            or intent.get("created_at"))
    if submitted is None:
        return "PENDING", None
    age = (now - submitted).total_seconds()
    if age < TX_ABSENCE_GRACE_SECONDS:
        return "PENDING", None
    try:
        latest_nonce = int(trader.rpc("eth_getTransactionCount",
                                      [trader.wallet_address(), "latest"]), 16)
        receipt = trader.rpc("eth_getTransactionReceipt", [tx_hash])
        seen = trader.rpc("eth_getTransactionByHash", [tx_hash])
    except Exception as exc:
        return "UNCERTAIN", f"nonce {type(exc).__name__}: {str(exc)[:120]}"
    if receipt:
        return "MINED", receipt
    if seen:
        return "PENDING", None
    nonce = intent.get("nonce")
    try:
        nonce_consumed = nonce is not None and latest_nonce > int(nonce)
    except (TypeError, ValueError):
        nonce_consumed = False
    if nonce_consumed:
        return "DROPPED", "NONCE_CONSUMED_BY_OTHER_TRANSACTION"
    if kind == "SWAP":
        deadline = intent.get("deadline")
        try:
            expired = deadline is not None and now.timestamp() > int(deadline) + SWAP_DEADLINE_MARGIN_SECONDS
        except (TypeError, ValueError):
            expired = False
        if expired or (deadline is None and age > cfg.DEADLINE_SECONDS + NON_SWAP_DROP_SECONDS):
            return "DROPPED", "ABSENT_AFTER_SWAP_DEADLINE"
        return "PENDING", None
    if age > NON_SWAP_DROP_SECONDS:
        return "DROPPED", "ABSENT_FROM_CHAIN_AND_MEMPOOL"
    return "PENDING", None


def _handle_dropped(token, intent, kind, detail):
    tx_hash = intent.get("tx_hash")
    intent.setdefault("dropped_transactions", []).append(
        {"tx": tx_hash, "kind": kind, "reason": detail, "at": trader.now()})
    if kind == "SWAP":
        # Never declare a swap dropped while the sold tokens already left the wallet.
        try:
            balance = int(trader.token_balance(token, trader.wallet_address()))
        except Exception as exc:
            intent["status"] = "UNKNOWN"
            intent["last_check_error"] = f"balance {type(exc).__name__}"
            intent["dropped_transactions"].pop()
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx_hash}
        before = int(intent.get("token_balance_before") or 0)
        requested = int(intent.get("requested_quantity_raw") or 0)
        if not before or balance <= before - requested:
            intent["dropped_transactions"].pop()
            intent["status"] = "UNKNOWN"
            intent["last_check_error"] = "wallet already reduced; swap absence not conclusive"
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx_hash}
        intent["wallet_balance_at_drop"] = balance
    if kind == "UNWRAP":
        intent.pop("tx_hash", None)
        intent["stage"] = "SWAP_CONFIRMED"
        intent["status"] = "UNWRAP_DROPPED"
        intent["unwrap_failures"] = int(intent.get("unwrap_failures") or 0) + 1
        _update_intent(token, intent)
        return {"action": "SELL", "status": "UNWRAP_DROPPED", "token": token, "tx": tx_hash}
    return _fail(token, intent, "DROPPED", detail, deterministic=False, stage="DROPPED")


def _apply_confirmed_sell(token, intent):
    """Apply one confirmed swap to the position exactly once."""
    state = trader.load_state()
    stored = state.get("sell_intents", {}).get(intent["sell_intent_id"])
    if intent.get("accounting_applied") or (isinstance(stored, dict) and stored.get("accounting_applied")):
        if isinstance(stored, dict):
            for key in ("accounting_applied", "accounting_result", "accounting_applied_at"):
                if key in stored:
                    intent[key] = stored[key]
        return intent.get("accounting_result") or {}
    sold_raw = int(intent.get("confirmed_sold_quantity_raw") or 0)
    proceeds_raw = int(intent.get("confirmed_proceeds_raw") or 0)
    proceeds_sda = Decimal(proceeds_raw) / Decimal(10**18)
    reason = intent["sell_reason"]
    position = state.get("positions", {}).get(token)
    allocated_cost = None
    realized_increment = None
    note = None
    if isinstance(position, dict):
        base = _position_defaults(position)
        remaining = int(base["remaining_quantity"])
        basis_qty = min(sold_raw, remaining)
        if basis_qty < sold_raw:
            note = "SOLD_EXCEEDS_TRACKED_QUANTITY"
        if basis_qty > 0:
            updated = policy.apply_confirmed_partial(base, basis_qty, proceeds_sda, trader.now())
        else:
            updated = dict(base)
        if reason != "PROFIT_PARTIAL_25":
            updated["trailing_armed"] = bool(position.get("trailing_armed"))
            updated["profit_partial_done"] = bool(position.get("profit_partial_done"))
        updated["amount_sda"] = float(Decimal(updated["remaining_cost_basis"]))
        updated["wallet_available_quantity"] = int(updated["remaining_quantity"])
        allocated_cost = Decimal(updated["sold_cost_basis"]) - Decimal(base.get("sold_cost_basis") or "0")
        realized_increment = proceeds_sda - allocated_cost
        if int(updated["remaining_quantity"]) <= 0:
            state["positions"].pop(token, None)
        else:
            updated["status"] = "OPEN"
            state["positions"][token] = updated
        state["daily_realized_pnl_sda"] = (
            float(state.get("daily_realized_pnl_sda") or 0) + float(realized_increment)
        )
        _clear_retry(state, intent.get("position_id") or token)
    else:
        note = "POSITION_ALREADY_CLOSED"
    result = {
        "sold_quantity_raw": sold_raw, "proceeds_sda": float(proceeds_sda),
        "allocated_cost_basis_sda": float(allocated_cost) if allocated_cost is not None else None,
        "realized_pnl_sda": float(realized_increment) if realized_increment is not None else None,
    }
    now = trader.now()
    intent.update({
        "accounting_applied": True, "accounting_applied_at": now, "accounting_result": result,
        "proceeds_sda": result["proceeds_sda"],
        "allocated_cost_basis_sda": result["allocated_cost_basis_sda"],
        "realized_pnl_sda": result["realized_pnl_sda"],
    })
    if note:
        intent["accounting_note"] = note
    state.setdefault("sell_intents", {})[intent["sell_intent_id"]] = dict(intent)
    state.setdefault("pending_sells", {})[token] = dict(intent)
    trade = {
        "action": "SELL", "symbol": intent.get("symbol"), "token": token,
        "tx": intent.get("swap_tx"), "sell_intent_id": intent["sell_intent_id"],
        "sell_reason": reason, "sold_quantity_raw": sold_raw,
        "amount_sda": result["allocated_cost_basis_sda"], "proceeds_sda": result["proceeds_sda"],
        "realized_pnl_sda": result["realized_pnl_sda"], "at": now,
    }
    if note:
        trade["accounting_status"] = note
    state.setdefault("trades", []).append(trade)
    state["accounting_refresh_pending"] = True
    state["updated_at"] = now
    trader.save_state(state)
    _request_accounting_refresh(state)
    return result


def _complete_intent(token, intent, unwrap_status="CONFIRMED"):
    intent["unwrap_status"] = unwrap_status
    intent["confirmed_at"] = trader.now()
    _close_intent(token, intent, "CONFIRMED", "CONFIRMED")
    state = trader.load_state()
    for trade in reversed(state.get("trades", [])):
        if isinstance(trade, dict) and trade.get("sell_intent_id") == intent["sell_intent_id"]:
            trade["unwrap_tx"] = intent.get("unwrap_tx")
            break
    trader.save_state(state)
    result = intent.get("accounting_result") or {}
    return {"action": "SELL", "status": "CONFIRMED", "token": token,
            "tx": intent.get("swap_tx"), "unwrap_tx": intent.get("unwrap_tx"),
            "sell_reason": intent.get("sell_reason"), **result}


def _ensure_swap_deltas(token, intent):
    before = int(intent.get("token_balance_before") or 0)
    sold = int(intent.get("confirmed_sold_quantity_raw") or 0)
    proceeds = int(intent.get("confirmed_proceeds_raw") or 0)
    if sold > 0 and proceeds > 0 and not (before and sold > before):
        return True
    try:
        receipt = trader.rpc("eth_getTransactionReceipt", [intent.get("swap_tx")])
        if not receipt or int(receipt.get("status", "0x0"), 16) != 1:
            return False
        sold, proceeds = _confirmed_transfer_deltas(receipt, token, trader.wallet_address())
    except Exception:
        return False
    if sold <= 0 or proceeds <= 0 or (before and sold > before):
        return False
    intent["confirmed_sold_quantity_raw"] = sold
    intent["confirmed_proceeds_raw"] = proceeds
    _update_intent(token, intent)
    return True


def _send_unwrap(token, intent):
    if int(intent.get("unwrap_failures") or 0) >= MAX_UNWRAP_FAILURES:
        # Proceeds stay as WSDA; the confirmed swap is already fully accounted.
        return _complete_intent(token, intent, unwrap_status="ABANDONED_WSDA_RETAINED")
    proceeds = int(intent.get("confirmed_proceeds_raw") or 0)
    account = trader.private_account()
    unwrap_data = cfg.UNWRAP_SELECTOR + trader.word(proceeds)

    def persist_unwrap(signed):
        intent.update({"stage": "UNWRAP_SUBMITTED", "status": "SUBMITTED",
                       "tx_hash": signed["tx_hash"], "unwrap_tx": signed["tx_hash"],
                       "nonce": signed["nonce"], "gas": signed["gas"],
                       "gas_price": signed["gas_price"], "submitted_at": trader.now()})
        _update_intent(token, intent)
    try:
        tx, _, _ = trader.send_transaction(
            account, cfg.WSDA_ADDRESS, unwrap_data, 0, 100000,
            before_broadcast=persist_unwrap,
        )
    except Exception as exc:
        if intent.get("stage") == "UNWRAP_SUBMITTED":
            # Hash was persisted; lifecycle checks decide whether it reached the chain.
            return {"action": "SELL", "status": "UNKNOWN", "token": token,
                    "tx": intent.get("tx_hash")}
        intent["status"] = "UNWRAP_DEFERRED"
        intent["last_check_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        _update_intent(token, intent)
        return {"action": "SELL", "status": "UNWRAP_DEFERRED", "token": token}
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
        intent["status"] = "UNWRAP_FAILED"
        intent["unwrap_failures"] = int(intent.get("unwrap_failures") or 0) + 1
        _update_intent(token, intent)
        return {"action": "SELL", "status": "UNWRAP_FAILED", "token": token, "tx": tx}
    intent.pop("tx_hash", None)
    intent["stage"] = "UNWRAP_CONFIRMED"
    intent["status"] = "CONFIRMED"
    _update_intent(token, intent)
    return _complete_intent(token, intent)


def _mark_swap_mined(token, intent, tx_hash, receipt, owner):
    intent.pop("tx_hash", None)
    intent["swap_tx"] = tx_hash
    intent["stage"] = "SWAP_CONFIRMED"
    intent["status"] = "SWAP_CONFIRMED"
    sold_raw, proceeds_raw = _confirmed_transfer_deltas(receipt, token, owner)
    intent["confirmed_sold_quantity_raw"] = sold_raw
    intent["confirmed_proceeds_raw"] = proceeds_raw
    before = int(intent.get("token_balance_before") or 0)
    if sold_raw <= 0 or proceeds_raw <= 0 or (before and sold_raw > before):
        intent["confirmed_sold_quantity_raw"] = 0
        intent["confirmed_proceeds_raw"] = 0
        intent["status"] = "UNKNOWN"
        _update_intent(token, intent)
        return False
    _update_intent(token, intent)
    return True


def _resolve_existing(token, intent):
    """Resolve persisted intents. Never resubmit a transaction that may still exist."""
    intent = _normalize(intent)
    stage = intent.get("stage")
    if stage in SUBMITTED_KIND:
        kind = SUBMITTED_KIND[stage]
        tx_hash = intent.get("tx_hash")
        if not tx_hash:
            intent["stage"] = "UNKNOWN"
            intent["status"] = "UNKNOWN"
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNKNOWN", "token": token}
        verdict, detail = _tx_status(intent, kind)
        if verdict == "UNCERTAIN":
            intent["status"] = "UNKNOWN"
            intent["last_check_error"] = detail
            _update_intent(token, intent)
            return {"action": "SELL", "status": "UNKNOWN", "token": token, "tx": tx_hash}
        if verdict == "PENDING":
            return {"action": "SELL", "status": "PENDING_CONFIRMATION", "token": token, "tx": tx_hash}
        if verdict == "DROPPED":
            return _handle_dropped(token, intent, kind, detail)
        receipt = detail
        ok = int(receipt.get("status", "0x0"), 16) == 1
        if stage == "APPROVAL_SUBMITTED":
            if not ok:
                return _fail(token, intent, "APPROVAL_REVERTED", tx_hash, deterministic=True)
            intent.pop("tx_hash", None)
            intent["approval_tx"] = tx_hash
            intent["stage"] = "APPROVAL_CONFIRMED"
            intent["status"] = "APPROVAL_CONFIRMED"
            _update_intent(token, intent)
        elif stage == "SWAP_SUBMITTED":
            if not ok:
                return _fail(token, intent, "SWAP_REVERTED", tx_hash, deterministic=False)
            if not _mark_swap_mined(token, intent, tx_hash, receipt, trader.wallet_address()):
                return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
        else:
            if not ok:
                intent.pop("tx_hash", None)
                intent["stage"] = "SWAP_CONFIRMED"
                intent["status"] = "UNWRAP_FAILED"
                intent["unwrap_failures"] = int(intent.get("unwrap_failures") or 0) + 1
                _update_intent(token, intent)
                return {"action": "SELL", "status": "UNWRAP_FAILED", "token": token, "tx": tx_hash}
            intent.pop("tx_hash", None)
            intent["unwrap_tx"] = tx_hash
            intent["stage"] = "UNWRAP_CONFIRMED"
            intent["status"] = "CONFIRMED"
            _update_intent(token, intent)
        stage = intent["stage"]

    if stage == "APPROVAL_CONFIRMED":
        owner = trader.wallet_address()
        if int(trader.allowance(token, owner, cfg.ROUTER_ADDRESS)) < int(intent["requested_quantity_raw"]):
            if intent.get("allowance_recheck"):
                return _fail(token, intent, "ALLOWANCE_NOT_CONFIRMED",
                             "allowance still insufficient after confirmed approval", deterministic=True)
            intent["allowance_recheck"] = True
            _update_intent(token, intent)
            return {"action": "SELL", "status": "ALLOWANCE_NOT_CONFIRMED", "token": token}
        intent["stage"] = "READY_TO_SWAP"
        intent["status"] = "READY_TO_SWAP"
        _update_intent(token, intent)
        stage = "READY_TO_SWAP"

    if stage in {"SWAP_CONFIRMED", "UNWRAP_CONFIRMED"}:
        if not intent.get("accounting_applied"):
            if not _ensure_swap_deltas(token, intent):
                intent["accounting_check_failures"] = int(intent.get("accounting_check_failures") or 0) + 1
                if intent["accounting_check_failures"] >= MAX_ACCOUNTING_CHECKS:
                    return _fail(token, intent, "ACCOUNTING_UNVERIFIED",
                                 "confirmed swap transfer logs could not be verified",
                                 deterministic=True, stage="QUARANTINED")
                intent["status"] = "UNKNOWN"
                _update_intent(token, intent)
                return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
            _apply_confirmed_sell(token, intent)
        if stage == "UNWRAP_CONFIRMED":
            return _complete_intent(token, intent)
        return _send_unwrap(token, intent)
    if stage == "READY_TO_SWAP":
        return _submit_swap(token, intent)
    if stage in {"CREATED", "APPROVAL_PENDING"}:
        # No signed hash was persisted, so nothing was broadcast.
        return _fail(token, intent, "PRE_BROADCAST_ABORTED", "intent interrupted before signing")
    if stage == "UNKNOWN":
        return _fail(token, intent, "UNKNOWN_STAGE_REVIEW", "submitted stage without persisted hash",
                     deterministic=True, stage="QUARANTINED")
    return {"action": "SELL", "status": intent.get("status", "UNKNOWN"), "token": token}


def _submit_swap(token, intent):
    account = trader.private_account()
    owner = account.address
    reconciliation = reconcile_position_wallet(token)
    if reconciliation.get("status") == "CLOSED_MANUAL_RECONCILED":
        _cancel_intent(token, intent, "CANCELLED_MANUAL_WALLET_EMPTY")
        return {"action": "SELL", "status": "MANUAL_CLOSED", "token": token}
    state = trader.load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        _cancel_intent(token, intent, "CANCELLED_POSITION_CLOSED")
        return {"action": "SELL", "status": "NO_POSITION", "token": token}
    wallet_qty = int(reconciliation.get("wallet_quantity") or 0)
    tracked_qty = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
    fraction = Decimal("0.20") if intent.get("sell_reason") == "PROFIT_PARTIAL_25" else Decimal("1")
    requested = int((Decimal(tracked_qty) * fraction).to_integral_value(rounding=ROUND_DOWN))
    actual = min(requested, wallet_qty)
    if actual <= 0:
        _cancel_intent(token, intent, "CANCELLED_NO_WALLET_BALANCE")
        return {"action": "SELL", "status": "NO_WALLET_BALANCE", "token": token}
    # Refresh both baselines immediately before the swap. A manual disposal
    # during approval has already been cost-allocated by wallet reconciliation.
    intent["token_balance_before"] = wallet_qty
    intent["wsda_balance_before"] = int(trader.token_balance(cfg.WSDA_ADDRESS, owner))
    _update_intent(token, intent)
    # Fresh executable quote is intentionally obtained only AFTER approval.
    try:
        fee, quoted, _, deadline = trader.quote_best_live_fee(
            token, cfg.WSDA_ADDRESS, token, owner, actual, value_wei=0
        )
        minimum = trader.min_out(quoted)
        if int(quoted) <= 0 or int(minimum) <= 0:
            raise RuntimeError("SELL quote/minOut is zero (no valid route)")
        calldata = trader.build_exact_input_single(
            token, cfg.WSDA_ADDRESS, fee, owner, deadline, actual, minimum
        )
    except Exception as exc:
        detail = f"SELL quote/preflight failed: {type(exc).__name__}: {str(exc)[:180]}"
        return _fail(token, intent, "QUOTE_FAILED", detail, deterministic=classify_failure(detail))
    intent.update({
        "requested_quantity_raw": actual, "fee_tier": fee,
        "quoted_wsda_raw": int(quoted), "minimum_wsda_raw": int(minimum),
        "deadline": int(deadline), "stage": "READY_TO_SWAP", "status": "READY_TO_SWAP",
    })
    _update_intent(token, intent)

    def persist_swap(signed):
        intent.update({"stage": "SWAP_SUBMITTED", "status": "SUBMITTED",
                       "tx_hash": signed["tx_hash"], "swap_tx": signed["tx_hash"],
                       "nonce": signed["nonce"], "gas": signed["gas"],
                       "gas_price": signed["gas_price"], "submitted_at": trader.now()})
        _update_intent(token, intent)
    try:
        tx, _, _ = trader.send_transaction(
            account, cfg.ROUTER_ADDRESS, calldata, 0, cfg.GAS_LIMIT,
            before_broadcast=persist_swap,
        )
    except Exception as exc:
        if intent.get("stage") == "SWAP_SUBMITTED":
            # Hash persisted before broadcast; lifecycle checks resolve or drop it.
            return {"action": "SELL", "status": "UNKNOWN", "token": token,
                    "tx": intent.get("tx_hash")}
        detail = f"swap signing failed: {type(exc).__name__}: {str(exc)[:160]}"
        return _fail(token, intent, "BROADCAST_PREPARATION_FAILED", detail, deterministic=False)
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
        return _fail(token, intent, "SWAP_REVERTED", tx, deterministic=False)
    if not _mark_swap_mined(token, intent, tx, receipt, owner):
        return {"action": "SELL", "status": "ACCOUNTING_UNVERIFIED", "token": token}
    return _resolve_existing(token, intent)


def _new_intent(token, symbol, position, decision, market_price, price_timestamp):
    reason = REASON_MAP[decision["reason"]]
    state = trader.load_state()
    counters = state.setdefault("sell_intent_counters", {})
    counter_key = f"{position.get('position_id') or position.get('buy_tx') or token}:{reason}"
    attempt = int(counters.get(counter_key, 0))
    intent_id = _intent_id(position, reason, attempt)
    existing = state.setdefault("sell_intents", {}).get(intent_id)
    if existing and _stage(existing) in ACTIVE_STAGES:
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
        "symbol": symbol, "sell_reason": reason, "attempt": attempt,
        "requested_quantity_raw": requested,
        "requested_percentage": float(fraction * 100),
        "reference_price": str(market_price),
        "price_timestamp": price_timestamp,
        "timestamp": trader.now(), "created_at": trader.now(),
        "status": "CREATED", "stage": "CREATED",
    }
    state.setdefault("pending_sells", {})[token] = intent
    state.setdefault("sell_intents", {})[intent_id] = intent
    state["updated_at"] = trader.now()
    trader.save_state(state)
    return intent, None


def evaluate_position(token, symbol, market_price, price_timestamp=None, wallet_reconciled=False):
    token = trader.addr(token)
    with _token_lock(token):
        migrated = migrate_if_needed()
        state = trader.load_state()
        existing = state.setdefault("pending_sells", {}).get(token)
        if isinstance(existing, dict):
            return _resolve_existing(token, existing)

        reconciled = ({"status": "UNCHANGED"} if wallet_reconciled
                      else reconcile_position_wallet(token))
        if reconciled["status"] == "CLOSED_MANUAL_RECONCILED":
            return {"action": "SELL", "status": "MANUAL_CLOSED", "token": token}
        if reconciled["status"] == "DUST_RECONCILED":
            return {"action": "HOLD", "status": "DUST_ONLY", "token": token}
        state = trader.load_state()
        position = state.get("positions", {}).get(token)
        if not isinstance(position, dict):
            return {"action": "HOLD", "status": "NO_POSITION", "token": token}
        if position.get("sellManagementEnabled") is False:
            return {"action": "HOLD", "status": "SELL_MANAGEMENT_DISABLED", "token": token}
        if migrated:
            return {"action": "HOLD", "status": "MIGRATION_BASELINE", "token": token}
        price = Decimal(str(market_price))
        if price <= 0:
            return {"action": "HOLD", "status": "PRICE_UNAVAILABLE", "token": token}
        migration_stamp = state.get("sell_migration_observation_started_at")
        if migration_stamp:
            try:
                observed = datetime.fromisoformat(str(price_timestamp).replace("Z", "+00:00"))
                migrated_at = datetime.fromisoformat(str(migration_stamp).replace("Z", "+00:00"))
                if observed.tzinfo is None or migrated_at.tzinfo is None or observed <= migrated_at:
                    return {"action": "HOLD", "status": "PRE_MIGRATION_PRICE", "token": token}
            except Exception:
                return {"action": "HOLD", "status": "PRICE_TIMESTAMP_INVALID", "token": token}
        position = _position_defaults(position)
        decimals = trader.token_decimals(token)
        quantity_human = Decimal(int(position["remaining_quantity"])) / (Decimal(10) ** decimals)
        cost = Decimal(str(position["remaining_cost_basis"]))
        if cfg.is_dust_quantity(position["remaining_quantity"], decimals, cost, price):
            return {"action": "HOLD", "status": "DUST_ONLY", "token": token}
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
        gate = _retry_gate(state, current.get("position_id") or token, REASON_MAP[decision["reason"]])
        if gate:
            return {"action": "HOLD", "token": token, "pnl_pct": float(pnl_fraction * 100),
                    "blocked_decision": decision["reason"], **gate}
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
            _cancel_intent(token, intent, "CANCELLED_NO_WALLET_BALANCE")
            return {"action": "SELL", "status": "NO_WALLET_BALANCE", "token": token}
        intent["requested_quantity_raw"] = actual_qty
        intent["token_balance_before"] = wallet_qty
        intent["wsda_balance_before"] = int(trader.token_balance(cfg.WSDA_ADDRESS, account.address))
        _update_intent(token, intent)
        current_allowance = int(trader.allowance(token, account.address, cfg.ROUTER_ADDRESS))
        if current_allowance < actual_qty:
            intent["stage"] = "APPROVAL_PENDING"
            intent["status"] = "APPROVAL_PENDING"
            _update_intent(token, intent)
            approval_data = cfg.APPROVE_SELECTOR + trader.address_word(cfg.ROUTER_ADDRESS) + trader.word((1 << 256) - 1)

            def persist_approval(signed):
                intent.update({"stage": "APPROVAL_SUBMITTED", "status": "SUBMITTED",
                               "tx_hash": signed["tx_hash"], "approval_tx": signed["tx_hash"],
                               "nonce": signed["nonce"], "gas": signed["gas"],
                               "gas_price": signed["gas_price"], "submitted_at": trader.now()})
                _update_intent(token, intent)
            try:
                tx, _, _ = trader.send_transaction(
                    account, token, approval_data, 0, 100000,
                    before_broadcast=persist_approval,
                )
            except Exception as exc:
                if intent.get("stage") == "APPROVAL_SUBMITTED":
                    return {"action": "SELL", "status": "UNKNOWN", "token": token,
                            "tx": intent.get("tx_hash")}
                detail = f"approval signing failed: {type(exc).__name__}: {str(exc)[:160]}"
                return _fail(token, intent, "BROADCAST_PREPARATION_FAILED", detail)
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
                return _fail(token, intent, "APPROVAL_REVERTED", tx, deterministic=True)
            intent.pop("tx_hash", None)
            intent["stage"] = "APPROVAL_CONFIRMED"
            intent["status"] = "APPROVAL_CONFIRMED"
            _update_intent(token, intent)
            return _resolve_existing(token, intent)
        intent["stage"] = "READY_TO_SWAP"
        intent["status"] = "READY_TO_SWAP"
        _update_intent(token, intent)
        return _submit_swap(token, intent)


def _same_manual_trade(a, b):
    return (isinstance(a, dict) and isinstance(b, dict)
            and a.get("action") == b.get("action") == "MANUAL_SELL"
            and a.get("at") == b.get("at") and trader.addr(a.get("token")) == trader.addr(b.get("token"))
            and int(a.get("sold_quantity_raw") or 0) == int(b.get("sold_quantity_raw") or 0))


def _active_manual_totals(state, token, opened_at):
    quantity = 0
    cost = Decimal(0)
    opened = _parse_time(opened_at)
    for trade in state.get("trades", []):
        if not (isinstance(trade, dict) and trade.get("action") == "MANUAL_SELL"
                and trader.addr(trade.get("token")) == token and not trade.get("superseded_by")):
            continue
        at = _parse_time(trade.get("at"))
        if opened is not None and (at is None or at < opened):
            continue
        quantity += int(trade.get("sold_quantity_raw") or 0)
        cost += Decimal(str(trade.get("cost_basis_sda") or 0))
    return quantity, cost


def repair_superseded_manual_sells():
    """Supersede MANUAL_SELL rows that were really an in-flight automatic SELL.

    Evidence required: the automatic intent was created before and confirmed
    after the manual row, its swap receipt shows the same wallet outflow, and
    the restored bot quantity is still fully present in the wallet.
    """
    state = trader.load_state()
    intents = state.get("sell_intents") or {}
    candidates = []
    for trade in state.get("trades", []):
        if not (isinstance(trade, dict) and trade.get("action") == "MANUAL_SELL"
                and trade.get("source") == "wallet_reconciliation" and not trade.get("superseded_by")):
            continue
        token = trader.addr(trade.get("token"))
        at = _parse_time(trade.get("at"))
        manual_qty = int(trade.get("sold_quantity_raw") or 0)
        for intent in intents.values():
            if not isinstance(intent, dict) or trader.addr(intent.get("token")) != token:
                continue
            if _stage(intent) != "CONFIRMED" and not intent.get("accounting_applied"):
                continue
            created = _parse_time(intent.get("created_at") or intent.get("timestamp"))
            confirmed = _parse_time(intent.get("accounting_applied_at") or intent.get("confirmed_at"))
            sold = int(intent.get("confirmed_sold_quantity_raw") or 0)
            if (at and created and confirmed and intent.get("swap_tx")
                    and created <= at <= confirmed and 0 < manual_qty <= sold):
                candidates.append((dict(trade), dict(intent)))
                break
    if not candidates:
        return []
    owner = trader.wallet_address()
    repairs = []
    for trade, intent in candidates:
        token = trader.addr(trade.get("token"))
        try:
            receipt = trader.rpc("eth_getTransactionReceipt", [intent["swap_tx"]])
            if not receipt or int(receipt.get("status", "0x0"), 16) != 1:
                continue
            chain_sold, _ = _confirmed_transfer_deltas(receipt, token, owner)
            if chain_sold != int(intent.get("confirmed_sold_quantity_raw") or 0):
                continue
            wallet_qty = int(trader.token_balance(token, owner))
        except Exception:
            continue
        state = trader.load_state()
        position = state.get("positions", {}).get(token)
        before = after = None
        if isinstance(position, dict):
            if str(position.get("position_id") or position.get("buy_tx")) != str(intent.get("position_id")):
                continue
            pos = _position_defaults(position)
            manual_qty, manual_cost = _active_manual_totals(state, token, position.get("opened_at"))
            manual_qty -= int(trade.get("sold_quantity_raw") or 0)
            manual_cost -= Decimal(str(trade.get("cost_basis_sda") or 0))
            expected_qty = (int(pos["initial_quantity"]) - int(pos.get("confirmed_sold_quantity") or 0)
                            - manual_qty)
            expected_cost = (Decimal(str(pos["initial_cost_basis"])) - Decimal(str(pos.get("sold_cost_basis") or 0))
                             - manual_cost)
            if expected_qty <= 0 or expected_cost <= 0 or wallet_qty < expected_qty:
                # Wallet does not hold the restored quantity: not provably a phantom.
                continue
            before = {"remaining_quantity": int(pos["remaining_quantity"]),
                      "remaining_cost_basis": str(pos["remaining_cost_basis"])}
            position.update({
                "remaining_quantity": expected_qty, "token_amount_raw": expected_qty,
                "remaining_cost_basis": str(expected_cost), "amount_sda": float(expected_cost),
                "wallet_available_quantity": wallet_qty,
            })
            position.pop("manual_reconciled", None)
            position.pop("manual_reconciled_quantity", None)
            after = {"remaining_quantity": expected_qty, "remaining_cost_basis": str(expected_cost)}
        for row in state.get("trades", []):
            if _same_manual_trade(row, trade):
                row.update({"superseded_by": intent["sell_intent_id"],
                            "superseded_by_tx": intent.get("swap_tx"),
                            "superseded_at": trader.now(),
                            "accounting_status": "SUPERSEDED_BY_AUTOMATIC_SELL"})
                break
        for item in state.get("manual_disposals", []):
            if (isinstance(item, dict) and trader.addr(item.get("token")) == token
                    and item.get("at") == trade.get("at") and not item.get("superseded_by")):
                item.update({"superseded_by": intent["sell_intent_id"],
                             "status": "SUPERSEDED_BY_AUTOMATIC_SELL"})
        record = {"token": token, "symbol": trade.get("symbol"),
                  "sell_intent_id": intent["sell_intent_id"], "swap_tx": intent.get("swap_tx"),
                  "superseded_manual_at": trade.get("at"),
                  "superseded_manual_quantity_raw": int(trade.get("sold_quantity_raw") or 0),
                  "wallet_quantity": wallet_qty, "before": before, "after": after,
                  "at": trader.now()}
        state.setdefault("accounting_repairs", []).append(record)
        state["accounting_refresh_pending"] = True
        state["updated_at"] = trader.now()
        trader.save_state(state)
        _request_accounting_refresh(state)
        repairs.append(record)
    return repairs


def startup_recovery():
    """Inspect all current v2 intents; no historical intent is ever replayed."""
    migrate_if_needed()
    results = []
    for repair in repair_superseded_manual_sells():
        results.append({"action": "ACCOUNTING_REPAIR", "status": "SUPERSEDED_PHANTOM_MANUAL_SELL",
                        **{k: v for k, v in repair.items() if k != "at"}})
    state = trader.load_state()
    _request_accounting_refresh(state)
    for token, intent in list(state.get("pending_sells", {}).items()):
        if isinstance(intent, dict):
            with _token_lock(token):
                results.append(_resolve_existing(token, intent))
    return results
