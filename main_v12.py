# SDA Scanner v12 — SDA accumulation mode
# Entry point wrapper over main.py. Real wallet remains READ ONLY.
import json
import os
import main as engine

STATE_FILE = "decision_state_v12.json"

# V12 parameters: fewer premature entries, stronger confirmation, slower exits.
engine.BUY_THRESHOLD = 75
engine.MIN_TRADES_1H = 3
engine.MAX_NEW_BUYS_PER_RUN = 1


def _n(v, d=0.0):
    try:
        return d if v is None else float(v)
    except Exception:
        return d


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _save_state(x):
    try:
        t = STATE_FILE + ".tmp"
        with open(t, "w", encoding="utf-8") as f:
            json.dump(x, f, indent=2, ensure_ascii=False)
        os.replace(t, STATE_FILE)
    except Exception:
        pass


def score_v12(addr, a, ws):
    # V11 scoring retained, but with cleaner normalization and stronger
    # preference for sustained trend/flow instead of one large print.
    m = a.get("momentum", {}) or {}
    f1 = engine.flow(a, "1h")
    f15 = engine.flow(a, "15m")
    m15 = _n(m.get("15m_pct")); m1 = _n(m.get("1h_pct")); m4 = _n(m.get("4h_pct"))
    bv = _n(f1.get("buy_volume")); sv = _n(f1.get("sell_volume"))
    bc = _n(f1.get("buy_count")); sc = _n(f1.get("sell_count"))
    tv = bv + sv; tt = bc + sc; net = bv - sv
    ratio = bv / max(tv, 1.0)

    w15 = engine.whale(addr, ws, "15m")
    w1 = engine.whale(addr, ws, "1h")
    wn = _n(w1.get("net_flow")); wn15 = _n(w15.get("net_flow"))
    wbc = _n(w1.get("buy_count")); wsc = _n(w1.get("sell_count"))
    wav = any(_n(w1.get(k)) != 0 for k in ("net_flow", "buy_volume", "sell_volume", "buy_count", "sell_count"))

    s = max(0, min(20, (m1 + 2) * 2))
    s += max(0, min(10, (m4 + 3) * 1.5))
    if tv > 0:
        s += max(0, min(22, 22 * ((ratio - .35) / .65)))
    s += 10 if tt >= 10 else (7 if tt >= 6 else (4 if tt >= 3 else 1 if tt >= 2 else 0))

    if m15 > 0 and m1 > 0:
        s += min(12, 6 + m15)
    elif m15 < -.5 and m1 > 3:
        s -= 6

    n15 = _n(f15.get("net_flow"))
    if n15 > 0:
        s += min(6, max(1, n15 / 100))
    elif n15 < 0:
        s -= min(5, max(1, abs(n15) / 100))

    va = a.get("volume_acceleration_15m_pct")
    if va is not None:
        s += 4 if _n(va) > 15 else (-2 if _n(va) < -20 else 0)

    # Volume is useful, but never enough by itself.
    s += max(0, min(7, tv / 15000 * 7))

    if wav:
        if wn > 0: s += min(14, wn / 250)
        elif wn < 0: s -= min(12, abs(wn) / 250)
        if wbc >= 2: s += 3
        if wsc >= 3 and wn < 0: s -= 3
        if wn15 > 0: s += 3
        elif wn15 < 0: s -= 3

    # Do not buy an already-extended move without fresh short-term support.
    if m1 >= 12 and m15 <= 0: s -= 8
    if m1 >= 18 and n15 <= 0: s -= 5
    if tt <= 2 and tv < 50: s -= 8

    return {
        "confidence": int(round(max(0, min(100, s)))),
        "m1h": m1, "m15": m15, "m4h": m4,
        "net_1h": net, "trades_1h": tt, "buy_ratio_1h": ratio,
        "whale_net": wn, "whale_15m_net": wn15,
        "whale_data_available": wav,
    }


engine.score = score_v12


def _primary_open_pnl(portfolio):
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    known = [x for x in cur.values() if x.get("unrealized_pnl_sda") is not None]
    cost = sum(_n(x.get("cost_sda")) for x in known if x.get("cost_sda") is not None)
    value = sum(_n(x.get("value_sda")) for x in known if x.get("value_sda") is not None)
    pnl = sum(_n(x.get("unrealized_pnl_sda")) for x in known)
    return cost, value, pnl, len(known)


def portfolio_history_v12(wallet, md, meta, ld, previous=None, wallet_obj=None):
    p = engine.portfolio_history(wallet, md, meta, ld, previous, wallet_obj)
    if not isinstance(p, dict):
        return p

    # Recalculate historical realized P/L conservatively from the matched
    # portions of completed sells. A partially unmatched sell is excluded in
    # full: missing acquisition history must never become fake profit.
    realized = 0.0; matched_sells = 0; excluded_sells = 0
    for tr in p.get("trades", []) or []:
        if str(tr.get("side", "")).upper() != "SELL":
            continue
        amount = _n(tr.get("amount")); matched = _n(tr.get("matched_amount"))
        if amount <= 0 or matched < amount * 0.99:
            excluded_sells += 1
            continue
        realized += _n(tr.get("matched_proceeds_sda")) - _n(tr.get("cost_basis_sda"))
        matched_sells += 1

    p["realized_pnl_sda"] = realized
    p["matched_sell_count"] = matched_sells
    p["excluded_unmatched_sell_count"] = excluded_sells
    cost, value, open_pnl, known_count = _primary_open_pnl(p)
    p["open_cost_sda"] = cost
    p["open_value_sda"] = value
    p["open_unrealized_pnl_sda"] = open_pnl
    p["known_open_positions"] = known_count
    p["known_total_pnl_sda"] = realized + open_pnl
    return p


engine.portfolio_history = portfolio_history_v12


def portfolio_recommendations_v12(portfolio, md, ws, meta, ld):
    out = []
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
    state = _load_state()
    next_state = {}

    for token, pf in cur.items():
        an = (tokens.get(token, {}) or {}).get("analysis", tokens.get(token, {}) or {})
        s = score_v12(token, an, ws) if isinstance(an, dict) and an else {
            "confidence": 0, "m1h": 0, "net_1h": 0, "trades_1h": 0, "whale_net": 0
        }
        pnl_raw = pf.get("unrealized_pnl_pct")
        cost = _n(pf.get("cost_sda"), 0); pnl = _n(pnl_raw, 0)
        c = s.get("confidence", 0); m1 = _n(s.get("m1h")); f = _n(s.get("net_1h"))

        negative = c < 35 and m1 < 0 and f < 0
        deep_negative = pnl <= -15 and m1 < 0 and f < 0
        profit_weakening = pnl >= 8 and (m1 < 0 or f < 0)
        positive = c >= 70 and m1 > 0 and f > 0

        old = state.get(token, {}) if isinstance(state.get(token, {}), dict) else {}
        neg_count = _n(old.get("negative_count"), 0)
        profit_count = _n(old.get("profit_weakening_count"), 0)
        neg_count = neg_count + 1 if (negative or deep_negative) else 0
        profit_count = profit_count + 1 if profit_weakening else 0
        next_state[token] = {
            "negative_count": int(min(5, neg_count)),
            "profit_weakening_count": int(min(5, profit_count)),
        }

        if pnl_raw is None or cost <= 0:
            action = "HOLD / NO COST BASIS"; reason = "cost basis unavailable"
        elif positive:
            action = "HOLD / TRAIL"; reason = "trend + positive SDA flow confirmed"
        elif profit_count >= 2:
            action = "PARTIAL SELL"; reason = "profit + weakening momentum/flow confirmed twice"
        elif neg_count >= 3:
            action = "SELL / EXIT"; reason = "trend + SDA flow deterioration confirmed 3 times"
        elif pnl <= -8 and m1 >= 0 and f >= 0:
            action = "HOLD / WATCH"; reason = "loss alone is not an exit; momentum/flow still positive"
        else:
            action = "HOLD / WATCH"; reason = "no confirmed exit condition"

        out.append({
            "token": token, "symbol": pf.get("symbol") or engine.lbl(token, meta),
            "action": action, "reason": reason, "pnl_pct": pnl,
            "pnl_sda": _n(pf.get("unrealized_pnl_sda")), "score": c,
            "m1h": m1, "flow_1h": f,
        })

    _save_state(next_state)
    order = {"SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2,
             "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4}
    return sorted(out, key=lambda x: (order.get(x["action"], 9), x["score"]))


engine.portfolio_recommendations = portfolio_recommendations_v12


def portfolio_message_v12(portfolio, recommendations):
    lines = ["📊 REAL PORTFOLIO / P&L — V12 SDA (READ-ONLY)", ""]
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    for token, pf in sorted(cur.items(), key=lambda x: x[1].get("symbol", x[0])):
        cost = pf.get("cost_sda")
        value = pf.get("value_sda")
        pct = pf.get("unrealized_pnl_pct")
        if cost is None or pct is None:
            pnl_text = "UNKNOWN (cost basis not detected)"
        else:
            pnl_text = f"{_n(pf.get('unrealized_pnl_sda')):+.2f} SDA ({_n(pct):+.2f}%)"
        value_text = f"{_n(value):.2f} SDA" if value is not None else "UNKNOWN"
        lines += [f"• {pf.get('symbol') or token[:10]}",
                  f"  Amount: {_n(pf.get('amount')):.8f} | Cost: {(_n(cost):.2f} SDA if cost is not None else 'UNKNOWN')}",
                  f"  Now: {value_text} | P/L: {pnl_text}",
                  f"  Avg cost: {_n(pf.get('avg_cost_sda')):.8f} SDA/token", ""]

    open_pnl = _n(portfolio.get("open_unrealized_pnl_sda"))
    realized = _n(portfolio.get("realized_pnl_sda"))
    total = _n(portfolio.get("known_total_pnl_sda"))
    lines += [f"📌 CURRENT OPEN P/L: {open_pnl:+.2f} SDA",
              f"📚 HISTORICAL MATCHED P/L: {realized:+.2f} SDA",
              f"🧮 KNOWN TOTAL P/L: {total:+.2f} SDA",
              f"   Matched sells: {int(_n(portfolio.get('matched_sell_count')))} | Excluded unmatched: {int(_n(portfolio.get('excluded_unmatched_sell_count')))}",
              f"Detected trades: {int(portfolio.get('buy_count', 0))} BUY / {int(portfolio.get('sell_count', 0))} SELL",
              "", "🧭 V12 POSITION RECOMMENDATIONS"]
    if not recommendations:
        lines.append("• No recommendation available.")
    for r in recommendations:
        icon = "🔴" if r["action"] == "SELL / EXIT" else ("🟠" if r["action"] == "PARTIAL SELL" else ("🟢" if r["action"] == "HOLD / TRAIL" else "🟡"))
        lines.append(f"• {icon} {r['symbol']}: {r['action']} | P/L {r['pnl_pct']:+.2f}% | score {r['score']}/100 | 1h {r['m1h']:+.2f}% | {r['reason']}")
    lines += ["", "🧠 V12 SDA ACCUMULATION MODE", "Primary P/L = current open positions.", "Loss alone ≠ SELL; exits require confirmed deterioration.", "Wallet remains READ-ONLY."]
    return "\n".join(lines)


engine.portfolio_message = portfolio_message_v12

if __name__ == "__main__":
    engine.main()
