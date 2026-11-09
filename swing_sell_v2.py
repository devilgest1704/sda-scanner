"""Pure Swing SELL V2 policy and position accounting.

No RPC, signing, network, or BUY dependencies. Execution adapters must persist
intents and call these helpers only after wallet reconciliation and confirmed
chain receipts.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
from typing import Any

STRATEGY_VERSION = "swing_v2"
PARTIAL_FRACTION = Decimal("0.20")
TRAILING_DISTANCE = Decimal("0.15")
LOSS_EXIT_PNL = Decimal("-0.50")
PARTIAL_TRIGGER_PNL = Decimal("0.25")
HARD_TP_PNL = Decimal("0.50")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def migrate_state(state: dict, migrated_at: str) -> dict:
    """Archive pre-migration SELL work; never execute or replay it."""
    result = deepcopy(state)
    legacy = result.setdefault("legacy_pending_sells", [])
    pending = result.setdefault("pending_sells", {})
    for token, item in list(pending.items()):
        legacy.append({"token": token, "record": deepcopy(item),
                       "archived_at": migrated_at,
                       "reason": "pre_swing_v2_pending_sell_never_replayed"})
    result["pending_sells"] = {}
    result["sell_strategy_version"] = STRATEGY_VERSION
    result["sell_strategy_migrated_at"] = migrated_at
    result["sell_strategy_migration_complete"] = True
    result["sell_migration_historical_transactions"] = 0
    return result


def reconcile_wallet_quantity(position: dict, wallet_quantity: int) -> dict:
    """Reconcile recorded raw quantity to wallet quantity; never sell missing units."""
    result = deepcopy(position)
    wallet_quantity = max(0, int(wallet_quantity))
    recorded = int(result.get("remaining_quantity", result.get("token_amount_raw", 0)) or 0)
    if wallet_quantity == 0:
        result.update({"remaining_quantity": 0, "token_amount_raw": 0,
                       "status": "CLOSED_MANUAL_RECONCILED",
                       "manual_reconciled": True, "manual_reconciled_quantity": recorded})
        return result
    if wallet_quantity < recorded:
        ratio = _d(wallet_quantity) / _d(recorded) if recorded else Decimal(0)
        old_cost = _d(result.get("remaining_cost_basis", result.get("amount_sda", 0)) or 0)
        result.update({"remaining_quantity": wallet_quantity, "token_amount_raw": wallet_quantity,
                       "remaining_cost_basis": str(old_cost * ratio),
                       "amount_sda": float(old_cost * ratio), "manual_reconciled": True,
                       "manual_reconciled_quantity": recorded - wallet_quantity})
    else:
        result.update({"remaining_quantity": wallet_quantity, "token_amount_raw": wallet_quantity})
    return result


def decide(position: dict, pnl_fraction: Any, current_price: Any) -> dict:
    """Return HOLD, PARTIAL_SELL (20%), or FULL_SELL for one fresh observation."""
    pnl, price = _d(pnl_fraction), _d(current_price)
    if price <= 0:
        return {"action": "HOLD", "reason": "PRICE_UNAVAILABLE"}
    if position.get("sell_intent_id") and position.get("sell_intent_status") not in {
        "CONFIRMED", "FAILED_FINAL", "CANCELLED"
    }:
        return {"action": "HOLD", "reason": "SELL_INTENT_UNRESOLVED"}
    if pnl <= LOSS_EXIT_PNL:
        return {"action": "FULL_SELL", "reason": "LOSS_LIMIT"}
    if pnl >= HARD_TP_PNL:
        return {"action": "FULL_SELL", "reason": "HARD_TP"}
    if pnl >= PARTIAL_TRIGGER_PNL and not position.get("profit_partial_done"):
        return {"action": "PARTIAL_SELL", "fraction": str(PARTIAL_FRACTION),
                "reason": "FIRST_PROFIT_PARTIAL"}
    if position.get("trailing_armed"):
        peak = max(_d(position.get("highest_price_since_trailing_activation") or price), price)
        trailing = peak * (Decimal(1) - TRAILING_DISTANCE)
        if price <= trailing:
            return {"action": "FULL_SELL", "reason": "TRAILING_STOP",
                    "peak_price": str(peak), "trailing_price": str(trailing)}
        return {"action": "HOLD", "reason": "TRAILING_ACTIVE",
                "peak_price": str(peak), "trailing_price": str(trailing)}
    return {"action": "HOLD", "reason": "NO_THRESHOLD"}


def apply_confirmed_partial(position: dict, sold_quantity: int,
                            proceeds_sda: Any, confirmed_at: str) -> dict:
    """Allocate cost basis proportionally using actual confirmed sold quantity."""
    result = deepcopy(position)
    initial_qty = int(result.get("initial_quantity", result.get("token_amount_raw", 0)) or 0)
    remaining_qty = int(result.get("remaining_quantity", result.get("token_amount_raw", 0)) or 0)
    sold_quantity = int(sold_quantity)
    if sold_quantity <= 0 or sold_quantity > remaining_qty:
        raise ValueError("confirmed sold quantity must be within remaining quantity")
    remaining_cost = _d(result.get("remaining_cost_basis",
                                   result.get("initial_cost_basis", result.get("amount_sda", 0))) or 0)
    allocated_cost = remaining_cost * _d(sold_quantity) / _d(remaining_qty)
    proceeds = _d(proceeds_sda)
    result.update({
        "initial_quantity": initial_qty,
        "remaining_quantity": remaining_qty - sold_quantity,
        "confirmed_sold_quantity": int(result.get("confirmed_sold_quantity", 0) or 0) + sold_quantity,
        "initial_cost_basis": str(result.get("initial_cost_basis", result.get("amount_sda", 0))),
        "remaining_cost_basis": str(remaining_cost - allocated_cost),
        "sold_cost_basis": str(_d(result.get("sold_cost_basis", 0) or 0) + allocated_cost),
        "realized_proceeds": str(_d(result.get("realized_proceeds", 0) or 0) + proceeds),
        "realized_pnl": str(_d(result.get("realized_pnl", 0) or 0) + proceeds - allocated_cost),
        "profit_partial_done": True, "trailing_armed": True,
        "highest_price_since_trailing_activation": str(
            result.get("highest_price_since_trailing_activation") or result.get("last_market_price") or ""),
        "last_confirmed_sell_at": confirmed_at,
        "status": "OPEN" if remaining_qty > sold_quantity else "CLOSED",
    })
    result["token_amount_raw"] = result["remaining_quantity"]
    peak = result["highest_price_since_trailing_activation"]
    result["current_trailing_price"] = str(_d(peak) * Decimal("0.85")) if peak and result["status"] == "OPEN" else None
    return result


def update_trailing_peak(position: dict, current_price: Any) -> dict:
    result = deepcopy(position)
    if not result.get("trailing_armed"):
        return result
    price = _d(current_price)
    peak = max(_d(result.get("highest_price_since_trailing_activation") or price), price)
    result["highest_price_since_trailing_activation"] = str(peak)
    result["current_trailing_price"] = str(peak * Decimal("0.85"))
    return result
