"""Conservative, PAPER-only MACD veto using completed observed 1h candles.

Do not derive a signal from a partial current-hour candle or from outdated,
sparsely observed trade-hour samples. This module never opens a trade and is
not imported by real trading.
"""
from datetime import datetime, timezone
import math

MIN_CLOSED_TRADE_HOURS = 35
RECENT_WINDOW_HOURS = 12
MIN_RECENT_TRADE_HOURS = 4
MAX_RECENT_GAP_HOURS = 5.0
MAX_COMPLETED_QUOTE_AGE_SECONDS = 7200


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        val = float(value)
        return val if math.isfinite(val) else None
    except (TypeError, ValueError, OverflowError):
        return None


def completed_hourly_macd(history, as_of):
    """Calculate a MACD from closed, genuinely traded 1h buckets.

    These are *observed* hours, not synthetic gap-filled hourly bars. Sparse
    data are explicitly refused. The newest incomplete hour and future
    transactions cannot influence the calculation.
    """
    from technical_analysis import build_candles, bucket_ts, macd, parse_ts
    base = {
        "available": False, "reason": "insufficient_completed_trade_hours",
        "method": "completed_observed_trade_hour_macd_sparse_guard",
        "macd": None, "completed_bars": 0, "traded_bars": 0,
        "last_completed_at": None, "quote_age_seconds": None,
        "recent_traded_hours": 0, "max_recent_gap_hours": None,
    }
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        return {**base, "reason": "invalid_as_of"}
    now = as_of.astimezone(timezone.utc)
    current_start = bucket_ts(now, 3600)
    observed = [
        tx for tx in (history if isinstance(history, list) else [])
        if isinstance(tx, dict) and (when := parse_ts(tx.get("timestamp"))) is not None
        and when.timestamp() < current_start
    ]
    # build_candles groups all valid records into occupied, ordered hourly
    # buckets and handles transaction sorting for high/low/close.
    candles = build_candles(observed, 3600)
    n = len(candles)
    if n < MIN_CLOSED_TRADE_HOURS:
        return {**base, "completed_bars": n, "traded_bars": n}
    times = [parse_ts(c["timestamp"]) for c in candles]
    last_end = times[-1].timestamp() + 3600
    # A bucket is complete, but its last actual trade may be earlier.
    latest_trade_ts = max(parse_ts(tx["timestamp"]) for tx in observed
                          if bucket_ts(parse_ts(tx["timestamp"]), 3600) ==
                          int(times[-1].timestamp()))
    age = (now - latest_trade_ts).total_seconds()
    recent = [t for t in times if t.timestamp() >= current_start - RECENT_WINDOW_HOURS*3600]
    latest12 = times[-12:]
    max_gap = max(((b-a).total_seconds()/3600 for a,b in zip(latest12,latest12[1:])), default=0)
    base.update({
        "completed_bars": n, "traded_bars": n,
        "last_completed_at": datetime.fromtimestamp(last_end, timezone.utc).isoformat(),
        "quote_age_seconds": round(age, 2),
        "recent_traded_hours": len(recent),
        "max_recent_gap_hours": round(max_gap, 2),
    })
    if age > MAX_COMPLETED_QUOTE_AGE_SECONDS or age < 0:
        return {**base, "reason": "stale_completed_hour"}
    if len(recent) < MIN_RECENT_TRADE_HOURS or max_gap > MAX_RECENT_GAP_HOURS:
        return {**base, "reason": "sparse_recent_trade_hours"}
    closes = [float(c["close"]) for c in candles]
    result = macd(closes)
    if not isinstance(result, dict):
        return {**base, "reason": "insufficient_macd_points"}
    price = closes[-1]
    if price <= 0 or not math.isfinite(price):
        return {**base, "reason": "invalid_price"}
    # MACD histogram is normalized to % of the last observed price to keep
    # reported strength comparable between different SDA token prices.
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    line = [a-b for a,b in zip(fast,slow)]
    signal = _ema(line, 9)
    hist = [a-b for a,b in zip(line,signal)]
    result.update({
        "histogram_pct": round(hist[-1] / price * 100, 8),
        "histogram_delta_pct": round((hist[-1]-hist[-2]) / price * 100, 8),
        "histogram_fading_2": hist[-1] > 0 and hist[-1] < hist[-2] < hist[-3],
    })
    return {**base, "available": True, "reason": "ok", "macd": result}


def _ema(values, period):
    # Identical EMA recurrence to technical_analysis. Avoid a cyclic import
    # at module import time (technical_analysis imports v32_breakout).
    alpha = 2.0 / (period+1)
    out = [values[0]]
    for x in values[1:]:
        out.append(x*alpha + out[-1]*(1-alpha))
    return out


def paper_macd_veto(analysis, as_of=None):
    """Only veto an observed, quality-verified bearish AND weakening MACD.

    On missing/stale data do not manufacture bullish evidence or block an
    otherwise valid V30 entry. State UNAVAILABLE explicitly for dashboards.
    Use this exclusively for new PAPER entries; exits remain independent.
    """
    raw = ((analysis.get("technical") or {}).get("1h_completed")
           if isinstance(analysis, dict) else None)
    result = {"applied": False, "blocked": False, "status": "UNAVAILABLE",
              "source": "1h_completed", "reason": "missing"}
    if not isinstance(raw, dict):
        return result
    if raw.get("available") is not True:
        return {**result, "reason": raw.get("reason") or "unavailable"}
    end = _parse(raw.get("last_completed_at"))
    when = as_of if as_of is not None else datetime.now(timezone.utc)
    age = finite(raw.get("quote_age_seconds"))
    m = raw.get("macd") or {}
    hist = finite(m.get("histogram_pct")) if isinstance(m, dict) else None
    delta = finite(m.get("histogram_delta_pct")) if isinstance(m, dict) else None
    recent = finite(raw.get("recent_traded_hours"))
    gap = finite(raw.get("max_recent_gap_hours"))
    if (not isinstance(when, datetime) or when.tzinfo is None or end is None
            or end > when or (when-end).total_seconds() > 7200
            or age is None or age < 0 or age > MAX_COMPLETED_QUOTE_AGE_SECONDS
            or recent is None or recent < MIN_RECENT_TRADE_HOURS
            or gap is None or gap > MAX_RECENT_GAP_HOURS
            or hist is None or delta is None):
        return {**result, "reason": "stale_or_invalid_signal"}
    bearish_weakening = hist < 0 and delta < 0
    return {
        "applied": True, "blocked": bearish_weakening,
        "status": "BEARISH_VETO" if bearish_weakening else "ALLOWED",
        "source": "1h_completed", "reason": "bearish_and_weakening"
        if bearish_weakening else "not_bearish_and_weakening",
        "histogram_pct": hist, "histogram_delta_pct": delta,
        "last_completed_at": end.isoformat(),
    }


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None
    except ValueError:
        return None
