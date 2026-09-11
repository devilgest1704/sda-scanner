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

# ---------------------------------------------------------------------------
# PAPER BUY QUALITY GUARD
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# PREDICTIVE PAPER BUY + ADAPTIVE RISK
# ---------------------------------------------------------------------------
# This is intentionally an online empirical predictor, not a black-box model.
# It learns only from completed paper trades and compares the current setup with
# historical entry setups. It therefore improves as the paper ledger grows.
PREDICTIVE_STATE_FILE = "paper_predictive_state.json"
PREDICTIVE_MIN_SAMPLES = 8
PREDICTIVE_K = 12
PREDICTIVE_MIN_P5 = 0.52
PREDICTIVE_MIN_P10 = 0.28
PREDICTIVE_MIN_EDGE = 0.10

# Risk profiles. Wider stops are earned by stronger predicted upside, not applied
# blindly to every trade.
RISK_PROFILES = (
    (0.60, 0.07, 0.08, 0.18, 0.90),  # strong pump candidate: SL 7%, TP1 8%, TP2 18%, trail 9%
    (0.45, 0.06, 0.07, 0.14, 0.08),  # strong: SL 6%, TP1 7%, TP2 14%, trail 8%
    (0.00, 0.05, 0.06, 0.11, 0.06),  # normal: SL 5%, TP1 6%, TP2 11%, trail 6%
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
    # Robust feature scales prevent SDA flow from dominating momentum.
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
        # Early in the experiment, retain the proven quality guard and don't
        # pretend a tiny sample is predictive.
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
    # Use the original engine's creation semantics first, then adapt only the
    # paper risk envelope. Real wallet code never calls this function.
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


# Capture the original create before replacing it.
_paper_original_create = _paper.create


def _paper_score_predictive(address, analysis, whale_state):
    s = _paper_score_guarded(address, analysis, whale_state)
    if not s.get("paper_buy_blocked"):
        pred, ok, text = _predictive_gate(s)
        s = dict(s)
        s["paper_prediction"] = pred
        s["paper_prediction_text"] = text
        s["paper_prediction_blocked"] = not ok
        if not ok:
            s["paper_buy_blocked"] = True
            s["paper_buy_block_reason"] = (str(s.get("paper_buy_block_reason") or "") + "; " + text).strip("; ")
            s["confidence"] = min(float(s.get("confidence") or 0), float(_paper.BUY_THRESHOLD) - 1.0)
    return s


_paper.score = _paper_score_predictive
_paper.create = _adaptive_create
engine.create = _adaptive_create


def _adaptive_prepare_positions():
    """Before each scan, ratchet stops upward after TP1 and enter trailing mode after TP2."""
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
                # Lock a small gain rather than allowing a winner to become a loss.
                pos["sl"] = max(float(pos.get("sl") or 0), entry * 1.005)
                changed = True
            if current >= tp2:
                # Do not hard-kill a pump at TP2. Move TP2 far away and let the
                # ratcheting trailing stop perform the exit on a later scan.
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

# Real-wallet statistics must keep the wallet-authoritative OPEN snapshot after rebuilding FIFO.
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
