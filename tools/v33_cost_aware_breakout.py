"""V33 independent breakout research with pessimistic, explicit cost arithmetic.

Input is the joined same-scan V32 transaction breakout observation, never
a price target or hindsight high. Turnover is NOT order-book liquidity.
All outputs are research-only and cannot authorize PAPER/real orders.
"""
from __future__ import annotations
import math

VERSION = "V33_COST_AWARE_BREAKOUT_1"
NOTIONAL_SDA = 50.0
SIDE_FRICTION = 0.015  # 1.1% baseline plus 0.4% stress on EACH side
TARGET_GROSS = 0.10
STOP_GROSS = -0.025
TRAIL_GROSS = 0.04
MAX_HOURS = 3


def number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def modeled_net_sda(gross_move):
    """Friction at BOTH ends, including the opening quote."""
    return NOTIONAL_SDA * (((1 + gross_move) * (1 - SIDE_FRICTION)) /
                           (1 + SIDE_FRICTION) - 1)


def cost_aware_breakout_signal(candidate, breakout, policy):
    """Snapshot-only admissibility test; no estimate of expected win probability."""
    from tools.v32_breakout import breakout_research_eligible

    reasons = []
    raw_keys = ("breakout_pct", "recent_15m_volume_sda",
                "recent_15m_net_flow_sda", "recent_15m_swaps",
                "volume_multiple", "last_vs_recent_peak_pct",
                "base_range_pct")
    values = {key: number(breakout.get(key)) if isinstance(breakout, dict)
              else None for key in raw_keys}
    if not isinstance(breakout, dict) or breakout.get("mode") != "RESEARCH_ONLY":
        reasons.append("missing_same_scan_breakout")
    elif not breakout_research_eligible(candidate, policy):
        reasons.append("original_breakout_entry_not_eligible")
    if any(v is None for v in values.values()):
        reasons.append("missing_observed_breakout_features")
    else:
        if not 0.5 <= values["breakout_pct"] <= 3.5:
            reasons.append("too_late_or_no_early_breakout")
        if (values["recent_15m_volume_sda"] < 400
                or NOTIONAL_SDA / max(values["recent_15m_volume_sda"], 1) > 0.125):
            reasons.append("insufficient_observed_turnover")
        if values["recent_15m_net_flow_sda"] < 250:
            reasons.append("weak_buy_pressure")
        if values["recent_15m_swaps"] < 6 or values["volume_multiple"] < 2:
            reasons.append("weak_activity_acceleration")
        if values["last_vs_recent_peak_pct"] < 98:
            reasons.append("already_reversing")
        if values["base_range_pct"] > 7:
            reasons.append("wide_consolidation")
    gain = modeled_net_sda(TARGET_GROSS)
    loss = modeled_net_sda(STOP_GROSS)
    breakeven = -loss / (gain - loss) if gain > 0 > loss else 1.0
    # This is the payout-model break-even win rate, NOT a prediction that
    # the strategy can attain that rate. Measured only by future SHADOW closes.
    if gain <= 0 or loss >= 0 or breakeven > 0.45:
        reasons.append("unattractive_after_friction_payoff")
    return {
        "version": VERSION, "mode": "RESEARCH_ONLY", "eligible": not reasons,
        "reason": reasons[0] if reasons else "early_breakout_after_cost_gate",
        "rejection_reasons": reasons,
        "observed_features": values,
        "notional_sda": NOTIONAL_SDA,
        "friction_per_side_pct": SIDE_FRICTION * 100,
        "target_gross_pct": TARGET_GROSS * 100,
        "stop_gross_pct": STOP_GROSS * 100,
        "modeled_net_take_sda": round(gain, 5),
        "modeled_net_stop_sda": round(loss, 5),
        "break_even_win_rate_pct": round(breakeven * 100, 3),
        "turnover_not_liquidity": True,
        "expected_return_estimated": False,
        "forward_executable_fills_verified": False,
        "can_promote_champion": False,
    }
