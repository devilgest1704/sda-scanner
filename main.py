"""Compatibility wrapper for the SDA scanner.

The implementation lives in main_core.py; this module keeps the public
entrypoint name `main.py` and applies the V21 borderline BUY flow bridge.
"""

import main_core as _core

# Preserve the historical public names from main.py.
for _name, _value in _core.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def _v21_buy_decision(buy_score, s, analysis, pred):
    """Single BUY decision shared by the paper engine and V21 diagnostics.

    Normal entry: score >= 60 and technical confirmation is acceptable.
    Borderline 58-59 entry: allow a strong, confirmed positive-flow setup.
    """
    threshold = float(getattr(_paper, "BUY_THRESHOLD", 60) or 60)
    score = float(buy_score or 0)
    tc = {}
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

    bridge = False
    flow_bridge = False
    if 58.0 <= score < threshold and isinstance(pred, dict) and pred.get("ready"):
        p5 = float(pred.get("p5") or 0)
        p10 = float(pred.get("p10") or 0)
        mean_roi = float(pred.get("mean_roi") or 0)
        m1 = float(s.get("m1h") or 0)
        m4 = float(s.get("m4h") or 0)
        m15 = float(s.get("m15") or 0)
        flow = float(s.get("net_1h") or 0)
        trades = float(s.get("trades_1h") or 0)
        f1 = (analysis.get("flow", {}).get("1h", {}) if isinstance(analysis, dict) else {})
        bv = _metric_number(f1, "buy_volume")
        sv = _metric_number(f1, "sell_volume")
        bc = _metric_number(f1, "buy_count")
        sc = _metric_number(f1, "sell_count")
        volume_ratio = bv / max(sv, 1.0)
        trade_ratio = bc / max(sc, 1.0)

        bridge = (
            p5 >= 0.55 and p10 >= 0.30 and mean_roi > 0
            and m1 >= 3.0 and m4 >= 3.0
            and flow > 0 and m15 >= -0.25
            and trades >= 10
            and (volume_ratio >= 1.5 or trade_ratio >= 1.5)
            and bull >= 2 and bear == 0
            and not any("MACD bearish" in x for x in evidence)
            and not any("near resistance" in x for x in evidence)
        )

        flow_bridge = (
            p5 >= 0.55 and p10 >= 0.30 and mean_roi > 0
            and m1 >= 2.0 and m4 >= 2.0
            and m15 >= -0.50 and flow > 0 and trades >= 10
            and (volume_ratio >= 2.0 or trade_ratio >= 2.0)
            and (float(s.get("whale_net") or 0) > 0 or float(s.get("whale_15m_net") or 0) > 0)
            and bull >= 2 and bear == 0
            and not any("MACD bearish" in x for x in evidence)
            and not any("near resistance" in x for x in evidence)
        )

    technical_ok = (not bool(analysis.get("technical"))) or bull >= 2
    allowed = adjusted >= threshold and technical_ok
    if bridge or flow_bridge:
        allowed = True

    reasons = []
    if not technical_ok and not (bridge or flow_bridge):
        reasons.append(f"technical confirmation {bull} bull / {bear} bear")
    if adjusted < threshold and not (bridge or flow_bridge):
        reasons.append(f"BUY score {adjusted:.0f} < {threshold:.0f}")
    if bridge:
        reasons.append("V21 exceptional 58-59 flow bridge")
    elif flow_bridge:
        reasons.append("V21 exceptional 58-59 whale-flow bridge")

    return {
        "allowed": allowed,
        "score": adjusted,
        "raw_score": score,
        "adjustment": adjustment,
        "technical_bull": bull,
        "technical_bear": bear,
        "technical_evidence": evidence,
        "bridge": bridge or flow_bridge,
        "flow_bridge": flow_bridge,
        "reason": "; ".join(reasons),
    }


_core._v21_buy_decision = _v21_buy_decision

# Preserve the original predictor and add one final bridge override. The
# original predictor can append generic prediction vetoes after the V21 bridge;
# those vetoes must not undo an already-confirmed exceptional 58-59 setup.
_original_paper_score_predictive = _core._paper_score_predictive


def _paper_score_predictive(address, analysis, whale_state):
    s = _original_paper_score_predictive(address, analysis, whale_state)
    if not isinstance(s, dict) or not s.get("paper_near_threshold_buy"):
        return s

    threshold = float(getattr(_paper, "BUY_THRESHOLD", 60) or 60)
    score = float(s.get("buy_score") or s.get("market_score") or 0)
    if score < 58.0:
        return s

    # Bridge already passed the predictive, flow and technical checks. Promote
    # the canonical score and clear only the secondary vetoes that would undo it.
    s = dict(s)
    promoted = max(score, threshold + 1.0)
    s["buy_score"] = round(promoted, 1)
    s["market_score"] = round(promoted, 1)
    s["confidence"] = promoted
    s["paper_buy_blocked"] = False
    s["paper_buy_block_reason"] = "V21 exceptional 58-59 flow bridge"
    s["paper_prediction_blocked"] = False
    s["paper_near_threshold_reason"] = "V21 exceptional 58-59 flow bridge"
    return s


_core._paper_score_predictive = _paper_score_predictive
_paper.score = _paper_score_predictive
_paper_score_predictive = _paper_score_predictive

if __name__ == "__main__":
    _core.engine.main()
