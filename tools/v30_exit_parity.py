#!/usr/bin/env python3
"""Independent, read-only replay audit of recorded V30 paper EXIT decisions.

Uses only a closed trade's own timestamped, *previously observed* price
samples and signal snapshots. It does not fetch market history, infer ticks,
manufacture fills, or modify BUY/SELL/Champion code. Missing, delayed or
unverifiable evidence is EXCLUDED rather than called a match. A matching exit
does not prove V30-vs-Shadow entry or portfolio execution parity.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone

MAX_QUOTE_AGE_SECONDS = 900
MAX_SAMPLE_GAP_SECONDS = 1200
MAX_FIRST_SAMPLE_DELAY_SECONDS = 900
MAX_LAST_SAMPLE_TO_CLOSE_SECONDS = 120
V30_EXIT_REASONS = frozenset({
    "V30.3 EMERGENCY/HARD STOP", "V30.3 TRAILING/STOP",
    "V30.3 EARLY WEAKNESS", "V30.3 STALE", "V30.3 PUMP BREAKDOWN",
})


def finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(raw):
    if not isinstance(raw, str):
        return None
    try:
        x = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def exit_transition(state, entry, sample, opened_at, observed_at):
    """Mirror V30.8 auto_exit threshold order; nothing is executed.

    Independent from the runtime implementation by design. Both are checked
    against actual execution evidence before anyone could claim parity.
    """
    price, score = finite(sample.get("price_sda")), finite(sample.get("score"))
    m15, flow = finite(sample.get("m15")), finite(sample.get("flow_1h"))
    if (price is None or price <= 0 or score is None or m15 is None
            or flow is None or entry <= 0 or observed_at < opened_at):
        raise ValueError("Incomplete exit transition features")
    peak = max(state["peak"], price)
    mfe = (peak / entry - 1) * 100
    roi = (price / entry - 1) * 100
    stop = state["stop"]
    if mfe >= 2.5:
        stop = max(stop, entry * 1.002)
    if mfe >= 4:
        stop = max(stop, peak * .975)
    if mfe >= 10:
        stop = max(stop, peak * .95)
    if mfe >= 20:
        stop = max(stop, peak * .92)
    if mfe >= 40:
        stop = max(stop, peak * .90)
    if mfe >= 70:
        stop = max(stop, peak * .88)
    weak = (m15 < 0 and flow <= 0) or score < 38
    weakness = (state["weak_count"] + 1 if weak
                else max(0, state["weak_count"] - 1))
    hours = max(0.0, (observed_at - opened_at).total_seconds() / 3600)
    next_state = {"peak": peak, "stop": stop, "weak_count": weakness}
    reason = (
        "V30.3 EMERGENCY/HARD STOP" if roi <= -3 else
        "V30.3 TRAILING/STOP" if price <= stop else
        "V30.3 EARLY WEAKNESS" if roi <= -1.75 and weakness >= 2 else
        "V30.3 STALE" if hours >= 2 and mfe < 2
                             and weakness >= 3 and roi < 0.5 else
        "V30.3 PUMP BREAKDOWN" if roi > 0.5 and mfe >= 2.5
                                      and weakness >= 3 else None
    )
    return next_state, reason


def compare_closed_trade(trade):
    """Return a small, auditable parity result.

    EXCLUDED is not a success. E.g. old paper trades missing quote timestamps,
    capped sample histories, stale prices and coarse monitoring are excluded.
    """
    if not isinstance(trade, dict) or trade.get("status") != "CLOSED":
        return {"result": "excluded", "reason": "not_closed_paper_trade"}
    if "V30" not in str(trade.get("v30_mode") or ""):
        return {"result": "excluded", "reason": "non_v30_mode"}
    reason = str(trade.get("close_reason") or "")
    if reason not in V30_EXIT_REASONS:
        return {"result": "excluded", "reason": "unsupported_exit_reason"}
    entry = finite(trade.get("entry_price"))
    opened, closed = timestamp(trade.get("opened_at")), timestamp(trade.get("closed_at"))
    samples = trade.get("pump_price_samples")
    if entry is None or entry <= 0 or opened is None or closed is None or opened >= closed:
        return {"result": "excluded", "reason": "invalid_entry_or_timestamps"}
    if not isinstance(samples, list) or not samples:
        return {"result": "excluded", "reason": "missing_price_samples"}
    state = {"peak": entry, "stop": entry * .975, "weak_count": 0}
    previous = opened
    last_observed = None
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            return {"result": "excluded", "reason": "invalid_sample"}
        observed, quote_at = timestamp(sample.get("at")), timestamp(sample.get("quote_at"))
        age = finite(sample.get("quote_age_seconds"))
        p = finite(sample.get("price_sda"))
        if observed is None or quote_at is None or age is None or p is None or p <= 0:
            return {"result": "excluded", "reason": "missing_observed_quote_evidence"}
        if not (0 <= age <= MAX_QUOTE_AGE_SECONDS) or quote_at > observed:
            return {"result": "excluded", "reason": "stale_or_future_quote"}
        if abs((observed - quote_at).total_seconds() - age) > 90:
            return {"result": "excluded", "reason": "quote_timestamp_disagreement"}
        if observed < opened or observed > closed + timedelta(seconds=5):
            return {"result": "excluded", "reason": "sample_outside_trade"}
        if observed < previous or (index and observed == previous):
            return {"result": "excluded", "reason": "nonchronological_samples"}
        if (observed - previous).total_seconds() > MAX_SAMPLE_GAP_SECONDS:
            return {"result": "excluded", "reason": "sample_gap_over_20m"}
        if index == 0 and (observed - opened).total_seconds() > MAX_FIRST_SAMPLE_DELAY_SECONDS:
            return {"result": "excluded", "reason": "missing_initial_observations"}
        if any(finite(sample.get(key)) is None for key in ("score", "m15", "flow_1h")):
            return {"result": "excluded", "reason": "missing_exit_signals"}
        state, projected = exit_transition(state, entry, sample, opened, observed)
        last_observed = observed
        if projected is not None:
            # Never call a reason correct based on an incomplete suffix.
            if index != len(samples)-1:
                return {"result": "mismatch", "reason": "predicted_earlier_exit",
                        "expected": projected, "actual": reason}
            actual_price = finite(trade.get("close_price"))
            if actual_price is None or abs(actual_price / p - 1) > 0.0001:
                return {"result": "mismatch", "reason": "exit_price_disagreement",
                        "expected": projected, "actual": reason}
            if (closed - observed).total_seconds() > MAX_LAST_SAMPLE_TO_CLOSE_SECONDS:
                return {"result": "excluded", "reason": "last_sample_far_from_exit"}
            return {"result": "match" if projected == reason else "mismatch",
                    "reason": "matching_exit" if projected == reason else "exit_reason_disagreement",
                    "expected": projected, "actual": reason}
        previous = observed
    if (closed - last_observed).total_seconds() > MAX_LAST_SAMPLE_TO_CLOSE_SECONDS:
        return {"result": "excluded", "reason": "last_sample_far_from_exit"}
    return {"result": "mismatch", "reason": "no_exit_predicted",
            "expected": None, "actual": reason}


def audit_archive(rows):
    if not isinstance(rows, list):
        raise ValueError("Archive must be a list")
    outcomes = [compare_closed_trade(t) for t in rows]
    counts = Counter(x["result"] for x in outcomes)
    excluded = Counter(x["reason"] for x in outcomes if x["result"] == "excluded")
    mismatches = Counter(x["reason"] for x in outcomes if x["result"] == "mismatch")
    comparable = counts["match"] + counts["mismatch"]
    return {
        "method": "historical_observed_sample_mirror_independent_not_fill_replay",
        "audited": len(outcomes), "comparable": comparable,
        "matched": counts["match"], "mismatched": counts["mismatch"],
        "excluded": counts["excluded"],
        "comparable_match_pct": round(100*counts["match"]/comparable,2)
                                if comparable else None,
        "excluded_by_reason": dict(sorted(excluded.items())),
        "mismatch_by_reason": dict(sorted(mismatches.items())),
        "runtime_entry_exit_parity_verified": False,
        "can_unlock_paper_autopromotion": False,
        "caveat": ("Matching observed V30 exit events is not a matched execution "
                   "path for virtual Shadow entries or alternative portfolio fills."),
    }
