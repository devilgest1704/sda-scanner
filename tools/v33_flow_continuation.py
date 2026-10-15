"""V33 forward SHADOW hypothesis: observed buy pressure without late-price chasing.

Uses only market flow buckets available at the SAME scanner decision time.
Hourly turnover is an activity proxy, NOT a DEX pool-reserve or executable
slippage estimate. This module cannot create real/PAPER Champion orders.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

VERSION = "V33_OBSERVED_FLOW_CONTINUATION_1"
ROUNDTRIP_FRICTION = 0.022
RESEARCH_NOTIONAL_SDA = 50.0


def num(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return t.astimezone(timezone.utc) if t.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def observed_flow_signal(candidate, token_analysis, decision_at, market_at):
    """Return a frozen, causal entry hypothesis and explicit gate failures.

    Pressure is observed from prior swaps; there is no candle extrapolation,
    future peak, invented liquidity, order book, or assumed executable fill.
    """
    issues = []
    evidence = {}
    if not isinstance(candidate, dict) or not isinstance(token_analysis, dict):
        issues.append("missing_inputs")
    if (not isinstance(decision_at, datetime) or decision_at.tzinfo is None
            or not isinstance(market_at, datetime) or market_at.tzinfo is None
            or market_at > decision_at
            or (decision_at-market_at).total_seconds() > 180):
        issues.append("market_snapshot_not_aligned")
    last = stamp(token_analysis.get("last_transaction")) if isinstance(token_analysis, dict) else None
    if last is None or not isinstance(decision_at, datetime) or decision_at.tzinfo is None:
        issues.append("missing_transaction_time")
    elif (last > decision_at or not isinstance(market_at, datetime)
          or market_at.tzinfo is None or last > market_at
          or not 0 <= (decision_at-last).total_seconds() <= 180):
        issues.append("stale_or_future_quote")

    flow = token_analysis.get("flow") if isinstance(token_analysis, dict) else None
    f15 = flow.get("15m") if isinstance(flow, dict) else None
    f1h = flow.get("1h") if isinstance(flow, dict) else None
    if not isinstance(f15, dict) or not isinstance(f1h, dict):
        issues.append("missing_observed_swap_buckets")
        f15, f1h = {}, {}
    raw_fields = {
        "recent_volume_sda": f15.get("total_volume"),
        "recent_net_flow_sda": f15.get("net_flow"),
        "recent_buy_volume_sda": f15.get("buy_volume"),
        "recent_sell_volume_sda": f15.get("sell_volume"),
        "recent_transactions": f15.get("transactions"),
        "recent_buys": f15.get("buy_count"),
        "hour_volume_sda": f1h.get("total_volume"),
        "hour_transactions": f1h.get("transactions"),
        "m15_pct": candidate.get("m15") if isinstance(candidate, dict) else None,
        "m1h_pct": candidate.get("m1h") if isinstance(candidate, dict) else None,
        "m4h_pct": candidate.get("m4h") if isinstance(candidate, dict) else None,
    }
    metrics = {key: num(val) for key, val in raw_fields.items()}
    evidence = {key: round(value, 4) if value is not None else None
                for key, value in metrics.items()}
    if any(value is None for value in metrics.values()):
        issues.append("missing_frozen_features")
    else:
        v15 = metrics["recent_volume_sda"]
        hvol = metrics["hour_volume_sda"]
        flow15 = metrics["recent_net_flow_sda"]
        buys = metrics["recent_buys"]
        trades15 = metrics["recent_transactions"]
        buys_sda = metrics["recent_buy_volume_sda"]
        sells_sda = metrics["recent_sell_volume_sda"]
        trades1h = metrics["hour_transactions"]
        m15, m1h, m4h = (metrics["m15_pct"], metrics["m1h_pct"], metrics["m4h_pct"])
        if any(x < 0 for x in (v15, hvol, buys, trades15, buys_sda, sells_sda, trades1h)):
            issues.append("negative_flow_fields")
        if trades15 < 4 or buys < 3 or trades1h < 12:
            issues.append("insufficient_observed_swaps")
        if hvol < 1000 or v15 < 200 or hvol <= 0 or v15/hvol < 0.20:
            issues.append("weak_trade_activity")
        if flow15 < 100 or buys_sda < 1.5*max(sells_sda, 1):
            issues.append("weak_recent_buy_pressure")
        # Require meaningful but non-exhausted momentum. Not a dip-catching
        # rule and never a buy based solely on a spectacular later peak.
        if not 1.5 <= m15 <= 6.5 or not 1.5 <= m1h <= 10:
            issues.append("missing_or_overheated_momentum")
        if not -4 <= m4h <= 25:
            issues.append("late_four_hour_chase")
        evidence["recent_share_of_hour_volume"] = round(v15/hvol, 4) if hvol else None
        evidence["paper_notional_over_hour_volume"] = round(RESEARCH_NOTIONAL_SDA/hvol, 4) if hvol else None

    return {
        "version": VERSION,
        "mode": "RESEARCH_ONLY",
        "available": not any(reason in issues for reason in (
            "missing_inputs", "market_snapshot_not_aligned",
            "missing_transaction_time", "stale_or_future_quote",
            "missing_observed_swap_buckets", "missing_frozen_features",
            "negative_flow_fields",
        )),
        "eligible": not issues,
        "reason": issues[0] if issues else "observed_pressure_confirmed",
        "rejection_reasons": issues,
        "metrics": evidence,
        "model_friction_roundtrip_pct": ROUNDTRIP_FRICTION * 100,
        "execution_parity": False,
        "pool_liquidity_verified": False,
        "can_promote_champion": False,
    }
