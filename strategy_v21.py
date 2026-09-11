"""V21 strategy overlay: shared technical confirmation and exit policy."""
# Shared exit policy is consumed by both paper trading and Telegram Position Action.

import inspect


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except Exception:
        return default


def _tech(analysis):
    t = analysis.get("technical", {}) if isinstance(analysis, dict) else {}
    return t if isinstance(t, dict) else {}


def technical_confirmation(analysis):
    t = _tech(analysis); bull = 0; bear = 0; evidence = []
    for tf in ("1h", "4h", "1d"):
        x = t.get(tf, {}) or {}; trend = str(x.get("trend", "")).upper()
        if trend.startswith("BULLISH"): bull += 1
        elif trend.startswith("BEARISH"): bear += 1
    d = t.get("1d", {}) or {}; h = d.get("macd", {}) or {}
    if isinstance(h, dict):
        if h.get("bullish") is True: bull += 1; evidence.append("MACD bullish")
        elif h.get("bullish") is False: bear += 1; evidence.append("MACD bearish")
        if h.get("histogram_rising") is True: bull += 1
        elif h.get("histogram_rising") is False: bear += 1
    rsi = _num(d.get("rsi_14"), -1)
    if rsi >= 0:
        if rsi < 35: bull += 1; evidence.append(f"RSI oversold {rsi:.1f}")
        elif rsi > 72: bear += 1; evidence.append(f"RSI high {rsi:.1f}")
        elif rsi < 55: bear += 1
        else: bull += 1
    close = _num(d.get("current_close"), 0); ema20 = _num(d.get("ema_20"), 0); ema50 = _num(d.get("ema_50"), 0)
    if close > 0 and ema20 > 0: bull += 1 if close > ema20 else 0; bear += 1 if close <= ema20 else 0
    if close > 0 and ema50 > 0: bull += 1 if close > ema50 else 0; bear += 1 if close <= ema50 else 0
    ds = _num(d.get("distance_to_support_pct"), -999); dr = _num(d.get("distance_to_resistance_pct"), -999)
    if ds >= 0 and ds <= 2.0: bull += 1; evidence.append("near support")
    if dr >= 0 and dr <= 2.0: bear += 1; evidence.append("near resistance")
    return {"bull": bull, "bear": bear, "evidence": evidence}


def _caller_is_top_buy():
    return any(frame.function == "_buy_gate_rows" for frame in inspect.stack())


def _caller_is_paper_main():
    return any(frame.function == "main" and frame.frame.f_globals.get("__name__") == "engine_legacy" for frame in inspect.stack())


def _predictive_score(addr, analysis, ws, engine):
    """Return the canonical predictive BUY score from main.py."""
    try:
        import main as scanner
        predictor = getattr(scanner, "_paper_score_predictive", None)
        if predictor is None:
            return None
        out = dict(predictor(addr, analysis, ws))
        threshold = _num(getattr(engine, "BUY_THRESHOLD", 60), 60)
        buy_score = _num(out.get("buy_score"), out.get("confidence"))
        tc = technical_confirmation(analysis)
        bear = int(tc.get("bear") or 0)
        bull = int(tc.get("bull") or 0)

        # main.py is the single source of truth for the predictive BUY score.
        # Neutral technical data (0 bull / 0 bear) must neither add nor subtract.
        # Bearish evidence remains a hard technical veto.
        if bear == 0 and bull == 0:
            raw = _num(out.get("paper_raw_confidence"), buy_score)
            buy_score = max(buy_score, raw)
            out["buy_score"] = buy_score
            out["confidence"] = buy_score if buy_score >= threshold else min(buy_score, threshold - 1.0)
            out["paper_buy_blocked"] = buy_score < threshold
            out["paper_buy_block_reason"] = f"BUY score {buy_score:.0f} < {threshold:.0f}" if buy_score < threshold else ""
        elif bear > 0:
            out["confidence"] = min(buy_score, threshold - 1.0)
            out["paper_buy_blocked"] = True
            out["paper_buy_block_reason"] = f"technical confirmation {bull} bull / {bear} bear"

        out["buy_threshold"] = threshold
        return out
    except Exception:
        return None


def patch_engine(engine):
    original_score = engine.score

    def score_v21(addr, analysis, ws):
        if _caller_is_top_buy() or _caller_is_paper_main():
            predictive = _predictive_score(addr, analysis, ws, engine)
            if predictive is not None:
                tc = technical_confirmation(analysis)
                out = dict(predictive)
                out["technical_bull"] = tc["bull"]
                out["technical_bear"] = tc["bear"]
                out["technical_evidence"] = tc["evidence"]
                out["technical_buy_gate"] = tc["bear"] == 0
                out["technical_sell_confirmed"] = tc["bear"] >= 2
                return out

        base = original_score(addr, analysis, ws)
        tc = technical_confirmation(analysis)
        score = _num(base.get("confidence"))
        adjustment = min(8, tc["bull"] * 1.5) - min(10, tc["bear"] * 1.8)
        # Technical confirmation is additive only when it is actually present.
        # 0 bull / 0 bear is neutral and leaves the base score unchanged.
        score = max(0, min(100, score + adjustment))
        threshold = _num(getattr(engine, "BUY_THRESHOLD", 60), 60)
        if tc["bear"] > 0:
            score = min(score, threshold - 1.0)
        out = dict(base)
        out["confidence"] = int(round(score))
        out["base_confidence"] = int(round(_num(base.get("confidence"))))
        out["technical_bull"] = tc["bull"]
        out["technical_bear"] = tc["bear"]
        out["technical_evidence"] = tc["evidence"]
        out["technical_buy_gate"] = tc["bear"] == 0
        out["technical_sell_confirmed"] = tc["bear"] >= 2
        return out

    engine.score = score_v21
    return engine


def technical_sell_confirmed(analysis):
    return technical_confirmation(analysis)["bear"] >= 2


def evaluate_exit(pnl_pct, score, momentum_1h, flow_1h, technical_bearish):
    """Single source of truth for V21 paper and POSITION ACTION exits."""
    pnl = _num(pnl_pct); score = _num(score); momentum = _num(momentum_1h); flow = _num(flow_1h); bearish = bool(technical_bearish)
    return {"emergency": pnl <= -15.0 and score < 35 and momentum < 0 and flow < 0, "negative": score < 30 and momentum < -1.0 and flow < 0 and pnl < -3.0 and bearish, "weakening": pnl > 0 and score < 40 and (momentum < 0 or flow < 0) and bearish}
