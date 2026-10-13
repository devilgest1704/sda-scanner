"""V32 Trend Runner: research-only partial profit and trailing remainder.

A counterfactual EXIT policy applied only to the forward Shadow entry stream.
One opening notional (50 SDA) produces at most ONE completed trade result.
A partial realized leg is held in the open position until the remainder closes.
A future observed quote is mandatory; no stop-limit prices are invented.
This module is never used by the V30 execution engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

FIRST_TAKE_GROSS = 0.08
PARTIAL_FRACTION = 0.50
INITIAL_HARD_STOP = -0.025
INITIAL_TRAIL = -0.035
RUNNER_TRAIL = 0.035
RUNNER_LOCK_GROSS = 0.002
PRE_TAKE_MAX_HOURS = 4.0
AFTER_TAKE_MAX_HOURS = 12.0
AFTER_TAKE_NO_QUOTE_HOURS = 16.0
MAX_CREDIBLE_QUOTE_GAP_MIN = 30.0
MAX_CREDIBLE_HOLDING_OVERRUN_HOURS = 0.5


def net_exit(entry, exit_price, notional, friction):
    """One partial notional with entry+exit fees/slippage charged once."""
    if not all(math.isfinite(v) and v > 0 for v in
               (entry, exit_price, notional, friction)):
        raise ValueError("Invalid runner quote or paper notional")
    return notional * (
        (exit_price * (1 - friction))/(entry * (1 + friction)) - 1
    )


def apply_observed_quote(position, observed_at, price, gap_minutes,
                         notional=50.0, friction=0.011):
    """Update an in-memory position from exactly one NEW observed quote.

    Returns ('hold'/'partial'/'close', reason). Caller closes at the observed
    quote, not at a hypothetical target or stop level. Time is UTC-aware.
    """
    opened = position.get("opened_at")
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ValueError("Must use a timestamped observed quote")
    start = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
    if start.tzinfo is None or observed_at <= start:
        raise ValueError("Must observe a quote after opening")
    entry = float(position["entry_price_sda"])
    if not all(math.isfinite(v) and v > 0 for v in (entry, price, notional)):
        raise ValueError("Bad observed price")
    if not math.isfinite(gap_minutes) or gap_minutes < 0:
        raise ValueError("Bad quote gap")

    position["runner_largest_quote_gap_min"] = max(
        float(position.get("runner_largest_quote_gap_min", 0)), gap_minutes
    )
    peak = max(float(position.get("peak_price_sda", entry)), price)
    position["peak_price_sda"] = peak
    duration = (observed_at - start).total_seconds() / 3600
    gross = price / entry - 1
    partials = position.setdefault("runner_legs", [])
    if not isinstance(partials, list):
        raise ValueError("Corrupt runner fill legs")
    # Existing snapshot may contain one partial only; no duplicate executions.
    if len(partials) > 1:
        raise ValueError("Runner has more than one partial leg")
    taken = bool(partials)
    if not taken:
        # Same hard risk and prior trailing as baseline, but waiting for a
        # future price sample is required to simulate an execution.
        if gross <= INITIAL_HARD_STOP:
            return "close", "STOP"
        if gross >= FIRST_TAKE_GROSS:
            leg = {
                "at": observed_at.astimezone(timezone.utc).isoformat(),
                "price_sda": price,
                "fraction": PARTIAL_FRACTION,
                "net_sda": round(net_exit(entry, price,
                                         notional * PARTIAL_FRACTION,
                                         friction), 6),
                "reason": "PARTIAL_TAKE",
                "quote_gap_minutes": round(gap_minutes, 3),
            }
            partials.append(leg)
            position["runner_first_take_at"] = leg["at"]
            return "partial", "PARTIAL_TAKE"
        if duration >= .25 and (price/peak - 1) <= INITIAL_TRAIL:
            return "close", "TRAIL"
        if duration >= PRE_TAKE_MAX_HOURS:
            return "close", "TIME"
        return "hold", None

    # After the first partial, protect the remainder using the highest
    # *observed* quote. There is no sell at the candle wick or at the stop.
    if gross <= INITIAL_HARD_STOP:
        return "close", "RUNNER_GAP_STOP"
    stop = max(entry * (1 + RUNNER_LOCK_GROSS),
               peak * (1 - RUNNER_TRAIL))
    position["runner_protective_stop_sda"] = round(stop, 12)
    if price <= stop:
        return "close", "RUNNER_TRAIL"
    if duration >= AFTER_TAKE_MAX_HOURS:
        return "close", "RUNNER_TIME"
    return "hold", None


def close_record(position, stamp, final_price, reason, quote_gap_minutes,
                 notional=50.0, friction=0.011):
    """Aggregate multiple legs into ONE closed outcome and one net SDA P/L."""
    entry = float(position["entry_price_sda"])
    legs = list(position.get("runner_legs") or [])
    if len(legs) > 1:
        raise ValueError("Unexpected multiple partial fills")
    realized = sum(float(x["net_sda"]) for x in legs)
    remaining = 1 - sum(float(x["fraction"]) for x in legs)
    if remaining <= 0 or remaining > 1:
        raise ValueError("Corrupt remaining runner position fraction")
    final_net = net_exit(entry, final_price, notional * remaining, friction)
    total = round(realized + final_net, 6)
    completed = legs + [{
        "at": stamp.astimezone(timezone.utc).isoformat(),
        "price_sda": final_price,
        "fraction": round(remaining, 6),
        "net_sda": round(final_net, 6),
        "reason": reason,
        "quote_gap_minutes": round(quote_gap_minutes, 3),
    }]
    gap = float(position.get("runner_largest_quote_gap_min",
                             quote_gap_minutes))
    started = datetime.fromisoformat(
        str(position["opened_at"]).replace("Z", "+00:00")
    )
    hours = (stamp-started).total_seconds()/3600
    max_hours = (AFTER_TAKE_MAX_HOURS if legs
                 else PRE_TAKE_MAX_HOURS)
    excluded = []
    if gap > MAX_CREDIBLE_QUOTE_GAP_MIN:
        excluded.append("runner_observation_gap_over_30m")
    if hours > max_hours + MAX_CREDIBLE_HOLDING_OVERRUN_HOURS:
        excluded.append("runner_holding_time_overrun")
    if not (0.2 <= final_price/entry <= 4.0):
        excluded.append("implausible_quote_jump")
    return {
        "entry_price_sda": entry,
        "exit_price_sda": final_price,
        "pnl_sda": total,
        "roi_pct": round(total/notional * 100, 5),
        "reason": reason,
        "quote_gap_minutes": round(quote_gap_minutes, 3),
        "largest_quote_gap_minutes": round(gap, 3),
        "valid_for_learning": not excluded,
        "learning_exclusion_reasons": excluded,
        "notional_sda": notional,
        "runner_first_take": bool(legs),
        "runner_leg_count": len(completed),
        "runner_legs": completed,
        "model": "observed_quote_partial_take_runner_not_executable_fill",
    }


def stale_writeoff(position, now, notional=50.0):
    """Conservative *non-executable* accounting for unquoted remainder.

    Never count it as a validated exit. Prior partial proceeds remain booked.
    """
    legs = list(position.get("runner_legs") or [])
    remaining = 1-sum(float(x["fraction"]) for x in legs)
    if not (0 < remaining <= 1):
        raise ValueError("Corrupt stale position")
    pnl = round(sum(float(x["net_sda"]) for x in legs) -
                remaining * notional, 6)
    return {
        "exit_price_sda": None, "pnl_sda": pnl,
        "roi_pct": round(pnl/notional * 100, 5),
        "reason": "STALE_TIMEOUT", "valid_for_learning": False,
        "learning_exclusion_reasons": ["stale_timeout_no_observed_exit_quote"],
        "notional_sda": notional,
        "runner_first_take": bool(legs),
        "runner_leg_count": len(legs),
        "runner_legs": legs,
        "model": "risk_writeoff_unquoted_remaining_not_executable_fill",
    }
