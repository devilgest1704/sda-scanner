"""V21 strategy overlay: technical confirmation for paper BUY/SELL decisions.

Keeps the existing market/whale score as the base signal, then applies RSI,
EMA, MACD, trend and support/resistance confirmations. Emergency exits remain
price/risk driven and are never blocked by technical indicators.
"""


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except Exception:
        return default


def _tech(analysis):
    t = analysis.get("technical", {}) if isinstance(analysis, dict) else {}
    return t if isinstance(t, dict) else {}


def technical_confirmation(analysis):
    t = _tech(analysis)
    bull = 0
    bear = 0
    evidence = []

    for tf in ("1h", "4h", "1d"):
        x = t.get(tf, {}) or {}
        trend = str(x.get("trend", "")).upper()
        if trend.startswith("BULLISH"):
            bull += 1
        elif trend.startswith("BEARISH"):
            bear += 1

    d = t.get("1d", {}) or {}
    h = d.get("macd", {}) or {}
    if isinstance(h, dict):
        if h.get("bullish") is True:
            bull += 1
            evidence.append("MACD bullish")
        elif h.get("bullish") is False:
            bear += 1
            evidence.append("MACD bearish")
        if h.get("histogram_rising") is True:
            bull += 1
        elif h.get("histogram_rising") is False:
            bear += 1

    rsi = _num(d.get("rsi_14"), -1)
    if rsi >= 0:
        if rsi < 35:
            bull += 1
            evidence.append(f"RSI oversold {rsi:.1f}")
        elif rsi > 72:
            bear += 1
            evidence.append(f"RSI high {rsi:.1f}")
        elif rsi < 55:
            bear += 1
        else:
            bull += 1

    close = _num(d.get("current_close"), 0)
    ema20 = _num(d.get("ema_20"), 0)
    ema50 = _num(d.get("ema_50"), 0)
    if close > 0 and ema20 > 0:
        if close > ema20:
            bull += 1
        else:
            bear += 1
    if close > 0 and ema50 > 0:
        if close > ema50:
            bull += 1
        else:
            bear += 1

    ds = _num(d.get("distance_to_support_pct"), -999)
    dr = _num(d.get("distance_to_resistance_pct"), -999)
    if ds >= 0 and ds <= 2.0:
        bull += 1
        evidence.append("near support")
    if dr >= 0 and dr <= 2.0:
        bear += 1
        evidence.append("near resistance")

    return {"bull": bull, "bear": bear, "evidence": evidence}


def patch_engine(engine):
    original_score = engine.score

    def score_v21(addr, analysis, ws):
        base = original_score(addr, analysis, ws)
        tc = technical_confirmation(analysis)
        score = _num(base.get("confidence"))

        # Technical confirmation adjusts, but does not dominate, the existing score.
        # This keeps market/whale flow as the primary signal.
        adjustment = min(8, tc["bull"] * 1.5) - min(10, tc["bear"] * 1.8)
        score = max(0, min(100, score + adjustment))

        # At the lower 65 BUY threshold, require at least two bullish technical
        # confirmations when technical data is available. Otherwise cap the score
        # just below the buy threshold rather than buying a technically weak setup.
        technical_available = bool(_tech(analysis))
        if technical_available and tc["bull"] < 2:
            score = min(score, 64)

        out = dict(base)
        out["confidence"] = int(round(score))
        out["base_confidence"] = int(round(_num(base.get("confidence"))))
        out["technical_bull"] = tc["bull"]
        out["technical_bear"] = tc["bear"]
        out["technical_evidence"] = tc["evidence"]
        out["technical_buy_gate"] = (not technical_available) or tc["bull"] >= 2
        out["technical_sell_confirmed"] = tc["bear"] >= 2
        return out

    engine.score = score_v21
    return engine


def technical_sell_confirmed(analysis):
    tc = technical_confirmation(analysis)
    return tc["bear"] >= 2
