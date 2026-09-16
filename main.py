"""Compatibility wrapper for the SDA scanner.

MAX-WIN Paper Trading profile:
- only high-quality setups are allowed to open
- predictor must agree with the setup
- max 6 concurrent paper positions
- max 1 new BUY per scan
- longer SL re-entry cooldown
- V24/V25 remain shadow-only until validated

The implementation lives in main_core.py.
"""

import main_core as _core
for _name, _value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name] = _value

MAX_WIN_BUY_THRESHOLD = 78.0
MAX_WIN_MAX_OPEN_POSITIONS = 6
MAX_WIN_MAX_NEW_BUYS_PER_RUN = 1
MAX_WIN_SL_COOLDOWN_SCANS = 12

_paper.BUY_THRESHOLD = MAX_WIN_BUY_THRESHOLD
_paper.MAX_OPEN_POSITIONS = MAX_WIN_MAX_OPEN_POSITIONS
_paper.MAX_NEW_BUYS_PER_RUN = MAX_WIN_MAX_NEW_BUYS_PER_RUN
_core.PAPER_SL_COOLDOWN_SCANS = MAX_WIN_SL_COOLDOWN_SCANS

# Controlled reversal entry.  This is deliberately narrower than the normal
# MAX-WIN path and is based on the V25 shadow event pattern: strong short-term
# reversal/activity can precede a move while M15/M4H are still negative.
REVERSAL_BUY_THRESHOLD = 68.0
REVERSAL_MIN_M1H = 8.0
REVERSAL_MIN_FLOW = 0.0
REVERSAL_MIN_TRADES = 20.0
REVERSAL_MIN_P5 = 0.40
REVERSAL_MIN_P10 = 0.20
REVERSAL_MIN_MEAN_ROI = 0.0
REVERSAL_MAX_M15 = 0.0
REVERSAL_MIN_M15 = -8.0
REVERSAL_MIN_M4H = -40.0
REVERSAL_MAX_BEAR = 1


def _strong_flow_bridge(s, analysis, pred=None):
    return False


def _v21_buy_decision(buy_score, s, analysis, pred):
    score = float(buy_score or 0); threshold = MAX_WIN_BUY_THRESHOLD
    try:
        from strategy_v21 import technical_confirmation
        tc = technical_confirmation(analysis)
    except Exception:
        tc = {"bull": 0, "bear": 0, "evidence": []}
    bull = int(tc.get("bull") or 0); bear = int(tc.get("bear") or 0); evidence = list(tc.get("evidence") or [])
    technical_ok = ((not bool(analysis.get("technical"))) or bull >= 2) and bear == 0
    m1 = float(s.get("m1h") or 0); m15 = float(s.get("m15") or 0); m4 = float(s.get("m4h") or 0)
    flow = float(s.get("net_1h") or 0); trades = float(s.get("trades_1h") or 0)
    prediction_ok = False; p5 = p10 = mean_roi = 0.0
    if isinstance(pred, dict) and pred.get("ready"):
        p5 = float(pred.get("p5") or 0); p10 = float(pred.get("p10") or 0); mean_roi = float(pred.get("mean_roi") or 0)
        prediction_ok = p5 >= 0.55 and p10 >= 0.25 and mean_roi > 0.0

    momentum_ok = m1 >= 3.0 and m15 >= 0.0 and m4 >= 0.0
    flow_ok = flow > 0.0; activity_ok = trades >= 5.0
    normal_allowed = score >= threshold and technical_ok and prediction_ok and momentum_ok and flow_ok and activity_ok

    # Reversal path: do not simply lower the global BUY threshold.  Require
    # simultaneous short-term strength, real activity and a positive predictor,
    # while allowing the lagging M15/M4H trend to remain negative within bounds.
    reversal_prediction_ok = (
        isinstance(pred, dict) and pred.get("ready") and
        p5 >= REVERSAL_MIN_P5 and p10 >= REVERSAL_MIN_P10 and mean_roi > REVERSAL_MIN_MEAN_ROI
    )
    reversal_shape_ok = (
        score >= REVERSAL_BUY_THRESHOLD and
        m1 >= REVERSAL_MIN_M1H and
        flow > REVERSAL_MIN_FLOW and
        trades >= REVERSAL_MIN_TRADES and
        REVERSAL_MIN_M15 <= m15 < REVERSAL_MAX_M15 and
        m4 >= REVERSAL_MIN_M4H and
        bear <= REVERSAL_MAX_BEAR and
        reversal_prediction_ok
    )
    reversal_allowed = reversal_shape_ok and not normal_allowed
    allowed = normal_allowed or reversal_allowed

    reasons = []
    if not allowed:
        if normal_allowed:
            reasons.append("normal MAX-WIN")
        else:
            reasons.append(f"BUY score {score:.0f} < {threshold:.0f}")
            if not technical_ok: reasons.append(f"technical {bull} bull / {bear} bear")
            if not prediction_ok: reasons.append(f"prediction weak P5={p5:.0%} P10={p10:.0%} mean={mean_roi:+.1f}%")
            if not momentum_ok: reasons.append(f"momentum {m15:+.1f}/{m1:+.1f}/{m4:+.1f}%")
            if not flow_ok: reasons.append("1h SDA flow <= 0")
            if not activity_ok: reasons.append(f"only {trades:.0f} trades/1h")
            if not reversal_shape_ok:
                reasons.append("reversal profile not confirmed")
    else:
        reasons.append("reversal BUY" if reversal_allowed else "normal MAX-WIN BUY")

    return {
        "allowed": allowed,
        "score": score,
        "raw_score": score,
        "adjustment": 0.0,
        "technical_bull": bull,
        "technical_bear": bear,
        "technical_evidence": evidence,
        "bridge": False,
        "flow_bridge": False,
        "reversal_buy": reversal_allowed,
        "reversal_profile": "M1H+FLOW+ACTIVITY" if reversal_allowed else "",
        "reason": "; ".join(reasons),
    }


_core._v21_buy_decision = _v21_buy_decision
_paper._v21_buy_decision = _v21_buy_decision
_original_paper_score_predictive = _core._paper_score_predictive


def _paper_score_predictive(address, analysis, whale_state):
    s = _original_paper_score_predictive(address, analysis, whale_state)
    if not isinstance(s, dict): return s
    s = dict(s)
    pred = s.get("paper_prediction") if isinstance(s.get("paper_prediction"), dict) else None
    score = float(s.get("buy_score", s.get("confidence") or 0) or 0)
    decision = _v21_buy_decision(score, s, analysis, pred)
    s["buy_score"] = round(score, 1); s["market_score"] = round(score, 1); s["v21_adjustment"] = 0.0
    s["technical_bull"] = decision["technical_bull"]; s["technical_bear"] = decision["technical_bear"]; s["technical_evidence"] = decision["technical_evidence"]
    s["paper_near_threshold_buy"] = False; s["paper_near_threshold_reason"] = ""; s["max_win_profile"] = True; s["max_win_threshold"] = MAX_WIN_BUY_THRESHOLD
    s["reversal_buy"] = bool(decision.get("reversal_buy")); s["reversal_profile"] = decision.get("reversal_profile", "")
    if pred is not None:
        p5 = float(pred.get("p5") or 0); p10 = float(pred.get("p10") or 0); mean_roi = float(pred.get("mean_roi") or 0)
        s["paper_prediction_role"] = "hard_entry_filter" if not decision.get("reversal_buy") else "reversal_entry_filter"
        s["paper_prediction_summary"] = f"P(+5) {p5:.0%} • P(+10) {p10:.0%} • mean {mean_roi:+.1f}%"
    s["paper_buy_blocked"] = not decision["allowed"]; s["paper_buy_block_reason"] = decision["reason"]
    s["confidence"] = score if decision["allowed"] else min(score, MAX_WIN_BUY_THRESHOLD - 1.0)
    try:
        import v23_predictor
        s = v23_predictor.enrich(s, analysis)
    except Exception as exc:
        s["v23_prediction"] = {"ready": False, "error": str(exc)}
        s["v23_p5"] = s["v23_p10"] = s["v23_p20"] = s["v23_p30"] = 0.0; s["v23_mean_roi"] = 0.0; s["v23_pump_score"] = 0.0
    try:
        import v24_predictor
        state = _paper.load(_paper.POSITIONS_FILE, {"positions": {}, "closed_trades": []})
        closed = state.get("closed_trades", []) if isinstance(state, dict) else []
        s = v24_predictor.enrich(s, s, closed)
        v = s.get("v24") or {}
        s["v24_p5_mfe"] = v.get("p5_mfe", 0.0); s["v24_p10_mfe"] = v.get("p10_mfe", 0.0); s["v24_p20_mfe"] = v.get("p20_mfe", 0.0); s["v24_p30_mfe"] = v.get("p30_mfe", 0.0)
        s["v24_expected_mfe"] = v.get("expected_mfe", 0.0); s["v24_expected_mae"] = v.get("expected_mae", 0.0); s["v24_pump_quality"] = v.get("pump_quality", 0.0)
    except Exception as exc:
        s["v24"] = {"version": "V24", "ready": False, "error": str(exc)}
    try:
        import v25_learner
        s = v25_learner.enrich(s, s, closed)
    except Exception as exc:
        s["v25"] = {"version": "V25", "ready": False, "error": str(exc), "learning_mode": "shadow"}
    return s


_core._paper_score_predictive = _paper_score_predictive
_paper.score = _paper_score_predictive


def _max_win_risk_profile(pred, score):
    p10 = float((pred or {}).get("p10") or 0); p5 = float((pred or {}).get("p5") or 0)
    if p10 >= 0.60 and p5 >= 0.70: return {"sl": 0.065, "tp1": 0.060, "tp2": 0.150, "trail": 0.070, "name": "PUMP-MW"}
    if p10 >= 0.40 and p5 >= 0.62: return {"sl": 0.050, "tp1": 0.050, "tp2": 0.110, "trail": 0.055, "name": "STRONG-MW"}
    return {"sl": 0.040, "tp1": 0.045, "tp2": 0.090, "trail": 0.050, "name": "NORMAL-MW"}

_core._risk_profile = _max_win_risk_profile


def _adaptive_create(a, an, s, meta, liq, investment=None):
    z = _core._paper_original_create(a, an, s, meta, liq, investment)
    pred = s.get("paper_prediction") if isinstance(s, dict) else None
    profile = _max_win_risk_profile(pred or {}, float(s.get("confidence") or 0)); e = float(z.get("entry_price") or 0)
    if e > 0:
        z["sl_pct"] = profile["sl"]; z["tp1_pct"] = profile["tp1"]; z["tp2_pct"] = profile["tp2"]; z["trail_pct"] = profile["trail"]
        z["risk_profile"] = profile["name"]; z["sl"] = e * (1.0 - profile["sl"]); z["initial_sl"] = z["sl"]; z["tp1"] = e * (1.0 + profile["tp1"]); z["tp2"] = e * (1.0 + profile["tp2"])
        z["paper_prediction"] = pred or {}; z["max_win_profile"] = True
        try:
            import v24_tracking; v24_tracking.initialize(z)
        except Exception: pass
    return z

_core._adaptive_create = _adaptive_create; _paper.create = _adaptive_create; engine.create = _adaptive_create


def paper_decision(address, analysis, whale_state):
    s = _paper_score_predictive(address, analysis, whale_state)
    if not isinstance(s, dict): return {"score": None, "blocked": True, "reason": "paper scorer returned no data"}
    return {"score": s.get("buy_score", s.get("confidence")), "blocked": bool(s.get("paper_buy_blocked")), "reason": str(s.get("paper_buy_block_reason") or ""), "prediction": s.get("paper_prediction") or {}, "prediction_blocked": bool(s.get("paper_prediction_blocked")), "prediction_role": str(s.get("paper_prediction_role") or "hard_entry_filter"), "prediction_veto_reason": str(s.get("paper_prediction_veto_reason") or ""), "prediction_text": str(s.get("paper_prediction_text") or ""), "technical_bull": int(s.get("technical_bull") or 0), "technical_bear": int(s.get("technical_bear") or 0), "technical_evidence": list(s.get("technical_evidence") or []), "components": s.get("buy_score_components") or {}, "raw_confidence": s.get("paper_raw_confidence"), "near_threshold": False, "near_threshold_reason": "", "score_band": str(s.get("buy_score_band") or "MAX-WIN"), "data": s, "v23": s.get("v23_prediction") or {}, "v23_p5": s.get("v23_p5", 0.0), "v23_p10": s.get("v23_p10", 0.0), "v23_p20": s.get("v23_p20", 0.0), "v23_p30": s.get("v23_p30", 0.0), "v23_mean_roi": s.get("v23_mean_roi", 0.0), "v23_pump_score": s.get("v23_pump_score", 0.0), "v24": s.get("v24") or {}, "v25": s.get("v25") or {}, "reversal_buy": bool(s.get("reversal_buy")), "reversal_profile": str(s.get("reversal_profile") or "")}


def _run_v24_tracking():
    try:
        import v24_tracking; return v24_tracking.update_open_positions(_paper)
    except Exception: return False


def _run_v25_learning():
    try:
        import v25_learner
        state = _paper.load(_paper.POSITIONS_FILE, {"positions": {}, "closed_trades": []})
        closed = state.get("closed_trades", []) if isinstance(state, dict) else []
        return v25_learner.learn(closed)
    except Exception: return False


_original_engine_main = engine.main

def _engine_main_with_v24_tracking(*args, **kwargs):
    _run_v24_tracking()
    _run_v25_learning()
    return _original_engine_main(*args, **kwargs)

engine.main = _engine_main_with_v24_tracking
_core.engine.main = _engine_main_with_v24_tracking

if __name__ == "__main__": engine.main()
