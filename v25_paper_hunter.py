"""Adaptive PUMP-HUNTER paper portfolio.

This module is deliberately isolated from the MAX-WIN paper engine.  It uses
its own positions/state and can therefore learn and trade pump setups without
changing the existing MAX-WIN entry/exit rules.

The learner objective is not win-rate alone: it optimizes post-fee expected
profit while tracking MFE/MAE, acceleration, flow and time-to-thresholds.
"""
from __future__ import annotations
import json, math, os
from datetime import datetime, timezone, timedelta

STATE_FILE = "v25_learner_state.json"
STATE_KEY = "paper_hunter"
INVESTMENT_SDA = 50.0
FEE_RATE = 0.01
SLIPPAGE_RATE = 0.001
MAX_OPEN = 3
MAX_NEW_PER_SCAN = 1
MIN_SCORE = 60.0
MIN_VOLUME = 250.0
MIN_TRADES = 7.0
MIN_M1H = 3.0
MIN_M15 = 0.25
MIN_M4H = 0.0
MIN_FLOW = 0.0
MIN_ACCEL = 8.0
LEARNER_WARMUP = 20
MAX_HOLD_HOURS = 24


def _num(v, d=0.0):
    try:
        return d if v is None else float(v)
    except (TypeError, ValueError):
        return d


def _now():
    return datetime.now(timezone.utc)


def _iso(x):
    return x.astimezone(timezone.utc).isoformat()


def _empty():
    return {
        "version": "V25-PUMP-HUNTER",
        "positions": {},
        "closed_trades": [],
        "watch": {},
        "watch_completed": [],
        "events": [],
        "stats": {},
    }


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x.get(STATE_KEY, _empty()) if isinstance(x, dict) else _empty()
    except Exception:
        return _empty()


def _save(s):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            b = json.load(f)
        if not isinstance(b, dict):
            b = {}
    except Exception:
        b = {}
    b[STATE_KEY] = s
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _analysis(td):
    if not isinstance(td, dict):
        return {}
    x = td.get("analysis", td)
    return x if isinstance(x, dict) else {}


def volume_1h_sda(an, data=None):
    for src in (data, an):
        if not isinstance(src, dict):
            continue
        for key in ("volume_1h", "volume1h", "volume_1h_sda", "one_hour_volume_sda"):
            if src.get(key) is not None:
                v = _num(src.get(key))
                if v > 0:
                    return v
        aa = _analysis(src)
        for key in ("volume_1h", "volume1h", "volume_1h_sda", "one_hour_volume_sda"):
            if aa.get(key) is not None:
                v = _num(aa.get(key))
                if v > 0:
                    return v
        f = aa.get("flow", {}).get("1h", {}) if isinstance(aa, dict) else {}
        if isinstance(f, dict) and f.get("total_volume") is not None:
            return _num(f.get("total_volume"))
        if isinstance(f, dict) and (f.get("buy_volume") is not None or f.get("sell_volume") is not None):
            return _num(f.get("buy_volume")) + _num(f.get("sell_volume"))
    return 0.0


def _flow_data(an):
    f = an.get("flow", {}).get("1h", {}) if isinstance(an, dict) else {}
    return f if isinstance(f, dict) else {}


def _momentum_acceleration(an):
    explicit = _num(an.get("volume_acceleration_15m_pct"), 0.0) if isinstance(an, dict) else 0.0
    m = an.get("momentum", {}) if isinstance(an, dict) else {}
    if not isinstance(m, dict):
        m = {}
    m15 = _num(m.get("15m_pct"))
    m1 = _num(m.get("1h_pct"))
    # Positive acceleration means the short window is catching up to or
    # exceeding the 1H trend.  Explicit scanner acceleration wins when set.
    structural = m15 - (m1 / 4.0)
    return explicit if explicit != 0 else structural * 10.0


def _pump_features(scanner, address, an, whale):
    try:
        decision = scanner.paper_decision(address, an, whale)
    except Exception:
        decision = {}
    data = decision.get("data") or {}
    v25 = decision.get("v25") or data.get("v25") or {}
    f = _flow_data(an)
    m = an.get("momentum", {}) if isinstance(an, dict) else {}
    if not isinstance(m, dict):
        m = {}
    m15 = _num(data.get("m15"), _num(m.get("15m_pct")))
    m1 = _num(data.get("m1h"), _num(m.get("1h_pct")))
    m4 = _num(data.get("m4h"), _num(m.get("4h_pct")))
    flow = _num(data.get("net_1h"), _num(f.get("net_flow")))
    trades = _num(data.get("trades_1h"), _num(f.get("buy_count")) + _num(f.get("sell_count")))
    volume = volume_1h_sda(an, data)
    buy_volume = _num(f.get("buy_volume"))
    sell_volume = _num(f.get("sell_volume"))
    buy_count = _num(f.get("buy_count"))
    sell_count = _num(f.get("sell_count"))
    volume_ratio = buy_volume / max(sell_volume, 1.0)
    trade_ratio = buy_count / max(sell_count, 1.0)
    accel = _momentum_acceleration(an)
    whale_net = _num(data.get("whale_net"))
    whale15 = _num(data.get("whale_15m_net"))
    score = _num(decision.get("score"), _num(data.get("buy_score"), _num(data.get("confidence"))))
    p5 = _num(v25.get("p5")); p10 = _num(v25.get("p10")); p20 = _num(v25.get("p20")); p30 = _num(v25.get("p30"))
    ev = _num(v25.get("expected_pl_sda")); mfe = _num(v25.get("expected_mfe")); mae = _num(v25.get("expected_mae"))
    bear = int(_num(decision.get("technical_bear"), _num(data.get("technical_bear"))))

    # Independent pump score. It intentionally rewards acceleration and
    # pressure, not just an already-high generic market score.
    pump = 0.0
    pump += min(25.0, max(0.0, m1) * 2.5)
    pump += min(15.0, max(0.0, m15) * 3.0)
    pump += min(10.0, max(0.0, m4) * 1.0)
    pump += min(15.0, max(0.0, accel) * 0.75)
    pump += min(15.0, max(0.0, flow) / 100.0)
    pump += min(8.0, max(0.0, trades - 5.0) * 0.8)
    if volume_ratio >= 2.0:
        pump += min(6.0, (volume_ratio - 2.0) * 1.5)
    if trade_ratio >= 1.5:
        pump += min(4.0, (trade_ratio - 1.5) * 2.0)
    if whale_net > 0:
        pump += min(5.0, whale_net / 1000.0)
    if whale15 > 0:
        pump += min(3.0, whale15 / 1000.0)
    if p10 > 0:
        pump += min(8.0, p10 * 10.0)
    if p20 > 0:
        pump += min(5.0, p20 * 10.0)
    if bear > 0:
        pump -= min(15.0, bear * 6.0)
    pump = max(0.0, min(100.0, pump))

    return {
        "decision": decision,
        "score": score,
        "pump_score": round(pump, 2),
        "v25": v25,
        "volume": volume,
        "trades": trades,
        "bear": bear,
        "m1h": m1,
        "m15": m15,
        "m4h": m4,
        "flow": flow,
        "accel": accel,
        "volume_ratio": volume_ratio,
        "trade_ratio": trade_ratio,
        "whale_net": whale_net,
        "whale_15m": whale15,
        "ev": ev,
        "expected_mfe": mfe,
        "expected_mae": mae,
        "p5": p5,
        "p10": p10,
        "p20": p20,
        "p30": p30,
    }


def _entry_gate(c, learned_ready):
    r = []
    if c["pump_score"] < MIN_SCORE:
        r.append(f"pump score {c['pump_score']:.0f}<{MIN_SCORE:.0f}")
    if c["volume"] < MIN_VOLUME:
        r.append(f"volume {c['volume']:.0f}<{MIN_VOLUME:.0f}")
    if c["trades"] < MIN_TRADES:
        r.append(f"trades {c['trades']:.0f}<{MIN_TRADES:.0f}")
    if c["m1h"] < MIN_M1H:
        r.append(f"M1H {c['m1h']:+.1f}%<{MIN_M1H:+.1f}%")
    if c["m15"] < MIN_M15:
        r.append(f"M15 {c['m15']:+.1f}%<{MIN_M15:+.2f}%")
    if c["m4h"] < MIN_M4H:
        r.append(f"M4H {c['m4h']:+.1f}%<0")
    if c["flow"] <= MIN_FLOW:
        r.append(f"flow {c['flow']:+.0f}<=0")
    if c["bear"] >= 2:
        r.append(f"technical bear {c['bear']}>=2")
    # A fresh pump is allowed to enter before the generic learner has enough
    # samples. Once learning is mature, expected outcomes become a hard gate.
    if learned_ready:
        if c["ev"] <= 0 and c["p10"] < 0.20 and c["p20"] < 0.05:
            r.append(f"learner EV {c['ev']:+.2f}, P10 {c['p10']:.0%}")
        if c["expected_mae"] < -7.0 and c["expected_mfe"] < 8.0:
            r.append("learner downside too high")
    else:
        if c["accel"] < MIN_ACCEL and c["volume_ratio"] < 2.0 and c["trade_ratio"] < 1.5:
            r.append("pump acceleration/pressure not strong enough")
    return (not r), r


def _adaptive_trailing(row, c, price):
    peak = max(_num(row.get("peak_price")), price)
    row["peak_price"] = peak
    gain = (peak / _num(row.get("entry_price"), price) - 1.0) * 100.0
    # Trailing becomes tighter as the pump matures. It is intentionally wider
    # at the beginning so normal pump noise does not cut the trade prematurely.
    if gain >= 50:
        trail = 0.055
    elif gain >= 30:
        trail = 0.065
    elif gain >= 20:
        trail = 0.075
    elif gain >= 12:
        trail = 0.085
    else:
        trail = 0.10
    # Strong continuation gets a little more breathing room.
    if c.get("m1h", 0) >= 8 and c.get("flow", 0) > 0 and c.get("accel", 0) > 5:
        trail += 0.015
    return max(0.05, min(0.12, trail))


def _close(row, price, reason, now, fraction=1.0):
    e = _num(row.get("entry_price")); inv_total = _num(row.get("investment_sda"), INVESTMENT_SDA)
    frac = max(0.0, min(1.0, fraction)); inv = inv_total * frac
    proceeds = inv * (price / e) * (1.0 - SLIPPAGE_RATE) * (1.0 - FEE_RATE) if e > 0 else 0.0
    cost = inv * (1.0 + FEE_RATE)
    profit = proceeds - cost
    return {
        **row,
        "closed_at": _iso(now),
        "final_price": price,
        "closed_fraction": frac,
        "closed_profit_sda": round(profit, 6),
        "closed_roi_pct": round(profit / cost * 100.0 if cost else 0.0, 6),
        "close_reason": reason,
    }


def _track(row, price, now, c=None):
    e = _num(row.get("entry_price"), price)
    row["peak_price"] = max(_num(row.get("peak_price"), e), price)
    row["trough_price"] = min(_num(row.get("trough_price"), e), price)
    row["mfe_pct"] = (row["peak_price"] / e - 1.0) * 100.0 if e else 0.0
    row["mae_pct"] = (row["trough_price"] / e - 1.0) * 100.0 if e else 0.0
    row["last_price"] = price
    row["last_seen_at"] = _iso(now)
    hits = row.setdefault("threshold_times", {})
    for t in (5, 10, 20, 30, 50):
        if row["mfe_pct"] >= t and str(t) not in hits:
            hits[str(t)] = _iso(now)
    if c:
        row.update({
            "last_pump_score": c["pump_score"],
            "last_score": c["score"],
            "last_ev": c["ev"],
            "last_m1h": c["m1h"],
            "last_m15": c["m15"],
            "last_m4h": c["m4h"],
            "last_flow": c["flow"],
            "last_accel": c["accel"],
            "last_volume_1h": c["volume"],
            "last_p10": c["p10"],
            "last_p20": c["p20"],
        })


def _learned_ready(s):
    return len(s.get("closed_trades", [])) >= LEARNER_WARMUP or len(s.get("watch_completed", [])) >= LEARNER_WARMUP


def _exit_decision(row, c, price, now):
    entry = _num(row.get("entry_price")); peak = _num(row.get("peak_price"), entry)
    roi = (price / entry - 1.0) * 100.0 if entry else 0.0
    peak_roi = (peak / entry - 1.0) * 100.0 if entry else 0.0
    age = None
    try:
        age = now - datetime.fromisoformat(str(row.get("opened_at")).replace("Z", "+00:00"))
    except Exception:
        pass

    weak = (c["m1h"] < 0 and c["flow"] <= 0) or (c["m15"] < -1.5 and c["flow"] <= 0)
    severe = c["m1h"] < -5 and c["m15"] < -3 and c["flow"] < 0
    trail = _adaptive_trailing(row, c, price)
    trailing_price = peak * (1.0 - trail)

    # Protect capital early, but only after a meaningful pump move. This avoids
    # turning every small fluctuation into an exit.
    if roi <= -7.0 and severe:
        return "PUMP HARD STOP", 1.0
    if row.get("partial_1_done") and price <= trailing_price and peak_roi >= 12:
        return "PUMP TRAILING EXIT", 1.0
    if not row.get("partial_1_done") and roi >= 12 and weak:
        return "PUMP PARTIAL REVERSAL", 0.35
    if row.get("partial_1_done") and not row.get("partial_2_done") and roi >= 20 and weak:
        return "PUMP PARTIAL REVERSAL 2", 0.30
    if roi >= 10 and severe:
        return "PUMP REVERSAL EXIT", 1.0
    # Do not kill a healthy pump at six hours.  The 24h limit is only a safety
    # valve; active momentum/flow can continue indefinitely within that limit.
    if age is not None and age >= timedelta(hours=MAX_HOLD_HOURS) and (weak or roi <= 0):
        return "PUMP TIME EXIT", 1.0
    return None, 0.0


def update():
    try:
        import main as scanner
        pe = scanner._paper
        md = pe.load(pe.MARKET_FILE, {"tokens": {}})
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        whale = pe.load(pe.WHALE_FILE, {})
        meta = pe.load(pe.META_FILE, {})
        s = _load(); pos = s.setdefault("positions", {}); closed = s.setdefault("closed_trades", [])
        watch = s.setdefault("watch", {}); wc = s.setdefault("watch_completed", [])
        now = _now(); events = []
        learned = _learned_ready(s)

        # Manage live pump positions first.
        for a in list(pos):
            row = pos.get(a)
            an = _analysis(tokens.get(a, {})); price = _num(an.get("price_in_sda"))
            if not row or price <= 0:
                continue
            c = _pump_features(scanner, a, an, whale); _track(row, price, now, c)
            reason, fraction = _exit_decision(row, c, price, now)
            if reason:
                if fraction < 1.0:
                    part = _close(row, price, reason, now, fraction)
                    closed.append(part)
                    row["investment_sda"] = _num(row.get("investment_sda")) * (1.0 - fraction)
                    if not row.get("partial_1_done"):
                        row["partial_1_done"] = True
                    else:
                        row["partial_2_done"] = True
                    events.append(("PARTIAL", part))
                else:
                    closed.append(_close(row, price, reason, now, 1.0))
                    del pos[a]
                    events.append(("SELL", closed[-1]))

        core = pe.load(pe.POSITIONS_FILE, {"positions": {}})
        corepos = core.get("positions", {}) if isinstance(core, dict) else {}
        candidates = []
        for raw, td in tokens.items():
            a = str(raw).lower()
            if a in pos or a in corepos:
                continue
            an = _analysis(td); price = _num(an.get("price_in_sda"))
            if price <= 0:
                continue
            c = _pump_features(scanner, a, an, whale)
            allowed, reasons = _entry_gate(c, learned)
            if not allowed:
                # Keep near-miss observations so the learner can discover what
                # happens after a rejected signal.
                if c["pump_score"] >= 50 and c["volume"] >= 150 and c["trades"] >= 5:
                    w = watch.get(a)
                    if not w:
                        w = {
                            "version": "V25-PUMP-WATCH",
                            "address": a,
                            "symbol": str(scanner.lbl(a, meta) if hasattr(scanner, "lbl") else a),
                            "entry_at": _iso(now),
                            "entry_price": price,
                            "peak_price": price,
                            "trough_price": price,
                            "mfe_pct": 0.0,
                            "mae_pct": 0.0,
                            "threshold_times": {},
                            "entry_pump_score": c["pump_score"],
                            "entry_metrics": c,
                            "gate_reasons": reasons,
                        }
                        watch[a] = w
                    _track(w, price, now, c); w["gate_reasons"] = reasons
                continue
            # Rank for early entry. Learned expected MFE/EV are bonuses, not the
            # primary driver: we want the beginning of a pump, not its aftermath.
            rank = c["pump_score"] + min(12.0, max(0.0, c["expected_mfe"])) * 0.5 + min(8.0, max(0.0, c["p10"]) * 10.0)
            candidates.append((rank, a, an, c, price))

        candidates.sort(reverse=True, key=lambda x: x[0])
        slots = max(0, MAX_OPEN - len(pos))
        for rank, a, an, c, price in candidates[:min(slots, MAX_NEW_PER_SCAN)]:
            label = str(scanner.lbl(a, meta) if hasattr(scanner, "lbl") else a)
            pos[a] = {
                "version": "V25-PUMP-HUNTER",
                "address": a,
                "symbol": label,
                "opened_at": _iso(now),
                "entry_price": price * (1.0 + SLIPPAGE_RATE),
                "investment_sda": INVESTMENT_SDA,
                "peak_price": price * (1.0 + SLIPPAGE_RATE),
                "trough_price": price * (1.0 + SLIPPAGE_RATE),
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "threshold_times": {},
                "partial_1_done": False,
                "partial_2_done": False,
                "entry_rank": round(rank, 3),
                "entry_pump_score": c["pump_score"],
                "entry_score": c["score"],
                "entry_metrics": c,
                "v25_prediction": c["v25"],
                "entry_gate": "ADAPTIVE PUMP",
                "entry_learned": learned,
                "entry_reason": "pump momentum + flow + activity + learner",
            }
            watch.pop(a, None)
            events.append(("BUY", pos[a]))

        # Complete rejected WATCH observations after six hours. Unlike an actual
        # trade they do not affect paper P/L, but they do train the learner.
        for a, w in list(watch.items()):
            try:
                started = datetime.fromisoformat(str(w.get("entry_at")).replace("Z", "+00:00"))
            except Exception:
                started = None
            if started and now - started >= timedelta(hours=6):
                w["completed_at"] = _iso(now)
                p = _num(w.get("last_price"), _num(w.get("entry_price")))
                e = _num(w.get("entry_price"))
                w["final_price"] = p
                w["final_roi_pct"] = (p / e - 1.0) * 100.0 if e else 0.0
                wc.append(w); del watch[a]

        closed = closed[-1000:]; wc = wc[-2000:]
        wins = sum(1 for x in closed if _num(x.get("closed_profit_sda")) > 0)
        pnl = sum(_num(x.get("closed_profit_sda")) for x in closed)
        s.update({
            "version": "V25-PUMP-HUNTER",
            "updated_at": _iso(now),
            "positions": pos,
            "closed_trades": closed,
            "watch": watch,
            "watch_completed": wc,
            "events": events[-30:],
            "stats": {
                "closed": len(closed),
                "wins": wins,
                "losses": max(0, len(closed) - wins),
                "win_rate_pct": round(wins / len(closed) * 100.0, 2) if closed else 0.0,
                "realized_pnl_sda": round(pnl, 6),
                "learned": learned,
                "max_open": MAX_OPEN,
                "objective": "maximize_post_fee_pnl_and_captured_mfe",
            },
        })
        _save(s)
        return s
    except Exception as exc:
        s = _load(); s["last_error"] = str(exc); s["updated_at"] = _iso(_now()); _save(s); return s


def summary(state=None):
    s = state or _load(); pos = s.get("positions", {}); closed = s.get("closed_trades", [])
    return {
        "open": len(pos),
        "closed": len(closed),
        "watch": len(s.get("watch", {})),
        "watch_completed": len(s.get("watch_completed", [])),
        "realized_pnl_sda": sum(_num(x.get("closed_profit_sda")) for x in closed),
        "win_rate_pct": (sum(1 for x in closed if _num(x.get("closed_profit_sda")) > 0) / len(closed) * 100.0) if closed else 0.0,
    }
