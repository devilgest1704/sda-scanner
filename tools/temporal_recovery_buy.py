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


def evaluate_continuation(address, current, *, at=None, previous=None):
    """Second PAPER entry lane: two independently observed, *fresh* swap quotes.

    The previous scanner scan must show an already forming IGNITION/CONFIRMATION
    with positive impulse. The NEW scan must strengthen that signal, including
    price follow-through and current buy-side liquidity. Strictly forward-only:
    neither an earlier stale recovery signal nor a single isolated spike can
    authorize this lane. The main PAPER loss circuit, MACD veto and cooldown
    are applied by the caller, never bypassed here.
    """
    answer = {"ready": False, "reason": "no_confirmed_fresh_continuation",
              "source": "research_temporal_recovery",
              "lane": "confirmed_continuation", "prior_quote_at": None}
    if not isinstance(current, dict):
        return {**answer, "reason": "missing_current_candidate"}
    prior = previous if previous is not None else previous_signals().get(str(address).lower())
    if not isinstance(prior, dict):
        return {**answer, "reason": "missing_prior_frozen_signal"}
    now = at if at is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        return {**answer, "reason": "invalid_clock"}
    now = now.astimezone(timezone.utc)
    previous_decision = stamp(prior.get("decision_at"))
    previous_quote = stamp(prior.get("quote_at"))
    current_quote = stamp(current.get("quote_at"))
    prior_age = num(prior.get("data_age_sec"))
    prior_price = num(prior.get("price_sda"))
    prior_score = num(prior.get("score"))
    prior_impulse = num(prior.get("impulse"))
    age = num(current.get("data_age_sec"))
    price = num(current.get("price_sda"))
    fields = ("score", "quality", "impulse", "m1h", "m15",
              "volume_1h", "flow_1h", "flow_15m", "trades_1h", "buy_ratio")
    v = {key: num(current.get(key)) for key in fields}
    if (previous_decision is None or previous_quote is None
            or current_quote is None or prior_age is None
            or prior_price is None or prior_price <= 0
            or prior_score is None or prior_impulse is None
            or age is None or price is None or price <= 0
            or any(value is None for value in v.values())):
        return {**answer, "reason": "incomplete_independent_quotes"}
    if (prior.get("phase") not in ("IGNITION", "CONFIRMATION")
            or prior_score < 62 or prior_impulse < 4
            or not 0 <= prior_age <= 300
            or not 0 <= (previous_decision - previous_quote).total_seconds() <= 300
            or abs((previous_decision - previous_quote).total_seconds() - prior_age) > 30
            or not 0 < (now - previous_decision).total_seconds() <= 480):
        return {**answer, "reason": "prior_continuation_not_confirmed"}
    if (current.get("phase") not in ("IGNITION", "CONFIRMATION")
            or not 0 <= age <= 180
            or not 0 <= (now - current_quote).total_seconds() <= 180
            or abs((now - current_quote).total_seconds() - age) > 30
            or current_quote <= previous_quote
            or current_quote <= previous_decision
            or not 0 < (current_quote - previous_quote).total_seconds() <= 480
            or not prior_price < price <= prior_price * 1.05):
        return {**answer, "reason": "no_new_fresh_price_followthrough"}
    strong = (
        v["score"] >= 70 and v["quality"] >= 62 and v["impulse"] >= 6
        and 1 <= v["m1h"] <= 10 and v["m15"] >= 1.5
        and v["volume_1h"] >= 700 and v["flow_1h"] >= 220
        and v["flow_15m"] >= 60 and v["trades_1h"] >= 8
        and v["buy_ratio"] >= 1.4
    )
    # 8-11 trades are only admissible at materially stronger activity than
    # 12+ trade signals; this is not a blanket relaxation of trade count.
    if v["trades_1h"] < 12:
        strong = (strong and v["volume_1h"] >= 900
                  and v["flow_1h"] >= 300
                  and v["flow_15m"] >= 80 and v["buy_ratio"] >= 1.6)
    if not strong:
        return {**answer, "reason": "continuation_strength_or_flow_insufficient"}
    return {
        "ready": True, "reason": "TWO_FRESH_QUOTES_CONFIRMED",
        "source": "research_temporal_recovery",
        "lane": "confirmed_continuation",
        "prior_quote_at": previous_quote.isoformat(),
        "current_quote_at": current_quote.isoformat(),
        "previous_score": prior_score,
        "previous_impulse": prior_impulse,
        "previous_data_age_sec": prior_age,
        "price_advance_pct": round((price / prior_price - 1) * 100, 4),
    }


def evaluate_paper(address, current, *, at=None, previous=None):
    """Prefer original research recovery; otherwise test strong continuation.

    Fail-closed when neither hypothesis has an independently validated signal.
    This function never changes positions, trade exits or real trading.
    """
    recovery = evaluate(address, current, at=at, previous=previous)
    if recovery["ready"]:
        return {**recovery, "lane": "temporal_recovery"}
    continuation = evaluate_continuation(address, current, at=at, previous=previous)
    if continuation["ready"]:
        return continuation
    return {**recovery, "continuation_reason": continuation["reason"]}


def evaluate_tp4(address, current, *, at=None, previous=None):
    """PAPER TP4 recovery with anti-chase and current flow confirmation.

    Experimental admission guard, not an estimated profit or executable quote.
    Independent research recovery/TP4 journals keep their original rules.
    """
    result = evaluate(address, current, at=at, previous=previous)
    if not result["ready"]:
        return result
    m15 = num(current.get("m15"))
    flow15 = num(current.get("flow_15m"))
    if m15 is None or m15 > 8:
        return {**result, "ready": False, "reason": "recovery_15m_overextended"}
    if flow15 is None or flow15 <= 0:
        return {**result, "ready": False, "reason": "recovery_no_current_buy_flow"}
    return {**result, "lane": "temporal_recovery",
            "paper_filter_revision": "tp4_flow_antichase_v1"}
