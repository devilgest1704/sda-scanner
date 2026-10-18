"""Temporal-recovery eligibility for the ACTUAL PAPER BUY path.

Read the previous scanner's immutable forward-observed signal journal. Never
open on a stale signal alone: require a *newer* observed swap quote, fresh
current data, and current buy-side pressure. Real-trading paths are untouched.
Only new PAPER entries may be affected; exit management stays in V30.
"""
from __future__ import annotations
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import json
import math

SOURCE = Path("strategy_lab/forward_shadow_state.json")


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def num(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return v if math.isfinite(v) else None


@lru_cache(maxsize=1)
def previous_signals():
    """Read only once per worker PROCESS; scanner launches a new process/scan.

    Missing/corrupt/out-of-date evidence fails CLOSED for BUY. Never synthesize
    or carry over legacy signals from an unverified source.
    """
    try:
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("paper_only") is not True
                or data.get("method") != "forward_observed_quote_shadow_simulation"):
            return {}
        store = data.get("timing_research")
        rows = store.get("last_signals") if isinstance(store, dict) else None
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def evaluate(address, current, *, at=None, previous=None):
    """Pure, fail-closed match of research_temporal_recovery admission rules.

    current: phase, score, quality, impulse, quote_at, data_age_sec,
             price_sda, m1h, m15, volume_1h, flow_1h, trades_1h, buy_ratio
    previous: previously observed *stale* yet strong quote/signal, from the
              persisted independent forward Shadow sampler.
    """
    answer = {"ready": False, "reason": "missing_prior_frozen_signal",
              "source": "research_temporal_recovery", "prior_quote_at": None}
    if not isinstance(current, dict):
        return {**answer, "reason": "missing_current_candidate"}
    prior = previous if previous is not None else previous_signals().get(str(address).lower())
    if not isinstance(prior, dict):
        return answer
    now = at if at is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        return {**answer, "reason": "invalid_clock"}
    now = now.astimezone(timezone.utc)
    previous_decision = stamp(prior.get("decision_at"))
    previous_quote = stamp(prior.get("quote_at"))
    current_quote = stamp(current.get("quote_at"))
    previous_age = num(prior.get("data_age_sec"))
    previous_score = num(prior.get("score"))
    previous_impulse = num(prior.get("impulse"))
    age = num(current.get("data_age_sec"))
    score = num(current.get("score"))
    quality = num(current.get("quality"))
    impulse = num(current.get("impulse"))
    price = num(current.get("price_sda"))
    m1h = num(current.get("m1h"))
    m15 = num(current.get("m15"))
    volume = num(current.get("volume_1h"))
    flow = num(current.get("flow_1h"))
    trades = num(current.get("trades_1h"))
    ratio = num(current.get("buy_ratio"))
    phase = current.get("phase")
    if (previous_decision is None or previous_quote is None
            or current_quote is None or previous_age is None
            or previous_score is None or previous_impulse is None):
        return {**answer, "reason": "incomplete_prior_signal"}
    if (prior.get("phase") not in ("IGNITION", "CONFIRMATION")
            or previous_score < 68 or previous_impulse < 8
            or not (300 < previous_age <= 900)
            or not (0 < (now - previous_decision).total_seconds() <= 600)
            or previous_quote > previous_decision):
        return {**answer, "reason": "prior_signal_not_recoverable"}
    if (age is None or not 0 <= age <= 300 or current_quote <= previous_quote
            or current_quote > now or price is None or price <= 0
            or abs((now - current_quote).total_seconds() - age) > 30):
        return {**answer, "reason": "no_new_fresh_observed_quote"}
    if phase not in ("IGNITION", "CONFIRMATION"):
        return {**answer, "reason": "no_current_ignition_confirmation"}
    if any(v is None for v in
           (score, quality, impulse, m1h, m15, volume, flow, trades, ratio)):
        return {**answer, "reason": "missing_current_metrics"}
    if not (score >= 64 and quality >= 60 and impulse >= 4
            and 0 <= m1h <= 12
            and m15 >= (1.5 if phase == "CONFIRMATION" else 0.8)
            and volume >= 500 and flow >= 150 and trades >= 12 and ratio >= 1.2):
        return {**answer, "reason": "current_strength_or_liquidity_insufficient"}
    return {
        "ready": True, "reason": "NEW_FRESH_QUOTE_CONFIRMED",
        "source": "research_temporal_recovery",
        "prior_quote_at": previous_quote.isoformat(),
        "current_quote_at": current_quote.isoformat(),
        "previous_score": previous_score,
        "previous_impulse": previous_impulse,
        "previous_data_age_sec": previous_age,
    }


def evaluate_fresh_confirmation(current, *, at=None):
    """Guarded PAPER-only second entry lane when temporal recovery is absent.

    This is NOT a profit prediction. It admits only a newly observed, actively
    traded quote showing early momentum, flow and buy-side confirmation.
    The caller must still apply cooldown, completed-hour MACD veto, position
    limits, legacy exits and the 24-hour realized-loss circuit.
    """
    base = {"ready": False, "reason": "missing_current_metrics",
            "source": "fresh_momentum_confirmation"}
    if not isinstance(current, dict):
        return base
    now = at if at is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        return {**base, "reason": "invalid_clock"}
    now = now.astimezone(timezone.utc)
    quote = stamp(current.get("quote_at"))
    age = num(current.get("data_age_sec"))
    price = num(current.get("price_sda"))
    fields = ("score", "quality", "impulse", "delta_impulse", "m1h", "m15",
              "m4h", "volume_1h", "flow_1h", "flow_15m", "trades_1h",
              "buy_ratio")
    m = {k: num(current.get(k)) for k in fields}
    if any(v is None for v in m.values()) or price is None or price <= 0:
        return base
    if (quote is None or quote > now or age is None or not 0 <= age <= 180
            or abs((now-quote).total_seconds()-age) > 30):
        return {**base, "reason": "no_fresh_observed_swap"}
    phase = current.get("phase")
    if phase not in ("IGNITION", "CONFIRMATION"):
        return {**base, "reason": "requires_ignition_or_confirmation"}
    if (current.get("fresh_transition") is not True
            or m["delta_impulse"] < 4):
        return {**base, "reason": "no_new_momentum_transition"}
    if not (0 < m["m1h"] <= 10 and 1.5 <= m["m15"] <= 8
            and m["m4h"] >= -0.25 and
            (phase != "CONFIRMATION" or m["m15"] >= 2)):
        return {**base, "reason": "momentum_outside_early_entry_window"}
    if (m["score"] < 76 or m["quality"] < 63 or m["impulse"] < 8):
        return {**base, "reason": "momentum_quality_not_confirmed"}
    if m["trades_1h"] >= 12:
        activity = (m["volume_1h"] >= 650 and m["flow_1h"] >= 180
                    and m["flow_15m"] >= 60 and m["buy_ratio"] >= 1.35)
    elif m["trades_1h"] >= 10:
        activity = (m["volume_1h"] >= 700 and m["flow_1h"] >= 210
                    and m["flow_15m"] >= 70 and m["buy_ratio"] >= 1.45)
    elif m["trades_1h"] >= 8:
        activity = (m["score"] >= 84 and m["quality"] >= 72
                    and m["delta_impulse"] >= 8 and
                    m["volume_1h"] >= 900 and m["flow_1h"] >= 300
                    and m["flow_15m"] >= 100 and m["buy_ratio"] >= 1.65)
    else:
        activity = False
    if not activity or m["flow_1h"] / max(m["volume_1h"], 1) < .22:
        return {**base, "reason": "insufficient_real_buy_flow_or_liquidity"}
    return {"ready": True, "reason": "FRESH_MOMENTUM_CONFIRMED",
            "source": "fresh_momentum_confirmation",
            "quote_at": quote.isoformat(), "data_age_sec": age,
            "trades_1h": m["trades_1h"], "observed_score": m["score"]}
