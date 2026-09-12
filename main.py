"""Compatibility wrapper for the SDA scanner.

The implementation lives in main_core.py. This module keeps the public
entrypoint name `main.py`, applies the V21 borderline BUY flow bridge, and
exposes one canonical Paper decision helper for dashboard diagnostics.
"""

import main_core as _core

for _name, _value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def _strong_flow_bridge(s, analysis, pred=None):
    """Allow only exceptional borderline setups while predictor is warming up.

    A ready predictor is authoritative. This prevents the old bridge from
    promoting a 58-59 setup after the predictive layer had already produced a
    negative expectation.
    """
    try:
        if isinstance(pred, dict) and pred.get("ready"):
            return False
        raw = float(s.get("paper_raw_confidence", s.get("confidence", 0)) or 0)
        if not 58.0 <= raw < float(getattr(_paper, "BUY_THRESHOLD", 60) or 60):
            return False
        from strategy_v21 import technical_confirmation
        tc = technical_confirmation(analysis)
        bull = int(tc.get("bull") or 0)
        bear = int(tc.get("bear") or 0)
        evidence = list(tc.get("evidence") or [])
        m1 = float(s.get("m1h") or 0)
        m4 = float(s.get("m4h") or 0)
        m15 = float(s.get("m15") or 0)
        flow = float(s.get("net_1h") or 0)
        trades = float(s.get("trades_1h") or 0)
        f1 = analysis.get("flow", {}).get("1h", {}) if isinstance(analysis, dict) else {}
        bv = _metric_number(f1, "buy_volume")
        sv = _metric_number(f1, "sell_volume")
        bc = _metric_number(f1, "buy_count")
        sc = _metric_number(f1, "sell_count")
        volume_ratio = bv / max(sv, 1.0)
        trade_ratio = bc / max(sc, 1.0)
        return (
            m1 >= 5.0 and m4 >= 5.0 and m15 >= -0.50 and flow > 0
            and trades >= 10 and float(f1.get("total_volume") or 0) >= 1000.0
            and (volume_ratio >= 1.25 or trade_ratio >= 1.50)
            and bear == 0
            and not any("MACD bearish" in x for x in evidence)
            and not any("near resistance" in x for x in evidence)
            and (bull >= 1 or m1 >= 10.0)
        )
    except Exception:
        return False


def _v21_buy_decision(buy_score, s, analysis, pred):
    """Single BUY decision shared by the paper engine and V21 diagnostics."""
    threshold = float(getattr(_paper, "BUY_THRESHOLD", 60) or 60)
    score = float(buy_score or 0)
    try:
        from strategy_v21 import technical_confirmation
        tc = technical_confirmation(analysis)
    except Exception:
        tc = {"bull": 0, "bear": 0, "evidence": []}
    bull = int(tc.get("bull") or 0)
    bear = int(tc.get("bear") or 0)
    evidence = list(tc.get("evidence") or [])
    adjustment = min(8.0, bull * 1.5) - min(10.0, bear * 1.8)
    adjusted = _clamp100(score + adjustment)
    bridge = _strong_flow_bridge(s, analysis, pred)
    technical_ok = (not bool(analysis.get("technical"))) or bull >= 2
    allowed = adjusted >= threshold and technical_ok
    if bridge:
        allowed = True
        adjusted = max(adjusted, threshold + 1.0)
    reasons = []
    if not technical_ok and not bridge:
        reasons.append(f"technical confirmation {bull} bull / {bear} bear")
    if adjusted < threshold and not bridge:
        reasons.append(f"BUY score {adjusted:.0f} < {threshold:.0f}")
    if bridge:
        reasons.append("V21 exceptional 58-59 flow bridge (predictor warming up)")
    return {
        "allowed": allowed, "score": adjusted, "raw_score": score,
        "adjustment": adjustment, "technical_bull": bull,
        "technical_bear": bear, "technical_evidence": evidence,
        "bridge": bridge, "flow_bridge": bridge, "reason": "; ".join(reasons),
    }


_core._v21_buy_decision = _v21_buy_decision
_original_paper_score_predictive = _core._paper_score_predictive


def _paper_score_predictive(address, analysis, whale_state):
    s = _original_paper_score_predictive(address, analysis, whale_state)
    if not isinstance(s, dict):
        return s
    pred = s.get("paper_prediction") if isinstance(s.get("paper_prediction"), dict) else None
    if _strong_flow_bridge(s, analysis, pred):
        threshold = float(getattr(_paper, "BUY_THRESHOLD", 60) or 60)
        s = dict(s)
        promoted = max(float(s.get("buy_score") or s.get("market_score") or 0), threshold + 1.0)
        s["buy_score"] = round(promoted, 1)
        s["market_score"] = round(promoted, 1)
        s["confidence"] = promoted
        s["paper_buy_blocked"] = False
        s["paper_buy_block_reason"] = "V21 exceptional 58-59 flow bridge (predictor warming up)"
        s["paper_prediction_blocked"] = False
        s["paper_near_threshold_buy"] = True
        s["paper_near_threshold_reason"] = "V21 exceptional 58-59 flow bridge (predictor warming up)"
    return s


_core._paper_score_predictive = _paper_score_predictive
_paper.score = _paper_score_predictive
_paper_score_predictive = _paper_score_predictive


def paper_decision(address, analysis, whale_state):
    """Return the exact canonical Paper BUY decision used by the scanner.

    Dashboard/debug code should call this helper instead of engine.score().
    It intentionally returns the complete scorer dictionary so prediction,
    vetoes, technical confirmation and the final score remain visible.
    """
    s = _paper_score_predictive(address, analysis, whale_state)
    if not isinstance(s, dict):
        return {"score": None, "blocked": True, "reason": "paper scorer returned no data"}
    return {
        "score": s.get("buy_score", s.get("confidence")),
        "blocked": bool(s.get("paper_buy_blocked")),
        "reason": str(s.get("paper_buy_block_reason") or ""),
        "prediction": s.get("paper_prediction") or {},
        "prediction_blocked": bool(s.get("paper_prediction_blocked")),
        "prediction_text": str(s.get("paper_prediction_text") or ""),
        "technical_bull": int(s.get("technical_bull") or 0),
        "technical_bear": int(s.get("technical_bear") or 0),
        "technical_evidence": list(s.get("technical_evidence") or []),
        "components": s.get("buy_score_components") or {},
        "raw_confidence": s.get("paper_raw_confidence"),
        "near_threshold": bool(s.get("paper_near_threshold_buy")),
        "near_threshold_reason": str(s.get("paper_near_threshold_reason") or ""),
        "score_band": str(s.get("buy_score_band") or ""),
        "data": s,
    }


if __name__ == "__main__":
    _core.engine.main()
