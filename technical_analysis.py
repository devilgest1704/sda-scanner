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

MARKET_FILE = "market_data.json"
TIMEFRAMES = {"1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
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


def analyze_token(token_data):
    history = token_data.get("transactions", []) if isinstance(token_data, dict) else []
    return {name: timeframe_analysis(build_candles(history, seconds)) for name, seconds in TIMEFRAMES.items()}


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
    for token_data in tokens.values():
        if not isinstance(token_data, dict):
            continue
        analysis = token_data.get("analysis")
        if not isinstance(analysis, dict):
            continue
        analysis["technical"] = analyze_token(token_data)
        analyzed += 1

    market["technical_analysis"] = {
        "version": 2,
        "mode": "analysis_only",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": list(TIMEFRAMES.keys()),
        "indicators": ["RSI14", "EMA20", "EMA50", "MACD12_26_9", "ATR14", "SUPPORT20", "RESISTANCE20"],
        "tokens_analyzed": analyzed,
        "history_source": "market_scanner transaction history",
        "history_expanded": True,
    }
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(tmp).replace(path)
    print(f"📐 TECHNICAL ANALYSIS: {analyzed} tokens updated")


if __name__ == "__main__":
    main()
