"""PAPER adapter for the shared Swing SELL V2 policy.

Decisions come only from swing_sell_v2.decide (the same pure function REAL
uses). Execution is simulated: PAPER never imports the REAL executor or trader.
"""
from __future__ import annotations

from decimal import Decimal

import swing_sell_v2 as policy

PROFILE = "swing_v2"
RULES = {"policy": policy.STRATEGY_VERSION, "emergency_pnl": str(policy.LOSS_EXIT_PNL),
         "partial_trigger_pnl": str(policy.PARTIAL_TRIGGER_PNL), "partial_fraction": str(policy.PARTIAL_FRACTION),
         "trailing_distance": str(policy.TRAILING_DISTANCE), "hard_tp_pnl": str(policy.HARD_TP_PNL),
         "time_exit": None}
REASONS = {"LOSS_LIMIT": "SWING V2 STOP -50", "HARD_TP": "SWING V2 TP +50",
           "TRAILING_STOP": "SWING V2 TRAIL", "FIRST_PROFIT_PARTIAL": "SWING V2 PARTIAL +25"}


def _d(value, default="0"):
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


def ensure_state(pos, fee_rate):
    """Initialise modeled PAPER quantity/cost-basis fields once (idempotent)."""
    entry = _d(pos.get("entry_price"))
    investment = _d(pos.get("investment_sda"))
    if entry <= 0 or investment <= 0:
        return pos
    fee = _d(fee_rate)
    remaining_fraction = _d(pos.get("remaining_fraction", 1))
    if pos.get("initial_quantity") is None:
        pos["initial_quantity"] = str(investment / entry)
        pos["initial_cost_basis"] = str(investment * (1 + fee))
        pos["remaining_quantity"] = str(investment / entry * remaining_fraction)
        pos["remaining_cost_basis"] = str(investment * (1 + fee) * remaining_fraction)
        pos.setdefault("sold_quantity", "0")
        pos.setdefault("sold_cost_basis", "0")
        pos.setdefault("realized_proceeds", "0")
        pos.setdefault("realized_pnl", "0")
        pos.setdefault("profit_partial_done", False)
        pos.setdefault("trailing_armed", False)
    return pos


def pnl_fraction(pos, price):
    qty = _d(pos.get("remaining_quantity"))
    cost = _d(pos.get("remaining_cost_basis"))
    if qty <= 0 or cost <= 0:
        return None
    return _d(price) / (cost / qty) - 1


def evaluate(pos, price, fee_rate):
    """Return (decision, pnl_fraction) for one fresh PAPER observation."""
    ensure_state(pos, fee_rate)
    pnl = pnl_fraction(pos, price)
    if pnl is None:
        return {"action": "HOLD", "reason": "INVALID_POSITION_BASIS"}, None
    pos["last_market_price"] = str(price)
    if pos.get("trailing_armed"):
        pos.update(policy.update_trailing_peak(pos, price))
    return policy.decide(pos, pnl, price), pnl


def apply_partial(pos, closed_row, price, sold_fraction_of_remaining):
    """Book a simulated partial: remaining runner keeps its proportional cost basis."""
    qty = _d(pos.get("remaining_quantity"))
    cost = _d(pos.get("remaining_cost_basis"))
    share = _d(sold_fraction_of_remaining)
    sold_qty = qty * share
    allocated = cost * share
    proceeds = _d((closed_row or {}).get("closed_value_sda"))
    pos.update({
        "remaining_quantity": str(qty - sold_qty),
        "remaining_cost_basis": str(cost - allocated),
        "sold_quantity": str(_d(pos.get("sold_quantity")) + sold_qty),
        "sold_cost_basis": str(_d(pos.get("sold_cost_basis")) + allocated),
        "realized_proceeds": str(_d(pos.get("realized_proceeds")) + proceeds),
        "realized_pnl": str(_d(pos.get("realized_pnl")) + proceeds - allocated),
        "profit_partial_done": True, "trailing_armed": True,
        "highest_price_since_trailing_activation": str(price),
        "current_trailing_price": str(_d(price) * (1 - policy.TRAILING_DISTANCE)),
    })
    return pos


def partial_close_fraction(pos):
    """Fraction of the ORIGINAL position (engine_legacy.close units) for a 20% partial."""
    return float(_d(pos.get("remaining_fraction", 1)) * policy.PARTIAL_FRACTION)
