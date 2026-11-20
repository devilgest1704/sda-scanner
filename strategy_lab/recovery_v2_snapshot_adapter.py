"""Read-only adapter for comparing frozen scanner observations with recovery-v2.

This module only returns diagnostic decisions. It does not submit trades,
modify PAPER gates, mutate state, or make promotion decisions.
"""
from datetime import datetime, timezone, timedelta
from math import isfinite
from strategy_lab.research_recovery_v2 import RecoveryObservation, evaluate_recovery_v2


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        return None


def _number(value):
    try:
        v = float(value)
        return v if isfinite(v) else None
    except (TypeError, ValueError):
        return None


def inspect_snapshot(candidate, token, snapshot_at):
    """Evaluate using data timestamped no later than snapshot_at; fail closed."""
    ts = _time(snapshot_at)
    if ts is None or not isinstance(candidate, dict) or not isinstance(token, dict):
        return {"ready": False, "reasons": ["invalid_snapshot"]}
    analysis = token.get("analysis") or token
    if not isinstance(analysis, dict):
        return {"ready": False, "reasons": ["missing_analysis"]}
    macd = ((analysis.get("technical") or {}).get("1h_completed") or {})
    hist = (macd.get("macd") or {}) if isinstance(macd, dict) else {}
    completed = _time(macd.get("last_completed_at")) if isinstance(macd, dict) else None
    if (not isinstance(macd, dict) or macd.get("available") is not True
            or completed is None or completed > ts or (ts-completed).total_seconds() > 7200):
        return {"ready": False, "reasons": ["macd_not_verified"]}
    current = _number(candidate.get("price_sda"))
    history = token.get("transactions") or []
    points = []
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            continue
        t, p = _time(row.get("timestamp")), _number(row.get("price"))
        if t is not None and t <= ts and p is not None and p > 0:
            points.append((t, p))
    points.sort()
    if current is None or current <= 0 or not points:
        return {"ready": False, "reasons": ["missing_price_history"]}
    def prior(minutes):
        items = [p for t, p in points if t <= ts-timedelta(minutes=minutes)]
        return items[-1] if items else None
    p15, p30 = prior(15), prior(30)
    hour = [p for t, p in points if t >= ts-timedelta(hours=1)]
    flow = (analysis.get("flow") or {}).get("15m") or {}
    if not p15 or not p30 or not hour or not isinstance(flow, dict):
        return {"ready": False, "reasons": ["insufficient_frozen_features"]}
    h, delta = _number(hist.get("histogram_pct")), _number(hist.get("histogram_delta_pct"))
    if h is None or delta is None:
        return {"ready": False, "reasons": ["macd_histogram_unavailable"]}
    # The current market feed does not prove executable pool depth.
    # An explicit verified flag is required; volume alone is insufficient.
    verified = analysis.get("pool_liquidity_verified") is True or token.get("pool_liquidity_verified") is True
    obs = RecoveryObservation(
        age_seconds=(ts-points[-1][0]).total_seconds(),
        trades_1h=int(_number(candidate.get("trades_1h")) or 0),
        buy_volume_15m=_number(flow.get("buy_volume")) or 0,
        sell_volume_15m=_number(flow.get("sell_volume")) or 0,
        momentum_15m_pct=100*(current/p15-1),
        previous_momentum_15m_pct=100*(p15/p30-1),
        macd_hist_completed_1h=h,
        previous_macd_hist_completed_1h=h-delta,
        recovered_from_local_low_pct=100*(current/min(hour)-1),
        liquidity_verified=verified,
        quote_valid=True,
    )
    result = evaluate_recovery_v2(obs)
    return {"ready": result.ready, "reasons": list(result.reasons)}
