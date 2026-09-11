from copy import deepcopy
import inspect
import json
import os
import math

from buy_threshold_config import apply as _apply_buy_threshold
import main_legacy as _legacy
import engine_legacy as _paper

_apply_buy_threshold(_legacy.engine)
globals().update({k: v for k, v in _legacy.__dict__.items() if not k.startswith("__")})
_apply_buy_threshold(engine)

PAPER_BUY_GUARD_FILE = "paper_buy_guard_state.json"
PAPER_SL_COOLDOWN_SCANS = 4
PAPER_MIN_1H_MOMENTUM = 0.0
PAPER_MIN_1H_FLOW = 0.0
PAPER_MIN_15M_MOMENTUM = -0.75

_paper_original_score = _paper.score
_paper_original_main = _paper.main
_paper_guard_state = {}
_paper_guard_prepared = False


def _paper_guard_load():
    try:
        with open(PAPER_BUY_GUARD_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _paper_guard_save(x):
    tmp = PAPER_BUY_GUARD_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PAPER_BUY_GUARD_FILE)
    except Exception:
        pass


def _paper_guard_load_positions():
    try:
        with open(_paper.POSITIONS_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _paper_guard_prepare():
    global _paper_guard_state, _paper_guard_prepared
    if _paper_guard_prepared:
        return
    state = _paper_guard_load()
    positions = _paper_guard_load_positions()
    closed = positions.get("closed_trades", []) if isinstance(positions, dict) else []
    latest_sl = {}
    for tr in closed:
        if not isinstance(tr, dict):
            continue
        reason = str(tr.get("close_reason") or "").upper()
        if "SL" not in reason:
            continue
        address = str(tr.get("address") or "").lower()
        stamp = str(tr.get("closed_at") or "")
        if address and stamp > str(latest_sl.get(address) or ""):
            latest_sl[address] = stamp
    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    last_sl = state.get("last_sl", {}) if isinstance(state.get("last_sl"), dict) else {}
    for address, stamp in latest_sl.items():
        if stamp and stamp != str(last_sl.get(address) or ""):
            cooldowns[address] = PAPER_SL_COOLDOWN_SCANS
            last_sl[address] = stamp
    if state.get("prepared_at"):
        for address in list(cooldowns):
            try:
                cooldowns[address] = max(0, int(cooldowns[address]) - 1)
            except Exception:
                cooldowns[address] = 0
    _paper_guard_state = {"cooldowns": cooldowns, "last_sl": last_sl}
    _paper_guard_save({**_paper_guard_state, "prepared_at": _paper.now()})
    _paper_guard_prepared = True


def _paper_guard_finish():
    global _paper_guard_prepared
    if _paper_guard_prepared:
        _paper_guard_save({**_paper_guard_state, "prepared_at": _paper.now()})
    _paper_guard_prepared = False


def _paper_score_guarded(address, analysis, whale_state):
    s = _paper_original_score(address, analysis, whale_state)
    stack = inspect.stack()
    in_auto_exit = any(frame.function == "_auto_exit" for frame in stack)
    in_paper_main = any(frame.function == "main" and frame.frame.f_globals.get("__name__") == _paper.__name__ for frame in stack)
    if not in_paper_main or in_auto_exit:
        return s
    address = str(address).lower()
    cooldown = int((_paper_guard_state.get("cooldowns") or {}).get(address, 0) or 0)
    m1 = float(s.get("m1h") or 0)
    m15 = float(s.get("m15") or 0)
    flow1 = float(s.get("net_1h") or 0)
    reasons = []
    if cooldown > 0:
        reasons.append(f"SL cooldown {cooldown}")
    if m1 <= PAPER_MIN_1H_MOMENTUM:
        reasons.append("1h momentum <= 0")
    if flow1 <= PAPER_MIN_1H_FLOW:
        reasons.append("1h SDA flow <= 0")
    if m15 < PAPER_MIN_15M_MOMENTUM:
        reasons.append("15m momentum too weak")
    s = dict(s)
    s["paper_buy_blocked"] = bool(reasons)
    s["paper_buy_block_reason"] = "; ".join(reasons)
    s["paper_raw_confidence"] = s.get("confidence")
    if reasons:
        s["confidence"] = min(float(s.get("confidence") or 0), float(_paper.BUY_THRESHOLD) - 1.0)
    return s


_paper.score = _paper_score_guarded

PREDICTIVE_STATE_FILE = "paper_predictive_state.json"
PREDICTIVE_MIN_SAMPLES = 8
PREDICTIVE_K = 12
PREDICTIVE_MIN_P5 = 0.52
PREDICTIVE_MIN_P10 = 0.28
PREDICTIVE_MIN_EDGE = 0.10

RISK_PROFILES = (
    (0.60, 0.07, 0.08, 0.18, 0.90),
    (0.45, 0.06, 0.07, 0.14, 0.08),
    (0.00, 0.05, 0.06, 0.11, 0.06),
)


def _predictive_load_positions():
    try:
        with open(_paper.POSITIONS_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _setup_vector(metrics):
    if not isinstance(metrics, dict):
        return [0.0] * 8
    return [
        float(metrics.get("confidence") or 0),
        float(metrics.get("m15") or 0),
        float(metrics.get("m1h") or 0),
        float(metrics.get("m4h") or 0),
        float(metrics.get("net_1h") or 0) / 1000.0,
        float(metrics.get("whale_net") or 0) / 1000.0,
        float(metrics.get("whale_15m_net") or 0) / 1000.0,
        float(metrics.get("trades_1h") or 0),
    ]


def _distance(a, b):
    scales = [20.0, 10.0, 20.0, 20.0, 10.0, 10.0, 10.0, 10.0]
    return math.sqrt(sum(((x - y) / sc) ** 2 for x, y, sc in zip(a, b, scales)))


def _predict_setup(metrics):
    current = _setup_vector(metrics)
    p = _predictive_load_positions()
    closed = p.get("closed_trades", []) if isinstance(p, dict) else []
    samples = []
    for tr in closed:
        if not isinstance(tr, dict):
            continue
        em = tr.get("entry_metrics")
        if not isinstance(em, dict):
            continue
        roi = tr.get("closed_roi_pct")
        if roi is None:
            profit = tr.get("closed_profit_sda")
            inv = tr.get("investment_sda")
            roi = float(profit) / float(inv) * 100 if profit is not None and inv else None
        if roi is None:
            continue
        samples.append((_distance(current, _setup_vector(em)), float(roi)))
    if len(samples) < PREDICTIVE_MIN_SAMPLES:
        return {"ready": False, "samples": len(samples), "p5": 0.5, "p10": 0.2, "mean_roi": 0.0, "confidence": 0.0}
    samples.sort(key=lambda x: x[0])
    nearest = samples[:PREDICTIVE_K]
    weights = [1.0 / (0.20 + d) for d, _ in nearest]
    sw = sum(weights) or 1.0
    p5 = sum(w for w, (_, roi) in zip(weights, nearest) if roi >= 5.0) / sw
    p10 = sum(w for w, (_, roi) in zip(weights, nearest) if roi >= 10.0) / sw
    mean_roi = sum(w * roi for w, (_, roi) in zip(weights, nearest)) / sw
    confidence = min(1.0, len(samples) / 40.0) * min(1.0, len(nearest) / float(PREDICTIVE_K))
    return {"ready": True, "samples": len(samples), "neighbors": len(nearest), "p5": p5, "p10": p10, "mean_roi": mean_roi, "confidence": confidence}


def _predictive_gate(s):
    pred = _predict_setup(s)
    if not pred["ready"]:
        return pred, True, "predictor warming up"
    p5 = pred["p5"]
    p10 = pred["p10"]
    score = float(s.get("confidence") or 0)
    edge = p5 - (1.0 - p5)
    ok = p5 >= PREDICTIVE_MIN_P5 and p10 >= PREDICTIVE_MIN_P10 and edge >= PREDICTIVE_MIN_EDGE
    if score >= 85 and p5 >= 0.50 and pred["mean_roi"] > 0:
        ok = True
    return pred, ok, f"P(+5) {p5:.0%} • P(+10) {p10:.0%} • mean {pred['mean_roi']:+.1f}%"


def _risk_profile(pred, score):
    p10 = float(pred.get("p10") or 0)
    p5 = float(pred.get("p5") or 0)
    if p10 >= 0.60 and p5 >= 0.70:
        return {"sl": 0.07, "tp1": 0.08, "tp2": 0.18, "trail": 0.09, "name": "PUMP"}
    if p10 >= 0.45 and p5 >= 0.62:
        return {"sl": 0.06, "tp1": 0.07, "tp2": 0.14, "trail": 0.08, "name": "STRONG"}
    return {"sl": 0.05, "tp1": 0.06, "tp2": 0.11, "trail": 0.06, "name": "NORMAL"}


def _adaptive_create(a, an, s, meta, liq, investment=None):
    z = _paper_original_create(a, an, s, meta, liq, investment) if '_paper_original_create' in globals() else _paper.create(a, an, s, meta, liq, investment)
    pred = s.get("paper_prediction") if isinstance(s, dict) else None
    profile = _risk_profile(pred or {}, float(s.get("confidence") or 0))
    e = float(z.get("entry_price") or 0)
    if e > 0:
        z["sl_pct"] = profile["sl"]
        z["tp1_pct"] = profile["tp1"]
        z["tp2_pct"] = profile["tp2"]
        z["trail_pct"] = profile["trail"]
        z["risk_profile"] = profile["name"]
        z["sl"] = e * (1.0 - profile["sl"])
        z["initial_sl"] = z["sl"]
        z["tp1"] = e * (1.0 + profile["tp1"])
        z["tp2"] = e * (1.0 + profile["tp2"])
        z["paper_prediction"] = pred or {}
    return z

_paper_original_create = _paper.create


def _clamp100(x):
    return max(0.0, min(100.0, float(x)))


def _metric_number(d, *keys):
    if not isinstance(d, dict):
        return 0.0
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def _buy_score_v2(s, analysis, pred):
    m15 = float(s.get("m15") or 0)
    m1 = float(s.get("m1h") or 0)
    m4 = float(s.get("m4h") or 0)
    momentum_score = _clamp100(50.0 + 3.0 * m1 + 1.5 * m4 + 1.5 * m15)
    flow = float(s.get("net_1h") or 0)
    whale = float(s.get("whale_net") or 0)
    whale15 = float(s.get("whale_15m_net") or 0)
    flow_score = _clamp100(50.0 + 35.0 * math.tanh(flow / 5000.0) + 15.0 * math.tanh((whale + 0.5 * whale15) / 5000.0))
    trades = float(s.get("trades_1h") or 0)
    activity_score = _clamp100(25.0 + 3.75 * min(trades, 20.0))
    if pred.get("ready"):
        p5 = float(pred.get("p5") or 0)
        p10 = float(pred.get("p10") or 0)
        mean_roi = float(pred.get("mean_roi") or 0)
        prediction_score = _clamp100(100.0 * (0.55 * p5 + 0.45 * p10) + 2.0 * mean_roi)
    else:
        prediction_score = 50.0
    impact = _metric_number(analysis, "estimated_price_impact_pct", "price_impact_pct", "expected_impact_pct", "impact_pct")
    if impact <= 0:
        liq = _metric_number(analysis, "liquidity_sda", "liquidity", "liquidity_usd")
        liquidity_score = _clamp100(35.0 + min(65.0, math.log10(max(liq, 1.0)) * 18.0)) if liq > 0 else 55.0
    else:
        liquidity_score = _clamp100(100.0 - 12.5 * impact)
    final = (0.35 * momentum_score + 0.25 * flow_score + 0.15 * activity_score + 0.15 * prediction_score + 0.10 * liquidity_score)
    return _clamp100(final), {"momentum": round(momentum_score, 1), "flow": round(flow_score, 1), "activity": round(activity_score, 1), "prediction": round(prediction_score, 1), "liquidity": round(liquidity_score, 1), "impact_pct": round(impact, 3) if impact > 0 else None}


def _paper_score_predictive(address, analysis, whale_state):
    s = _paper_score_guarded(address, analysis, whale_state)
    if not s.get("paper_buy_blocked"):
        pred, ok, text = _predictive_gate(s)
        s = dict(s)
        buy_score, components = _buy_score_v2(s, analysis, pred)
        s["paper_raw_confidence"] = s.get("confidence")
        s["market_score"] = round(float(s.get("confidence") or 0), 1)
        s["buy_score_components"] = components
        s["paper_prediction"] = pred
        s["paper_prediction_text"] = text
        s["paper_prediction_blocked"] = not ok
        vetoes = []
        impact = components.get("impact_pct")
        if impact is not None and impact > 8.0:
            vetoes.append(f"price impact {impact:.1f}%")
        if pred.get("ready"):
            p5 = float(pred.get("p5") or 0)
            p10 = float(pred.get("p10") or 0)
            mean_roi = float(pred.get("mean_roi") or 0)
            if p5 < 0.50 or p10 < 0.20 or mean_roi <= -1.0:
                vetoes.append("prediction too weak")
            if 65.0 <= buy_score < 70.0 and not (p5 >= 0.62 and p10 >= 0.35 and mean_roi > 0):
                vetoes.append("score 65-69 needs strong prediction")
        # Canonical BUY threshold: 60. Scores below 60 are blocked unless the
        # V21 strategy overlay explicitly promotes a 58-59 exceptional setup.
        if buy_score < 60.0:
            vetoes.append(f"BUY score {buy_score:.0f} < 60")
        if not ok:
            vetoes.append(text)
        s["buy_score"] = round(buy_score, 1)
        s["buy_score_band"] = ("OPATRNÝ BUY" if buy_score < 75 else "BUY" if buy_score < 85 else "STRONG BUY" if buy_score < 93 else "PUMP BUY")
        s["paper_buy_blocked"] = bool(vetoes)
        s["paper_buy_block_reason"] = "; ".join(vetoes)
        if vetoes:
            s["confidence"] = min(buy_score, float(_paper.BUY_THRESHOLD) - 1.0)
        else:
            s["confidence"] = buy_score
    return s


_paper.score = _paper_score_predictive
_paper.create = _adaptive_create
engine.create = _adaptive_create


def _adaptive_prepare_positions():
    try:
        p = _paper.load(_paper.POSITIONS_FILE, {"positions": {}, "closed_trades": []})
        positions = p.get("positions", {}) if isinstance(p, dict) else {}
        md = _paper.load(_paper.MARKET_FILE, {"tokens": {}})
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        changed = False
        for address, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            an = tokens.get(str(address).lower(), {}) or {}
            if isinstance(an, dict) and isinstance(an.get("analysis"), dict):
                an = an["analysis"]
            current = float(an.get("price_in_sda") or 0)
            entry = float(pos.get("entry_price") or 0)
            if current <= 0 or entry <= 0:
                continue
            profile = {"sl": float(pos.get("sl_pct") or 0.05), "trail": float(pos.get("trail_pct") or 0.06)}
            tp1 = float(pos.get("tp1") or entry * 1.05)
            tp2 = float(pos.get("tp2") or entry * 1.10)
            if current >= tp1 and not pos.get("tp1_hit"):
                pos["tp1_hit"] = True
                pos["sl"] = max(float(pos.get("sl") or 0), entry * 1.005)
                changed = True
            if current >= tp2:
                trail = max(0.05, min(0.12, profile["trail"]))
                new_sl = current * (1.0 - trail)
                pos["sl"] = max(float(pos.get("sl") or 0), new_sl)
                pos["tp2"] = max(float(pos.get("tp2") or 0), current * 1.50)
                pos["trailing_active"] = True
                pos["trailing_peak_price"] = max(float(pos.get("trailing_peak_price") or 0), current)
                changed = True
            elif pos.get("trailing_active"):
                peak = max(float(pos.get("trailing_peak_price") or 0), current)
                trail = max(0.05, min(0.12, profile["trail"]))
                new_sl = peak * (1.0 - trail)
                if peak != pos.get("trailing_peak_price") or new_sl > float(pos.get("sl") or 0):
                    pos["trailing_peak_price"] = peak
                    pos["sl"] = max(float(pos.get("sl") or 0), new_sl)
                    changed = True
        if changed:
            _paper.save(_paper.POSITIONS_FILE, p)
    except Exception:
        pass


def _paper_main_guarded():
    _paper_guard_prepare()
    _adaptive_prepare_positions()
    try:
        return _paper_original_main()
    finally:
        _paper_guard_finish()


_paper.main = _paper_main_guarded
engine.main = _paper_main_guarded

_legacy_rebuild_fifo = _legacy._rebuild_fifo


def _rebuild_fifo(p, meta):
    wallet_sync = bool(isinstance(p, dict) and p.get("wallet_sync_at"))
    wallet_current = deepcopy(p.get("current", {})) if wallet_sync and isinstance(p.get("current"), dict) else None
    rebuilt = _legacy_rebuild_fifo(p, meta)
    if wallet_current is not None and isinstance(rebuilt, dict):
        rebuilt["current"] = wallet_current
        rebuilt["open_cost_sda"] = sum(float(x.get("cost_sda") or 0) for x in wallet_current.values() if isinstance(x, dict))
        rebuilt["open_pnl_sda"] = sum(float(x.get("unrealized_pnl_sda") or 0) for x in wallet_current.values() if isinstance(x, dict))
        rebuilt["open_unrealized_pnl_sda"] = rebuilt["open_pnl_sda"]
    return rebuilt


if __name__ == "__main__":
    engine.main()
