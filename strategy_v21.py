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
    return any(
        frame.function == "main" and frame.frame.f_globals.get("__name__") == "engine_legacy"
        for frame in inspect.stack()
    )


def _predictive_score(addr, analysis, ws, engine):
    """Return the canonical predictive BUY score from main.py.

    60 is the normal hard threshold. A narrow 58-59 bridge is allowed only for
    genuinely strong setups; it exists so a rounded/borderline candidate does
    not get discarded solely because the composite score is one or two points
    below the normal threshold.
    """
    try:
        import main as scanner
        predictor = getattr(scanner, "_paper_score_predictive", None)
        if predictor is None:
            return None
        out = dict(predictor(addr, analysis, ws))
        threshold = _num(getattr(engine, "BUY_THRESHOLD", 60), 60)
        buy_score = _num(out.get("buy_score"), out.get("confidence"))
        reason_text = str(out.get("paper_buy_block_reason") or "")
        reasons = [x.strip() for x in reason_text.split(";") if x.strip()]

        # Remove legacy hard score vetoes. The canonical threshold is applied
        # here so V21 and main.py cannot disagree about the entry threshold.
        cleaned = []
        for reason in reasons:
            if reason.startswith("BUY score ") and (" < 65" in reason or " < 60" in reason):
                continue
            cleaned.append(reason)

        # Borderline bridge: 58-59 can become a BUY only for an exceptionally
        # strong momentum/flow setup with no bearish technical confirmation.
        near_threshold = False
        pred = out.get("paper_prediction") or {}
        if 58.0 <= buy_score < threshold and isinstance(pred, dict) and pred.get("ready"):
            p5 = _num(pred.get("p5"))
            p10 = _num(pred.get("p10"))
            mean_roi = _num(pred.get("mean_roi"))
            m1 = _num(out.get("m1h"))
            m4 = _num(out.get("m4h"))
            flow = _num(out.get("net_1h"))
            m15 = _num(out.get("m15"))
            trades = _num(out.get("trades_1h"))
            tc = technical_confirmation(analysis)
            near_threshold = (
                p5 >= 0.62 and p10 >= 0.35 and mean_roi > 0
                and m1 >= 15.0 and m4 >= 10.0 and flow > 0
                and m15 >= -0.25 and trades >= 3
                and tc["bull"] >= 2 and tc["bear"] == 0
            )

        if near_threshold:
            out["paper_near_threshold_buy"] = True
            out["paper_near_threshold_reason"] = "58-59 bridge: exceptional momentum + flow + prediction + technical confirmation"
            out["confidence"] = threshold
            out["paper_buy_blocked"] = bool(cleaned)
            out["paper_buy_block_reason"] = "; ".join(cleaned)
        else:
            if buy_score < threshold:
                cleaned.append(f"BUY score {buy_score:.0f} < {threshold:.0f}")
            blocked = bool(cleaned)
            out["confidence"] = min(buy_score, threshold - 1.0) if blocked else buy_score
            out["paper_buy_blocked"] = blocked
            out["paper_buy_block_reason"] = "; ".join(cleaned)

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
                technical_available = bool(_tech(analysis))
                threshold = _num(getattr(engine, "BUY_THRESHOLD", 60), 60)
                score = _num(predictive.get("confidence"))
                if technical_available and tc["bull"] < 2 and not predictive.get("paper_near_threshold_buy"):
                    score = min(score, threshold - 1.0)
                out = dict(predictive)
                out["confidence"] = int(round(score))
                out["base_confidence"] = int(round(_num(predictive.get("buy_score", score))))
                out["technical_bull"] = tc["bull"]
                out["technical_bear"] = tc["bear"]
                out["technical_evidence"] = tc["evidence"]
                out["technical_buy_gate"] = (not technical_available) or tc["bull"] >= 2
                out["technical_sell_confirmed"] = tc["bear"] >= 2
                return out

        base = original_score(addr, analysis, ws)
        tc = technical_confirmation(analysis)
        score = _num(base.get("confidence"))
        adjustment = min(8, tc["bull"] * 1.5) - min(10, tc["bear"] * 1.8)
        score = max(0, min(100, score + adjustment))
        technical_available = bool(_tech(analysis))
        threshold = _num(getattr(engine, "BUY_THRESHOLD", 60), 60)
        if technical_available and tc["bull"] < 2:
            score = min(score, threshold - 1.0)
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
    return technical_confirmation(analysis)["bear"] >= 2


def evaluate_exit(pnl_pct, score, momentum_1h, flow_1h, technical_bearish):
    """Single source of truth for V21 paper and POSITION ACTION exits."""
    pnl = _num(pnl_pct); score = _num(score); momentum = _num(momentum_1h); flow = _num(flow_1h); bearish = bool(technical_bearish)
    return {"emergency": pnl <= -15.0 and score < 35 and momentum < 0 and flow < 0, "negative": score < 30 and momentum < -1.0 and flow < 0 and pnl < -3.0 and bearish, "weakening": pnl > 0 and score < 40 and (momentum < 0 or flow < 0) and bearish}
