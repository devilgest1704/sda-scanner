"""Conservative, PAPER-only MACD veto using completed observed 1h candles.

Do not derive a signal from a partial current-hour candle or from outdated,
sparsely observed trade-hour samples. This module never opens a trade and is
not imported by real trading.
"""
from datetime import datetime, timezone
import math

# MACD (12,26,9) needs a CLOCK-TIME series, not the last 35 arbitrarily
# spaced traded hours. Missing hours carry only an actually observed last
# traded close. No synthetic volume or trade is ever counted.
MIN_CLOSED_TRADE_HOURS = 35
LOOKBACK_CLOCK_HOURS = 60
QUALITY_WINDOW_HOURS = 35
MIN_LOOKBACK_TRADE_HOURS = 8
MAX_LOOKBACK_GAP_HOURS = 12.0
RECENT_WINDOW_HOURS = 12
MIN_RECENT_TRADE_HOURS = 4
MAX_RECENT_GAP_HOURS = 5.0
MAX_COMPLETED_QUOTE_AGE_SECONDS = 7200
MACD_METHOD = "completed_clock_hour_last_observed_close_bounded_carry_v2"


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        val = float(value)
        return val if math.isfinite(val) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _largest_clock_gap(traded_bucket_keys, start, end):
    """Elapsed HOURS between genuine observations inside [start,end).

    Include both boundaries; unlike the old code this never reaches back to
    the last 12 traded hours scattered across the historical archive.
    """
    keys = [key for key in traded_bucket_keys if start <= key < end]
    endpoints = [start] + keys + [end]
    return max((right-left)/3600 for left,right in
               zip(endpoints, endpoints[1:]))


def completed_hourly_macd(history, as_of):
    """MACD on chronological completed CLOCK hours, with honest data quality.

    Hours without trades carry the last actual observed close, but are marked
    as carried (not counted as traded). Reject insufficient activity in the
    last 35 and 12 clock hours, large genuine recent gaps, and stale prices.
    Incomplete/current or future-hour transactions are never read. No
    intraday or lookahead substitute can accidentally block a PAPER BUY.
    """
    from technical_analysis import bucket_ts, macd, parse_ts
    base = {
        "available": False, "reason": "insufficient_completed_clock_hours",
        "method": MACD_METHOD, "macd": None,
        "completed_bars": 0, "traded_bars": 0, "carried_bars": 0,
        "lookback_traded_hours": 0, "max_lookback_gap_hours": None,
        "last_completed_at": None, "quote_age_seconds": None,
        "recent_traded_hours": 0, "max_recent_gap_hours": None,
    }
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        return {**base, "reason": "invalid_as_of"}
    now = as_of.astimezone(timezone.utc)
    current_start = bucket_ts(now, 3600)
    records = []
    for tx in history if isinstance(history, list) else []:
        if not isinstance(tx, dict):
            continue
        at = parse_ts(tx.get("timestamp"))
        price = finite(tx.get("price"))
        if (at is None or at.timestamp() >= current_start
                or price is None or price <= 0):
            continue
        records.append((at, str(tx.get("hash") or ""), price))
    if not records:
        return {**base, "reason": "no_completed_observed_trades"}
    records.sort(key=lambda record: (record[0], record[1]))
    prices = {}
    timestamps = {}
    for at, _, price in records:
        key = bucket_ts(at, 3600)
        prices[key] = price
        timestamps[key] = at

    # A previous real quote may initialize the window, but there is never
    # an invented starting price; the earliest observed hour is the fallback.
    window_start = current_start - LOOKBACK_CLOCK_HOURS*3600
    first = max(window_start, min(prices))
    prior = None
    for key in prices:
        if key < first:
            prior = prices[key]
        else:
            break
    closes = []
    trade_flags = []
    for key in range(first, current_start, 3600):
        traded = key in prices
        if traded:
            prior = prices[key]
        if prior is None:
            continue
        closes.append(prior)
        trade_flags.append(traded)

    latest_bucket = max(prices)
    age = (now-timestamps[latest_bucket]).total_seconds()
    recent_start = current_start-RECENT_WINDOW_HOURS*3600
    lookback_start = current_start-QUALITY_WINDOW_HOURS*3600
    keys = sorted(prices)
    recent_trades = sum(recent_start <= key < current_start for key in keys)
    lookback_trades = sum(lookback_start <= key < current_start for key in keys)
    max_recent_gap = _largest_clock_gap(keys, recent_start, current_start)
    max_lookback_gap = _largest_clock_gap(keys, lookback_start, current_start)
    base.update({
        "completed_bars": len(closes), "traded_bars": sum(trade_flags),
        "carried_bars": len(closes)-sum(trade_flags),
        # This is the end of the most recently COMPLETED hour. It is
        # intentionally not the time of the last trade or a partial candle.
        "last_completed_at": datetime.fromtimestamp(
            current_start, timezone.utc).isoformat(),
        "quote_age_seconds": round(age, 2),
        "recent_traded_hours": recent_trades,
        "max_recent_gap_hours": round(max_recent_gap, 2),
        "lookback_traded_hours": lookback_trades,
        "max_lookback_gap_hours": round(max_lookback_gap, 2),
    })
    if len(closes) < MIN_CLOSED_TRADE_HOURS:
        return {**base, "reason": "insufficient_completed_clock_hours"}
    if age < 0 or age > MAX_COMPLETED_QUOTE_AGE_SECONDS:
        return {**base, "reason": "stale_completed_hour"}
    if (recent_trades < MIN_RECENT_TRADE_HOURS
            or max_recent_gap > MAX_RECENT_GAP_HOURS):
        return {**base, "reason": "sparse_recent_trade_hours"}
    if (lookback_trades < MIN_LOOKBACK_TRADE_HOURS
            or max_lookback_gap > MAX_LOOKBACK_GAP_HOURS):
        return {**base, "reason": "sparse_lookback_trade_hours"}
    result = macd(closes)
    if not isinstance(result, dict):
        return {**base, "reason": "insufficient_macd_points"}
    price = closes[-1]
    if price <= 0 or not math.isfinite(price):
        return {**base, "reason": "invalid_price"}
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
    lookback_trades = finite(raw.get("lookback_traded_hours"))
    lookback_gap = finite(raw.get("max_lookback_gap_hours"))
    if (raw.get("method") != MACD_METHOD
            or lookback_trades is None or lookback_trades < MIN_LOOKBACK_TRADE_HOURS
            or lookback_gap is None or lookback_gap > MAX_LOOKBACK_GAP_HOURS
            or not isinstance(when, datetime) or when.tzinfo is None or end is None
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
