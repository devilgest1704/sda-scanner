"""V28 Pump-Ignition paper strategy.

Designed from the observed V27 paper results: avoid late/weak entries, require
fresh re-acceleration and positive buy pressure, cut dead trades faster, and
give genuine pumps more room to run. Paper only.
"""
import json, math, os
from datetime import datetime, timezone, timedelta

STATE_FILE = "v26_pump_state.json"
SL_PCT = 0.045
MAX_OPEN = 5
MAX_BUYS_PER_RUN = 1
ENTRY_SCORE = 74.0
WATCH_SCORE = 48.0
ENTRY_CHANGE = 12.0
MIN_M15 = 0.35
MIN_TRADES = 12
MIN_VOLUME = 3000.0
MAX_VOL_ACCEL_DROP = -25.0
COOLDOWN_HOURS = 6.0
FRESH_MINUTES = 45.0

EARLY_SL_PCT = 0.025
EARLY_WEAK_COUNT = 2
STALE_HOURS = 3.0
STALE_MFE_PCT = 2.5
STALE_WEAK_COUNT = 4

def _num(v, d=0.0):
    try:
        return d if v is None else float(v)
    except Exception:
        return d

def _now():
    return datetime.now(timezone.utc)

def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}

def _save(x):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(x, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass

def _flow(a, w="1h"):
    x = (a.get("flow", {}) or {}).get(w, {}) if isinstance(a, dict) else {}
    return x if isinstance(x, dict) else {}

def _metrics(a):
    m = a.get("momentum", {}) or {}
    f1, f15 = _flow(a), _flow(a, "15m")
    bv, sv = _num(f1.get("buy_volume")), _num(f1.get("sell_volume"))
    bc, sc = _num(f1.get("buy_count")), _num(f1.get("sell_count"))
    return {
        "price": _num(a.get("price_in_sda")),
        "m1": _num(m.get("1h_pct")),
        "m15": _num(m.get("15m_pct")),
        "m4": _num(m.get("4h_pct")),
        "flow": _num(f1.get("net_flow")),
        "flow15": _num(f15.get("net_flow")),
        "vol": bv + sv,
        "trades": bc + sc,
        "buy_ratio": bv / max(sv, 1.0),
        "trade_ratio": bc / max(sc, 1.0),
        "vol_accel": _num(a.get("volume_acceleration_15m_pct")),
    }

def _fresh(a):
    raw = str(a.get("last_transaction") or "")
    if not raw:
        return False
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (_now() - t).total_seconds() <= FRESH_MINUTES * 60
    except Exception:
        return False

def _delta(c, p, k):
    return _num(c.get(k)) - _num(p.get(k)) if p else 0.0

def _cooldown_until(state, key):
    return str((state.get("cooldowns") or {}).get(key) or "")

def _cooldown_active(state, key):
    raw = _cooldown_until(state, key)
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) > _now()
    except Exception:
        return False

def _score(address, analysis):
    state = _load()
    hist = state.setdefault("history", {})
    key = str(address).lower()
    cur = _metrics(analysis)
    prev = hist.get(key, {}).get("last", {})
    old = hist.get(key, {})
    if prev and all(abs(_num(cur.get(k)) - _num(prev.get(k))) < 1e-12 for k in cur):
        return cur, _num(old.get("last_score")), _num(old.get("last_change")), str(old.get("phase") or "NO")

    accel = max(0, _delta(cur, prev, "m1")) * 2.0 + max(0, _delta(cur, prev, "m15")) * 1.5
    flow_imp = max(0, _delta(cur, prev, "flow")) / max(50, abs(_num(prev.get("flow"))) + 50) * 100
    vm = cur["vol"] / max(100, _num(prev.get("vol"), 100))
    tm = cur["trades"] / max(3, _num(prev.get("trades"), 3))

    pressure = (
        min(30, max(0, cur["m1"] * 1.5 + max(0, cur["m15"]) * .7))
        + min(22, max(0, (cur["buy_ratio"] - .9) * 22))
        + min(18, max(0, cur["flow"] / 500 * 6))
        + min(15, max(0, math.log(max(1, vm)) * 10 + math.log(max(1, tm)) * 5))
        + min(8, max(0, cur["vol_accel"] / 10))
        + min(7, max(0, cur["flow15"] / 300 * 7))
    )
    change = min(20, max(0, accel + flow_imp * .5 + max(0, vm - 1) * 5 + max(0, tm - 1) * 3))
    # Penalize entries that are already extended; V27's score bands showed that
    # simply taking the highest score was not enough.
    extension = min(12, max(0, (cur["m15"] - 6) * 1.2)) + min(10, max(0, (cur["m1"] - 18) * .45))
    score = max(0, min(100, pressure + change - extension))
    if cur["flow"] < 0:
        score -= 12
    if cur["buy_ratio"] < .8:
        score -= 10
    if cur["m1"] < 0 and cur["m15"] < 0:
        score -= 12
    score = max(0, min(100, score))

    entry = (
        prev and score >= ENTRY_SCORE and change >= ENTRY_CHANGE
        and cur["flow"] > 0 and cur["flow15"] > 0
        and cur["m1"] > .8 and cur["m15"] > MIN_M15 and cur["m4"] > -12
        and cur["buy_ratio"] >= 1.05 and cur["trade_ratio"] >= 1.0
        and cur["trades"] >= MIN_TRADES and cur["vol"] >= MIN_VOLUME
        and cur["vol_accel"] > MAX_VOL_ACCEL_DROP and _fresh(analysis)
    )
    phase = "ENTRY" if entry else ("WATCH" if score >= WATCH_SCORE else "NO")
    hist[key] = {
        "last": cur, "last_score": round(score, 2), "last_change": round(change, 2),
        "phase": phase, "updated_at": _now().isoformat()
    }
    state["updated_at"] = _now().isoformat()
    _save(state)
    return cur, round(score, 1), round(change, 1), phase

def decision(address, analysis, whale_state=None):
    cur, score, change, phase = _score(address, analysis)
    state, key = _load(), str(address).lower()
    cooldown = _cooldown_active(state, key)
    blocked = phase != "ENTRY" or cooldown
    if cooldown:
        reason = f"hard-stop cooldown active until {_cooldown_until(state, key)}"
    elif phase == "ENTRY":
        reason = "V28 PUMP-IGNITION: fresh acceleration + positive flow/activity gates passed"
    else:
        reason = f"pump phase {phase}; score {score:.0f}, acceleration +{change:.1f}"
    return {
        "score": score, "confidence": score, "buy_score": score, "market_score": score,
        "m1h": cur["m1"], "m15": cur["m15"], "m4h": cur["m4"],
        "net_1h": cur["flow"], "whale_net": cur["flow"],
        "trades_1h": cur["trades"], "volume_1h": cur["vol"],
        "buy_ratio": cur["buy_ratio"], "trade_ratio": cur["trade_ratio"],
        "eligible_for_buy": cur["trades"] > 0,
        "pump_score": score, "pump_change": change, "pump_phase": phase,
        "paper_buy_blocked": blocked, "paper_buy_block_reason": reason,
        "paper_prediction": {"ready": False, "role": "advisory"},
        "paper_prediction_role": "advisory",
        "technical_bull": 0, "technical_bear": 0, "technical_evidence": [],
        "buy_score_components": {
            "momentum": round(min(100, max(0, cur["m1"] * 5 + 50)), 1),
            "flow": round(min(100, max(0, 50 + cur["flow"] / 20)), 1),
            "activity": round(min(100, max(0, 40 + cur["trades"] * 2)), 1),
            "prediction": 0.0, "liquidity": 0.0
        },
        "v28": {
            "version": "V28-PUMP-IGNITION", "paper_only": True,
            "early_sl_pct": EARLY_SL_PCT, "stale_hours": STALE_HOURS,
            "fresh_minutes": FRESH_MINUTES,
            "trail_8": .07, "trail_15": .075, "trail_25": .09,
            "trail_40": .10, "trail_60": .12
        }
    }

def _position_age_hours(pos):
    raw = str(pos.get("opened_at") or pos.get("created_at") or pos.get("entry_time") or "")
    if not raw:
        return 0.0
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0, (_now() - t).total_seconds() / 3600)
    except Exception:
        return 0.0

def patch(main_module, engine_module):
    original_create = getattr(engine_module, "create", None)
    legacy = getattr(engine_module, "_legacy", None)
    targets = [engine_module] + ([legacy] if legacy is not None else [])

    def paper_decision(address, analysis, ws):
        d = decision(address, analysis, ws)
        return {**d, "data": d, "prediction": d["paper_prediction"],
                "blocked": d["paper_buy_blocked"], "reason": d["paper_buy_block_reason"],
                "score_band": "PUMP ENTRY" if not d["paper_buy_blocked"] else d["pump_phase"]}

    def score(address, analysis, ws):
        return decision(address, analysis, ws)

    def create(a, an, s, meta, liq, investment=None):
        z = original_create(a, an, s, meta, liq, investment) if callable(original_create) else {}
        e = _num(z.get("entry_price"), _num(an.get("price_in_sda")))
        z.update({
            "v28_mode": "PUMP-IGNITION", "pump_peak_price": e, "pump_mfe_pct": 0.0,
            "pump_weak_count": 0, "pump_age_scans": 0, "sl_pct": SL_PCT,
            "tp1_pct": 9.99, "tp2_pct": 9.99, "trail_pct": 0.0,
            "sl": e * (1 - SL_PCT), "initial_sl": e * (1 - SL_PCT),
            "tp1": e * 10.99, "tp2": e * 10.99,
            "risk_profile": "V28-PUMP-IGNITION",
            "entry_pump_score": _num(s.get("pump_score", s.get("confidence"))),
            "entry_pump_change": _num(s.get("pump_change")),
            "entry_phase": s.get("pump_phase", "ENTRY")
        })
        return z

    def auto_exit(p, tokens, ws):
        events = []
        for address in list((p.get("positions") or {}).keys()):
            pos = p["positions"].get(address)
            td = (tokens or {}).get(address, {}) or {}
            analysis = td.get("analysis", td) if isinstance(td, dict) else {}
            current = _num(analysis.get("price_in_sda"))
            if not pos or current <= 0:
                continue

            s = decision(address, analysis, ws)
            score = _num(s.get("pump_score"))
            entry = _num(pos.get("entry_price"))
            roi = (current - entry) / entry * 100 if entry else 0
            peak = max(_num(pos.get("pump_peak_price"), entry), current)
            pos["pump_peak_price"] = peak
            pos["pump_mfe_pct"] = (peak - entry) / entry * 100 if entry else 0
            pos["pump_age_scans"] = int(_num(pos.get("pump_age_scans")) + 1)
            age_h = _position_age_hours(pos)

            if roi >= 8:
                pos["sl"] = max(_num(pos.get("sl")), peak * .93)
            if roi >= 15:
                pos["sl"] = max(_num(pos.get("sl")), peak * .925)
            if roi >= 25:
                pos["sl"] = max(_num(pos.get("sl")), peak * .91)
            if roi >= 40:
                pos["sl"] = max(_num(pos.get("sl")), peak * .90)
            if roi >= 60:
                pos["sl"] = max(_num(pos.get("sl")), peak * .88)

            weak = (s.get("m1h", 0) <= 0 and s.get("net_1h", 0) <= 0) or score < 45 or s.get("m15", 0) < 0
            pos["pump_weak_count"] = int(_num(pos.get("pump_weak_count")) + 1 if weak else max(0, _num(pos.get("pump_weak_count")) - 1))
            stop = _num(pos.get("sl"))

            if current <= stop:
                hard = roi <= -SL_PCT * 100
                reason = "V28 HARD STOP" if hard else "V28 TRAILING STOP"
                r = engine_module.close(p, address, current, reason)
                if r:
                    if hard:
                        state = _load()
                        state.setdefault("cooldowns", {})[str(address).lower()] = (_now() + timedelta(hours=COOLDOWN_HOURS)).isoformat()
                        _save(state)
                    events.append(f"🔴 V28 STOP {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | age {age_h:.1f}h | score {score:.0f}")
            elif roi <= -EARLY_SL_PCT * 100 and pos["pump_weak_count"] >= EARLY_WEAK_COUNT and (score < 45 or s.get("m1h", 0) <= 0 or s.get("m15", 0) < 0):
                r = engine_module.close(p, address, current, "V28 EARLY WEAKNESS")
                if r:
                    events.append(f"🟠 V28 EARLY EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | weak {pos['pump_weak_count']} | score {score:.0f}")
            elif age_h >= STALE_HOURS and pos["pump_mfe_pct"] < STALE_MFE_PCT and pos["pump_weak_count"] >= STALE_WEAK_COUNT and roi < 2:
                r = engine_module.close(p, address, current, "V28 STALE POSITION")
                if r:
                    events.append(f"⚪ V28 STALE EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | age {age_h:.1f}h | weak {pos['pump_weak_count']}")
            elif roi > 0 and pos["pump_weak_count"] >= 3:
                r = engine_module.close(p, address, current, "V28 PUMP BREAKDOWN")
                if r:
                    events.append(f"🟠 V28 PUMP EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | score {score:.0f}")
        return events

    main_module.paper_decision = paper_decision
    for target in targets:
        if getattr(target, "_v28_engine_patched", False):
            continue
        target.score = score
        target.create = create
        target.BUY_THRESHOLD = ENTRY_SCORE
        target.MAX_OPEN_POSITIONS = MAX_OPEN
        target.MAX_NEW_BUYS_PER_RUN = MAX_BUYS_PER_RUN
        target.SL_PCT = SL_PCT
        target._auto_exit = auto_exit
        target._v28_engine_patched = True
    if hasattr(main_module, "engine"):
        main_module.engine.score = score
        main_module.engine.create = create
        main_module.engine.BUY_THRESHOLD = ENTRY_SCORE
        main_module.engine.MAX_OPEN_POSITIONS = MAX_OPEN
        main_module.engine.MAX_NEW_BUYS_PER_RUN = MAX_BUYS_PER_RUN
        main_module.engine.SL_PCT = SL_PCT
        main_module.engine._auto_exit = auto_exit
        main_module.engine._v28_engine_patched = True
    main_module._v28_patched = True
