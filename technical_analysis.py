#!/usr/bin/env python3
"""Technical-analysis layer for SDA market data.

This module is deliberately ANALYTICAL ONLY in V1.  It does not change BUY/SELL
logic.  It derives candles from the transaction history already collected by
market_scanner.py and writes the results back into market_data.json under
analysis['technical'].

Indicators:
- RSI(14)
- EMA20 / EMA50
- MACD(12,26,9)
- ATR(14)
- rolling support / resistance from the latest 20 completed/available candles
- distance to support / resistance
- trend state for 1h / 4h / 1d / 1w

If a timeframe does not have enough history, the corresponding values are None
and `available` is false.  We never manufacture a signal from insufficient data.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

MARKET_FILE = "market_data.json"
TIMEFRAMES = {
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}
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
    epoch = int(dt.timestamp())
    return (epoch // seconds) * seconds


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
    gains = []
    losses = []
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
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values):
    if len(values) < MIN_MACD_POINTS:
        return None
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [a - b for a, b in zip(fast, slow)]
    signal = ema(line, 9)
    return {
        "macd": line[-1],
        "signal": signal[-1],
        "histogram": line[-1] - signal[-1],
        "bullish": line[-1] > signal[-1],
        "histogram_rising": len(line) >= 2 and (line[-1] - signal[-1]) > (line[-2] - signal[-2]),
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
    result = {
        "available": len(candles) >= MIN_TREND_POINTS,
        "candles": len(candles),
        "current_close": current,
        "first_timestamp": candles[0]["timestamp"],
        "last_timestamp": candles[-1]["timestamp"],
        "coverage_days": max(0.0, (parse_ts(candles[-1]["timestamp"]) - parse_ts(candles[0]["timestamp"])).total_seconds() / 86400.0) if len(candles) > 1 else 0.0,
        "rsi_14": rsi(closes, 14),
        "ema_20": ema(closes, 20)[-1] if len(closes) >= 20 else None,
        "ema_50": ema(closes, 50)[-1] if len(closes) >= 50 else None,
        "atr_14": atr(candles, 14),
        "macd": macd(closes),
    }

    recent = candles[-SR_LOOKBACK:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)
    result["support"] = support
    result["resistance"] = resistance
    result["distance_to_support_pct"] = ((current - support) / current * 100.0) if current else None
    result["distance_to_resistance_pct"] = ((resistance - current) / current * 100.0) if current else None

    if result["ema_20"] is not None and result["ema_50"] is not None:
        if current > result["ema_20"] > result["ema_50"]:
            trend = "BULLISH"
        elif current < result["ema_20"] < result["ema_50"]:
            trend = "BEARISH"
        else:
            trend = "MIXED"
    else:
        trend = "INSUFFICIENT_DATA"
    result["trend"] = trend

    r = result["rsi_14"]
    if r is None:
        result["rsi_state"] = "INSUFFICIENT_DATA"
    elif r >= 70:
        result["rsi_state"] = "OVERBOUGHT"
    elif r <= 30:
        result["rsi_state"] = "OVERSOLD"
    else:
        result["rsi_state"] = "NEUTRAL"

    return result


def analyze_token(token_data):
    history = token_data.get("transactions", []) if isinstance(token_data, dict) else []
    out = {}
    for name, seconds in TIMEFRAMES.items():
        candles = build_candles(history, seconds)
        out[name] = timeframe_analysis(candles)
    return out


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
        technical = analyze_token(token_data)
        analysis["technical"] = technical
        analyzed += 1

    market["technical_analysis"] = {
        "version": 1,
        "mode": "analysis_only",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": list(TIMEFRAMES.keys()),
        "indicators": ["RSI14", "EMA20", "EMA50", "MACD12_26_9", "ATR14", "SUPPORT20", "RESISTANCE20"],
        "tokens_analyzed": analyzed,
    }
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(tmp).replace(path)
    print(f"📐 TECHNICAL ANALYSIS: {analyzed} tokens updated")


if __name__ == "__main__":
    main()
