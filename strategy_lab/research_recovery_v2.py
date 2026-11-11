"""Research-only recovery-v2 entry evaluator.

No order placement, state writes, champion promotion, or PAPER gate changes.
All inputs must be frozen at the signal timestamp; never use future returns.
"""
from dataclasses import dataclass
from math import isfinite
from typing import Optional


@dataclass(frozen=True)
class RecoveryObservation:
    age_seconds: float
    trades_1h: int
    buy_volume_15m: float
    sell_volume_15m: float
    momentum_15m_pct: float
    previous_momentum_15m_pct: float
    macd_hist_completed_1h: Optional[float]
    previous_macd_hist_completed_1h: Optional[float]
    recovered_from_local_low_pct: float
    liquidity_verified: bool
    quote_valid: bool


@dataclass(frozen=True)
class ResearchDecision:
    ready: bool
    reasons: tuple[str, ...]


def evaluate_recovery_v2(o: RecoveryObservation) -> ResearchDecision:
    """Conservative research hypothesis; not calibrated or validated."""
    values = (o.age_seconds, o.buy_volume_15m, o.sell_volume_15m,
              o.momentum_15m_pct, o.previous_momentum_15m_pct,
              o.recovered_from_local_low_pct)
    if any(not isfinite(v) for v in values):
        return ResearchDecision(False, ("invalid_numeric_input",))
    reasons = []
    if not o.quote_valid or o.age_seconds < 0 or o.age_seconds > 300:
        reasons.append("quote_stale_or_invalid")
    if not o.liquidity_verified:
        reasons.append("liquidity_unverified")
    if o.trades_1h < 12:
        reasons.append("insufficient_trades")
    if o.sell_volume_15m < 0 or o.buy_volume_15m <= 0:
        reasons.append("invalid_flow")
    elif o.buy_volume_15m < 1.5 * max(o.sell_volume_15m, 1e-9):
        reasons.append("weak_buy_pressure")
    if not (0.5 <= o.momentum_15m_pct <= 8.0
            and o.momentum_15m_pct > o.previous_momentum_15m_pct):
        reasons.append("momentum_not_recovering")
    if not (1.0 <= o.recovered_from_local_low_pct <= 10.0):
        reasons.append("recovery_unconfirmed_or_late")
    h, prev = o.macd_hist_completed_1h, o.previous_macd_hist_completed_1h
    if h is None or prev is None or not isfinite(h) or not isfinite(prev):
        reasons.append("macd_unavailable")
    elif not (h > prev and h > 0):
        reasons.append("macd_not_confirmed")
    return ResearchDecision(not reasons, tuple(reasons))
