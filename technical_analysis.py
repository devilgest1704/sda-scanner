#!/usr/bin/env python3
"""Technical-analysis layer for SDA market data.

This module is deliberately ANALYTICAL ONLY in V1. It does not change BUY/SELL
logic. It derives candles from the transaction history already collected by
market_scanner.py and writes the results back into market_data.json under
analysis['technical'].
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from tools.v32_breakout import analyze_breakout
from tools.v32_hourly_macd import completed_hourly_macd

MARKET_FILE = "market_data.json"
TIMEFRAMES = {"1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
INTRADAY_MACD_FRAMES = {"5m": 300, "15m": 900}
MIN_INTRADAY_TRADED_BARS = 21  # 60% of the 35 closed bars
MAX_INTRADAY_EMPTY_STREAK = 3
MIN_RSI_POINTS = 15
MIN_MACD_POINTS = 35
MIN_TREND_POINTS = 20
SR_LOOKBACK = 20


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def parse_ts(v):
    if not v:
        return None
    try:
        s = str(v)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def bucket_ts(dt, seconds):
    return (int(dt.timestamp()) // seconds) * seconds


def build_candles(history, seconds):
    buckets = {}
    for tx in history:
        dt = parse_ts(tx.get("timestamp"))
        price = num(tx.get("price"))
        volume = num(tx.get("volume"))
        if dt is None or price is None or price <= 0:
            continue
        if volume is None or volume < 0:
            volume = 0.0
        key = bucket_ts(dt, seconds)
        c = buckets.get(key)
        if c is None:
            buckets[key] = {
                "timestamp": datetime.fromtimestamp(key, timezone.utc).isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "transactions": 1,
            }
        else:
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            c["volume"] += volume
            c["transactions"] += 1
    return [buckets[k] for k in sorted(buckets)]


def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for x in values[1:]:
        out.append((x * alpha) + (out[-1] * (1.0 - alpha)))
    return out


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))


def macd(values):
    if len(values) < MIN_MACD_POINTS:
        return None
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [a - b for a, b in zip(fast, slow)]
    signal = ema(line, 9)
    histogram = line[-1] - signal[-1]
    previous_histogram = line[-2] - signal[-2]
    return {
        "macd": line[-1],
        "signal": signal[-1],
        "histogram": histogram,
        "bullish": line[-1] > signal[-1],
        "histogram_rising": histogram > previous_histogram,
    }


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev))
        trs.append(tr)
    return sum(trs[-period:]) / period


def timeframe_analysis(candles):
    closes = [c["close"] for c in candles]
    if not closes:
        return {"available": False, "candles": 0}

    current = closes[-1]
    rsi_value = rsi(closes, 14)
    ema20 = ema(closes, 20)[-1] if len(closes) >= 20 else None
    ema50 = ema(closes, 50)[-1] if len(closes) >= 50 else None
    atr14 = atr(candles, 14)
    macd_value = macd(closes)
    first_ts = parse_ts(candles[0]["timestamp"])
    last_ts = parse_ts(candles[-1]["timestamp"])
    coverage_days = max(0.0, (last_ts - first_ts).total_seconds() / 86400.0) if first_ts and last_ts else 0.0

    result = {
        "available": len(candles) >= MIN_TREND_POINTS,
        "candles": len(candles),
        "current_close": current,
        "first_timestamp": candles[0]["timestamp"],
        "last_timestamp": candles[-1]["timestamp"],
        "coverage_days": coverage_days,
        "rsi_14": rsi_value,
        "rsi14": rsi_value,
        "ema_20": ema20,
        "ema20": ema20,
        "ema_50": ema50,
        "ema50": ema50,
        "atr_14": atr14,
        "atr14": atr14,
        "macd": macd_value,
    }

    recent = candles[-SR_LOOKBACK:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)
    result["support"] = support
    result["resistance"] = resistance
    result["distance_to_support_pct"] = ((current - support) / current * 100.0) if current else None
    result["distance_to_resistance_pct"] = ((resistance - current) / current * 100.0) if current else None
    result["support_distance_pct"] = result["distance_to_support_pct"]
    result["resistance_distance_pct"] = result["distance_to_resistance_pct"]

    if ema20 is not None and ema50 is not None:
        if current > ema20 > ema50:
            trend = "BULLISH"
        elif current < ema20 < ema50:
            trend = "BEARISH"
        else:
            trend = "MIXED"
    elif ema20 is not None:
        if current > ema20:
            trend = "BULLISH_SHORT"
        elif current < ema20:
            trend = "BEARISH_SHORT"
        else:
            trend = "FLAT_SHORT"
    else:
        trend = "INSUFFICIENT_DATA"
    result["trend"] = trend

    if rsi_value is None:
        result["rsi_state"] = "INSUFFICIENT_DATA"
    elif rsi_value >= 70:
        result["rsi_state"] = "OVERBOUGHT"
    elif rsi_value <= 30:
        result["rsi_state"] = "OVERSOLD"
    else:
        result["rsi_state"] = "NEUTRAL"

    return result



def intraday_macd_analysis(history, seconds, as_of):
    """Continuous-time, completed-candle MACD, research only.

    The old general-purpose candle builder omits intervals without transactions,
    incorrectly compressing time for short indicators. Here missing intervals
    carry the LAST actually observed trade close, are counted as imputed, and
    reject too sparse histories. The current incomplete candle is ALWAYS
    excluded; no future transactions are used. Unavailable means NO signal,
    never zero or bullish.
    """
    base = {
        "available": False, "macd": None,
        "last_completed_at": None, "reason": "insufficient_history",
        "completed_bars": 0, "traded_bars": 0, "carried_bars": 0,
        "max_empty_streak": None, "quote_age_seconds": None,
        "method": "completed_fixed_interval_last_trade_close_with_bounded_carry",
    }
    if as_of is None or as_of.tzinfo is None or seconds <= 0:
        return {**base, "reason": "invalid_as_of"}
    as_of = as_of.astimezone(timezone.utc)
    current_start = bucket_ts(as_of, seconds)
    last_start = current_start - seconds
    first_start = last_start - (MIN_MACD_POINTS - 1) * seconds
    base["last_completed_at"] = datetime.fromtimestamp(current_start, timezone.utc).isoformat()
    records = []
    for tx in history if isinstance(history, list) else []:
        if not isinstance(tx, dict):
            continue
        dt = parse_ts(tx.get("timestamp"))
        p = num(tx.get("price"))
        if (dt is None or dt > as_of or p is None or
                not math.isfinite(p) or p <= 0):
            continue
        # Transaction order from external APIs is not guaranteed.
        records.append((dt, p))
    records.sort(key=lambda x: x[0])
    latest_by_bucket = {}
    prior_price = None
    for dt, p in records:
        key = bucket_ts(dt, seconds)
        if key < first_start:
            prior_price = p
        elif first_start <= key <= last_start:
            latest_by_bucket[key] = (dt, p)
        # Ignore current, incomplete interval even if its price is known.
    closes = []
    last_quote_at = None
    empty_streak = max_empty = traded = 0
    for key in range(first_start, current_start, seconds):
        hit = latest_by_bucket.get(key)
        if hit is not None:
            last_quote_at, prior_price = hit
            traded += 1
            empty_streak = 0
        else:
            empty_streak += 1
            max_empty = max(max_empty, empty_streak)
        if prior_price is None:
            return {**base, "reason": "missing_initial_price"}
        closes.append(prior_price)
    quote_age = (as_of-last_quote_at).total_seconds() if last_quote_at else None
    base.update({"completed_bars": len(closes), "traded_bars": traded,
                 "carried_bars": len(closes)-traded,
                 "max_empty_streak": max_empty, "quote_age_seconds": quote_age})
    if traded < MIN_INTRADAY_TRADED_BARS:
        return {**base, "reason": "insufficient_traded_bars"}
    if max_empty > MAX_INTRADAY_EMPTY_STREAK:
        return {**base, "reason": "too_many_consecutive_missing_bars"}
    if quote_age is None or quote_age > max(600, 2*seconds):
        return {**base, "reason": "last_completed_price_too_old"}
    macd_value = macd(closes)
    if macd_value is None:
        return {**base, "reason": "insufficient_macd_bars"}
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    line = [f-s for f,s in zip(fast,slow)]
    sig = ema(line, 9)
    h = [a-b for a,b in zip(line,sig)]
    # Express histograms as a percentage of the last *observed* close.
    # Raw histograms cannot be compared across differently priced SDA tokens.
    last = closes[-1]
    macd_value.update({
        "histogram_pct": round(100*h[-1]/last, 8),
        "histogram_delta_pct": round(100*(h[-1]-h[-2])/last, 8),
        "histogram_fading_2": h[-1] > 0 and h[-1] < h[-2] < h[-3],
    })
    return {**base, "available": True, "reason": "ok", "macd": macd_value}


def analyze_token(token_data, as_of=None):
    history = token_data.get("transactions", []) if isinstance(token_data, dict) else []
    results = {name: timeframe_analysis(build_candles(history, seconds))
               for name, seconds in TIMEFRAMES.items()}
    at = as_of or datetime.now(timezone.utc)
    results.update({name: intraday_macd_analysis(history, seconds, at)
                    for name, seconds in INTRADAY_MACD_FRAMES.items()})
    # Unlike sparse intraday data, traded hourly buckets often support a
    # guarded MACD. Only COMPLETED hours may influence the PAPER BUY veto;
    # the existing general-purpose 1h chart is unchanged.
    results["1h_completed"] = completed_hourly_macd(history, at)
    # Unlike MACD, breakout research can still inspect sparse but valid
    # transaction data. Never mix its research signal into V30 BUY/SELL.
    results["breakout_volume_trend"] = analyze_breakout(history, at)
    return results


def main():
    path = Path(MARKET_FILE)
    if not path.exists():
        return
    try:
        market = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    tokens = market.get("tokens") if isinstance(market, dict) else None
    if not isinstance(tokens, dict):
        return

    analyzed = 0
    as_of = parse_ts(market.get("updated_at")) or datetime.now(timezone.utc)
    for token_data in tokens.values():
        if not isinstance(token_data, dict):
            continue
        analysis = token_data.get("analysis")
        if not isinstance(analysis, dict):
            continue
        analysis["technical"] = analyze_token(token_data, as_of)
        analyzed += 1

    market["technical_analysis"] = {
        "version": 4,
        "mode": "analysis_only",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": list(TIMEFRAMES.keys()) + list(INTRADAY_MACD_FRAMES.keys()),
        "indicators": ["RSI14", "EMA20", "EMA50", "MACD12_26_9", "ATR14", "SUPPORT20", "RESISTANCE20"],
        "tokens_analyzed": analyzed,
        "history_source": "market_scanner transaction history",
        "history_expanded": True,
        "intraday_macd": "research_only_closed_bars_no_future_price",
        "hourly_macd": "completed_observed_trade_hour_paper_buy_veto",
        "intraday_missing_bars": "bounded_carry_forward_unavailable_if_sparse",
        "breakout_volume_trend": "research_only_observed_transactions_excluding_future_quotes",
    }
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(tmp).replace(path)
    print(f"📐 TECHNICAL ANALYSIS: {analyzed} tokens updated")


if __name__ == "__main__":
    main()
