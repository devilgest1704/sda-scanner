"""One forward-only temporal-recovery BUY signal shared by PAPER and SHADOW.

A previously strong but stale sample is a *hypothesis*, never a fill.
A new transaction quote and current flow/liquidity must independently
confirm the entry. Nothing here can place a real trade or bypass risk.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import json

SOURCE = Path("strategy_lab/forward_shadow_state.json")


def when(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def positive(value):
    amount = finite(value)
    return amount if amount is not None and amount > 0 else None


def temporal_recovery_match(prior, current, at_time):
    """Evaluate the exact independent-entry hypothesis, without hindsight.

    Returns False for missing history, old/current duplicate transactions,
    untrusted timestamps or inadequate *current* liquidity/activity.
    """
    if (not isinstance(prior, dict) or not isinstance(current, dict)
            or not isinstance(at_time, datetime) or at_time.tzinfo is None):
        return False
    at_time = at_time.astimezone(timezone.utc)
    prior_at = when(prior.get("decision_at"))
    prior_quote = when(prior.get("quote_at"))
    prior_age = finite(prior.get("data_age_sec"))
    prior_score = finite(prior.get("score"))
    prior_impulse = finite(prior.get("impulse"))
    now_at = when(current.get("at"))
    quote = when(current.get("quote_at"))
    current_age = finite(current.get("data_age_sec"))
    values = {k: finite(current.get(k)) for k in (
        "score", "quality", "impulse", "m1h", "m15", "volume_1h",
        "flow_1h", "trades_1h", "buy_ratio"
    )}
    if (prior_at is None or prior_quote is None or now_at is None
            or quote is None or prior_age is None or prior_score is None
            or prior_impulse is None or current_age is None
            or positive(current.get("price_sda")) is None
            or any(v is None for v in values.values())):
        return False
    phase = current.get("phase")
    if prior.get("phase") not in ("IGNITION", "CONFIRMATION"):
        return False
    if (prior_score < 68 or prior_impulse < 8 or not 300 < prior_age <= 900
            or prior_quote > prior_at or
            abs((prior_at-prior_quote).total_seconds()-prior_age) > 30):
        return False
    if (not 0 <= (at_time-now_at).total_seconds() <= 600
            or not 0 < (now_at-prior_at).total_seconds() <= 600
            or quote <= prior_quote or quote > now_at
            or current_age > 300 or current_age < 0
            or abs((now_at-quote).total_seconds()-current_age) > 30):
        return False
    if phase not in ("IGNITION", "CONFIRMATION"):
        return False
    v = values
    return (
        v["score"] >= 64 and v["quality"] >= 60 and v["impulse"] >= 4
        and 0 <= v["m1h"] <= 12
        and v["m15"] >= (1.5 if phase == "CONFIRMATION" else .8)
        and v["volume_1h"] >= 500 and v["flow_1h"] >= 150
        and v["trades_1h"] >= 12 and v["buy_ratio"] >= 1.2
    )


@lru_cache(maxsize=1)
def previous_shadow_signals():
    """Read the *previous* scanner cycle only; fail closed if untrusted."""
    try:
        state = json.loads(SOURCE.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("paper_only") is not True
                or state.get("method") != "forward_observed_quote_shadow_simulation"):
            return {}
        last_at = when(state.get("last_snapshot_at"))
        if last_at is None:
            return {}
        last = (state.get("timing_research") or {}).get("last_signals")
        return dict(last) if isinstance(last, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def paper_recovery_ready(address, candidate, at_time=None):
    """Validate the prior observed SHADOW signal against a fresh PAPER quote."""
    at_time = at_time or datetime.now(timezone.utc)
    if not isinstance(address, str):
        return False
    prior = previous_shadow_signals().get(address.lower())
    return temporal_recovery_match(prior, candidate, at_time)
