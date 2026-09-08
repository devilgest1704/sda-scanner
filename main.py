# SDA Scanner — main entry point
# V15: robust portfolio accounting + readable REAL WALLET / PORTFOLIO output.
import json, os
import engine

from engine import *

STATE_FILE = "decision_state_v15.json"
_original_portfolio_history = engine.portfolio_history

engine.BUY_THRESHOLD = 75
engine.MIN_TRADES_1H = 3
engine.MAX_NEW_BUYS_PER_RUN = 1


def _n(v, d=0.0):
    try:
        return d if v is None else float(v)
    except Exception:
        return d


def _decimals(token, meta):
    x = (meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {})
    try:
        return int(x.get("decimals", 18))
    except Exception:
        return 18


def _normalize_trade_amount(tr, meta):
    raw = _n(tr.get("amount"), 0)
    vol = _n(tr.get("cost_sda") if str(tr.get("side")).upper() == "BUY" else tr.get("proceeds_sda"), 0)
    px = _n(tr.get("price_sda"), 0)
    expected = vol / px if vol > 0 and px > 0 else 0
    if expected > 0 and raw / expected > 1e6:
        return raw / (10 ** _decimals(tr.get("token"), meta))
    return raw


def _rebuild_fifo(p, meta):
    source = []
    for src in p.get("trades", []) or []:
        if not isinstance(src, dict):
            continue
        tr = dict(src)
        tr["amount"] = _normalize_trade_amount(tr, meta)
        if tr["amount"] > 0:
            source.append(tr)

    lots = {}
    realized = {}
    matched = 0
    excluded = 0
    rebuilt = []

    for tr in sorted(source, key=lambda x: (x.get("block_number", x.get("block", 0)), str(x.get("timestamp")), str(x.get("tx")))):
        token = str(tr.get("token", "")).lower()
        side = str(tr.get("side", "")).upper()
        if side == "BUY":
            lots.setdefault(token, []).append({"amount": tr["amount"], "cost_sda": _n(tr.get("cost_sda"))})
            rebuilt.append(tr)
            continue
        if side != "SELL":
            continue

        qty = tr["amount"]
        removed = 0.0
        q = lots.setdefault(token, [])
        while qty > 1e-12 and q:
            lot = q[0]
            take = min(qty, lot["amount"])
            unit = lot["cost_sda"] / lot["amount"] if lot["amount"] else 0.0
            removed += take * unit
            lot["amount"] -= take
            lot["cost_sda"] -= take * unit
            qty -= take
            if lot["amount"] <= 1e-12:
                q.pop(0)

        matched_amt = tr["amount"] - qty
        proceeds = _n(tr.get("proceeds_sda"))
        matched_proceeds = proceeds * (matched_amt / tr["amount"]) if tr["amount"] else 0.0
        tr["cost_basis_sda"] = removed
        tr["matched_amount"] = matched_amt
        tr["unmatched_amount"] = max(0.0, qty)
        tr["matched_proceeds_sda"] = matched_proceeds
        if tr["amount"] > 0 and matched_amt >= tr["amount"] * 0.99:
            realized[token] = realized.get(token, 0.0) + matched_proceeds - removed
            matched += 1
        else:
            excluded += 1
        rebuilt.append(tr)

    current = {}
    for token, q in lots.items():
        amount = sum(z["amount"] for z in q)
        cost = sum(z["cost_sda"] for z in q)
        if amount <= 1e-12:
            continue
        buys = [x for x in source if str(x.get("token", "")).lower() == token and str(x.get("side")).upper() == "BUY"]
        current[token] = {
            "amount": amount, "cost_sda": cost, "avg_cost_sda": cost / amount if amount else None,
            "lots": q, "first_buy_at": buys[0].get("timestamp", "") if buys else "",
            "last_buy_at": buys[-1].get("timestamp", "") if buys else "",
            "age_days": None, "symbol": buys[-1].get("symbol", token[:10] + "...") if buys else token[:10] + "...",
        }

    p["trades"] = rebuilt[-500:]
    p["current"] = current
    p["realized_pnl_sda"] = sum(realized.values())
    p["matched_sell_count"] = matched
    p["excluded_unmatched_sell_count"] = excluded
    return p


def portfolio_history_v15(wallet, md, meta, ld, previous=None, wallet_obj=None):
    p = _original_portfolio_history(wallet, md, meta, ld, previous, wallet_obj)
    if not isinstance(p, dict):
        return p

    p = _rebuild_fifo(p, meta)
    w = wallet_obj if isinstance(wallet_obj, dict) else engine.wallet_snapshot(md, meta)
    holdings = {str(h.get("address", "")).lower(): h for h in w.get("holdings", []) if h.get("address")}

    for token, cur in p.get("current", {}).items():
        h = holdings.get(token)
        if not h:
            continue
        cur["amount"] = _n(h.get("amount"), cur.get("amount", 0))
        cur["symbol"] = h.get("symbol") or cur.get("symbol")
        cur["price_sda"] = _n(h.get("price_sda"), 0)
        valid_price = cur["price_sda"] > 0 and h.get("value_sda") is not None
        cur["value_sda"] = _n(h.get("value_sda")) if valid_price else None
        if valid_price:
            cur["unrealized_pnl_sda"] = cur["value_sda"] - _n(cur.get("cost_sda"))
            cur["unrealized_pnl_pct"] = (cur["unrealized_pnl_sda"] / _n(cur.get("cost_sda")) * 100) if _n(cur.get("cost_sda")) else None
            cur["cost_basis_status"] = "DETECTED"
        else:
            cur["unrealized_pnl_sda"] = None
            cur["unrealized_pnl_pct"] = None
            cur["cost_basis_status"] = "PRICE_UNKNOWN"

    for token, h in holdings.items():
        if token in p.get("current", {}):
            continue
        px = _n(h.get("price_sda"), 0)
        valid_price = px > 0 and h.get("value_sda") is not None
        p.setdefault("current", {})[token] = {
            "amount": _n(h.get("amount")), "cost_sda": None, "avg_cost_sda": None, "lots": [],
            "first_buy_at": "", "last_buy_at": "", "age_days": None, "symbol": h.get("symbol"),
            "price_sda": px, "value_sda": _n(h.get("value_sda")) if valid_price else None,
            "unrealized_pnl_sda": None, "unrealized_pnl_pct": None,
            "cost_basis_status": "UNKNOWN" if valid_price else "PRICE_UNKNOWN",
        }

    known = [x for x in p.get("current", {}).values() if x.get("unrealized_pnl_sda") is not None]
    p["open_cost_sda"] = sum(_n(x.get("cost_sda")) for x in known)
    p["open_value_sda"] = sum(_n(x.get("value_sda")) for x in known)
    p["open_unrealized_pnl_sda"] = sum(_n(x.get("unrealized_pnl_sda")) for x in known)
    p["known_open_positions"] = len(known)
    p["known_total_pnl_sda"] = _n(p.get("realized_pnl_sda")) + _n(p.get("open_unrealized_pnl_sda"))
    return p


engine.portfolio_history = portfolio_history_v15


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
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


def portfolio_recommendations_v15(portfolio, md, ws, meta, ld):
    out = []
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
    state = _load_state()
    nxt = {}

    for token, pf in cur.items():
        an = (tokens.get(token, {}) or {}).get("analysis", tokens.get(token, {}) or {})
        s = engine.score(token, an, ws) if isinstance(an, dict) and an else {"confidence": 0, "m1h": 0, "net_1h": 0}
        c = _n(s.get("confidence")); m1 = _n(s.get("m1h")); f = _n(s.get("net_1h"))
        pnl_raw = pf.get("unrealized_pnl_pct")
        pnl = _n(pnl_raw)
        cost = pf.get("cost_sda")
        old = state.get(token, {}) if isinstance(state.get(token), dict) else {}
        neg = int(_n(old.get("negative_count"))); weak = int(_n(old.get("profit_weakening_count")))
        negative = c < 35 and m1 < 0 and f < 0
        deep = pnl <= -15 and m1 < 0 and f < 0
        weakening = pnl >= 8 and (m1 < 0 or f < 0)
        neg = min(5, neg + 1) if (negative or deep) else 0
        weak = min(5, weak + 1) if weakening else 0
        nxt[token] = {"negative_count": neg, "profit_weakening_count": weak}

        if cost is None or pnl_raw is None:
            action = "HOLD / NO COST BASIS"; reason = "cost basis or valid valuation unavailable"
        elif weak >= 2:
            action = "PARTIAL SELL"; reason = "profit + weakening confirmed twice"
        elif neg >= 3:
            action = "SELL / EXIT"; reason = "trend + negative SDA flow confirmed 3 times"
        elif c >= 70 and m1 > 0 and f > 0:
            action = "HOLD / TRAIL"; reason = "positive trend and SDA flow"
        else:
            action = "HOLD / WATCH"; reason = "no confirmed exit condition"

        out.append({"token": token, "symbol": pf.get("symbol") or engine.lbl(token, meta), "action": action,
                    "reason": reason, "pnl_pct": pnl, "pnl_sda": pf.get("unrealized_pnl_sda"),
                    "score": c, "m1h": m1, "flow_1h": f})

    _save_state(nxt)
    order = {"SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2, "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4}
    return sorted(out, key=lambda x: (order.get(x["action"], 9), -x["score"]))


engine.portfolio_recommendations = portfolio_recommendations_v15


def _meta_info(token, pf, meta):
    m = meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {}
    symbol = pf.get("symbol") or m.get("symbol") or str(token)[:10] + "..."
    name = m.get("name") or symbol
    return "🔹", symbol, name


def _fmt_amount(amount):
    x = _n(amount)
    if abs(x) >= 1000000: return f"{x:,.0f}"
    if abs(x) >= 1000: return f"{x:,.2f}"
    if abs(x) >= 1: return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:,.8f}".rstrip("0").rstrip(".")


def _pnl_text(pnl, pct):
    if pnl is None or pct is None:
        return "⚪ P/L UNKNOWN"
    icon = "🟢" if _n(pnl) > 0 else ("🔴" if _n(pnl) < 0 else "⚪")
    return f"{icon} P/L {_n(pnl):+.2f} SDA ({_n(pct):+.2f}%)"


def portfolio_message_v15(portfolio, recommendations):
    meta = engine.load(engine.META_FILE, {})
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    lines = ["📊 REAL PORTFOLIO", "", "Current token positions — SDA accumulation", "────────────────────────"]

    for token, pf in sorted(cur.items(), key=lambda x: (x[1].get("symbol") or x[0]).lower()):
        icon, symbol, name = _meta_info(token, pf, meta)
        val = pf.get("value_sda")
        value_text = f"{_n(val):.2f} SDA" if val is not None else "UNKNOWN"
        lines += [
            f"{icon} {symbol} — {name}",
            f"   {_fmt_amount(pf.get('amount'))} {symbol}  •  {value_text}",
            f"   {_pnl_text(pf.get('unrealized_pnl_sda'), pf.get('unrealized_pnl_pct'))}",
            "",
        ]

    op = portfolio.get("open_unrealized_pnl_sda")
    rp = portfolio.get("realized_pnl_sda")
    tp = portfolio.get("known_total_pnl_sda")
    lines += [
        "────────────────────────",
        _pnl_text(op, (op / _n(portfolio.get("open_cost_sda")) * 100) if _n(portfolio.get("open_cost_sda")) else None).replace("P/L", "Current open P/L"),
        _pnl_text(rp, 0).replace("P/L", "Historical matched P/L"),
        _pnl_text(tp, 0).replace("P/L", "Known total P/L"),
        f"Matched sells: {int(_n(portfolio.get('matched_sell_count')))}  •  Excluded unmatched: {int(_n(portfolio.get('excluded_unmatched_sell_count')))}",
        "", "🧭 POSITION ACTION"
    ]

    for r in recommendations:
        icon = "🔴" if r["action"] == "SELL / EXIT" else ("🟠" if r["action"] == "PARTIAL SELL" else ("🟢" if r["action"] == "HOLD / TRAIL" else "🟡"))
        p = r.get("pnl_sda")
        pt = f"{_n(p):+.2f} SDA" if p is not None else "UNKNOWN"
        lines.append(f"{icon} {r['symbol']}: {r['action']}  •  P/L {pt}  •  score {int(_n(r['score']))}/100")

    lines += ["", "ℹ️ Actual token logos are shown in the portfolio card below when available.",
              "ℹ️ Zero/invalid prices are UNKNOWN — never treated as a loss.", "🔒 Wallet is READ-ONLY."]
    return "\n".join(lines)


engine.portfolio_message = portfolio_message_v15


def wallet_message_v15(w):
    meta = engine.load(engine.META_FILE, {})
    hs = w.get("holdings") or []
    lines = ["👛 REAL WALLET", "", f"💰 SDA: {_n(w.get('native_sda')):.4f}", f"🪙 Token positions: {len(hs)}", "────────────────────────"]
    total_known = 0.0
    for h in sorted(hs, key=lambda x: (x.get("symbol") or "").lower()):
        token = str(h.get("address", "")).lower(); m = meta.get(token, {}) if isinstance(meta, dict) else {}
        symbol = h.get("symbol") or m.get("symbol") or token[:10] + "..."; name = m.get("name") or symbol
        val = h.get("value_sda"); px = _n(h.get("price_sda"))
        if val is not None: total_known += _n(val)
        value_text = f"{_n(val):.2f} SDA" if val is not None else "UNKNOWN"
        price_text = f"{price(px)} SDA" if px > 0 else "UNKNOWN"
        lines += [f"🔹 {symbol} — {name}", f"   {_fmt_amount(h.get('amount'))} {symbol}  •  {value_text}", f"   Price: {price_text}", ""]
    lines += ["────────────────────────", f"📊 Known token value: {total_known:.2f} SDA"]
    if w.get("native_sda") is not None and w.get("total_value_sda") is not None:
        lines.append(f"💼 TOTAL WALLET VALUE: {_n(w.get('total_value_sda')):.2f} SDA")
    if w.get("holding_source"): lines.append(f"Source: {w['holding_source']}")
    if w.get("error"): lines += ["", f"⚠️ {w['error'][:900]}"]
    return "\n".join(lines)


engine.wallet_message = wallet_message_v15

if __name__ == "__main__":
    engine.main()
