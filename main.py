# SDA Scanner V17 — canonical main entry point
# Portfolio valuation/accounting is centralized here; engine remains the scanner core.
import json
import math
import os
import engine
from engine import *

STATE_FILE = "decision_state_v15.json"
_original_portfolio_history = engine.portfolio_history
_original_wallet_snapshot = engine.wallet_snapshot

engine.BUY_THRESHOLD = 75
engine.MIN_TRADES_1H = 3
engine.MAX_NEW_BUYS_PER_RUN = 5
engine.MAX_OPEN_POSITIONS = 10


def _n(v, default=0.0):
    try:
        x = default if v is None else float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _decimals(token, meta):
    m = meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {}
    try:
        d = int(m.get("decimals"))
        return d if 0 <= d <= 36 else None
    except Exception:
        return None


def _normalize_trade_amount(tr, meta):
    raw = _n(tr.get("amount"))
    if raw <= 0:
        return 0.0
    side = str(tr.get("side") or "").upper()
    volume = _n(tr.get("cost_sda") if side == "BUY" else tr.get("proceeds_sda"))
    price = _n(tr.get("price_sda"))
    expected = volume / price if volume > 0 and price > 0 else 0.0
    if expected > 0 and raw <= expected * 1000:
        return raw
    decimals = _decimals(tr.get("token"), meta)
    if decimals is None:
        return raw if expected <= 0 else 0.0
    return raw / (10 ** decimals)


def _valuation_status(h):
    if not isinstance(h, dict):
        return "UNKNOWN"
    px = _n(h.get("price_sda"))
    value = h.get("value_sda")
    amount = _n(h.get("amount"))
    if not (px > 0 and amount > 0 and value is not None):
        return "UNKNOWN"
    value = _n(value, -1.0)
    if value < 0:
        return "INVALID"
    symbol = str(h.get("symbol") or "").upper()
    if "USD" in symbol and not (0.01 <= px <= 10.0):
        return "INVALID"
    expected = amount * px
    if expected <= 0 or not math.isfinite(expected):
        return "UNKNOWN"
    ratio = value / expected
    return "VALID" if math.isfinite(ratio) and 0.80 <= ratio <= 1.20 else "INVALID"


def _sanitize_wallet(w):
    if not isinstance(w, dict):
        return {}
    known = 0.0
    for h in w.get("holdings", []) or []:
        status = _valuation_status(h)
        h["valuation_status"] = status
        if status != "VALID":
            h["price_sda"] = 0.0
            h["value_sda"] = None
        else:
            known += _n(h.get("value_sda"))
    w["total_token_value_sda"] = known
    if w.get("native_sda") is not None:
        w["total_value_sda"] = _n(w.get("native_sda")) + known
    return w


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
    realized = 0.0
    matched = excluded = 0
    rebuilt = []

    for tr in sorted(source, key=lambda x: (x.get("block_number", x.get("block", 0)), str(x.get("timestamp")), str(x.get("tx")))):
        token = str(tr.get("token", "")).lower()
        side = str(tr.get("side", "")).upper()
        if not token:
            continue
        if side == "BUY":
            lots.setdefault(token, []).append({"amount": tr["amount"], "cost_sda": max(0.0, _n(tr.get("cost_sda")))})
            rebuilt.append(tr)
            continue
        if side != "SELL":
            continue

        qty = tr["amount"]
        removed = 0.0
        queue = lots.setdefault(token, [])
        while qty > 1e-12 and queue:
            lot = queue[0]
            take = min(qty, lot["amount"])
            unit_cost = lot["cost_sda"] / lot["amount"] if lot["amount"] else 0.0
            removed += take * unit_cost
            lot["amount"] -= take
            lot["cost_sda"] = max(0.0, lot["cost_sda"] - take * unit_cost)
            qty -= take
            if lot["amount"] <= 1e-12:
                queue.pop(0)

        matched_amount = tr["amount"] - qty
        proceeds = max(0.0, _n(tr.get("proceeds_sda")))
        matched_proceeds = proceeds * (matched_amount / tr["amount"]) if tr["amount"] else 0.0
        tr.update({"cost_basis_sda": removed, "matched_amount": matched_amount, "unmatched_amount": max(0.0, qty), "matched_proceeds_sda": matched_proceeds})
        if matched_amount >= tr["amount"] * 0.99:
            realized += matched_proceeds - removed
            matched += 1
        else:
            excluded += 1
        rebuilt.append(tr)

    current = {}
    for token, queue in lots.items():
        amount = sum(x["amount"] for x in queue)
        cost = sum(x["cost_sda"] for x in queue)
        if amount <= 1e-12:
            continue
        buys = [x for x in source if str(x.get("token", "")).lower() == token and str(x.get("side")).upper() == "BUY"]
        current[token] = {
            "amount": amount,
            "cost_sda": cost,
            "avg_cost_sda": cost / amount,
            "lots": queue,
            "first_buy_at": buys[0].get("timestamp", "") if buys else "",
            "last_buy_at": buys[-1].get("timestamp", "") if buys else "",
            "age_days": None,
            "symbol": buys[-1].get("symbol", token[:10] + "...") if buys else token[:10] + "...",
        }

    p["trades"] = rebuilt[-500:]
    p["current"] = current
    p["realized_pnl_sda"] = realized
    p["matched_sell_count"] = matched
    p["excluded_unmatched_sell_count"] = excluded
    return p


def portfolio_history_v16(wallet, md, meta, ld, previous=None, wallet_obj=None):
    p = _original_portfolio_history(wallet, md, meta, ld, previous, wallet_obj)
    if not isinstance(p, dict):
        return p

    p = _rebuild_fifo(p, meta)
    w = wallet_obj if isinstance(wallet_obj, dict) else _original_wallet_snapshot(md, meta)
    w = _sanitize_wallet(w)
    holdings = {str(h.get("address", "")).lower(): h for h in w.get("holdings", []) if h.get("address")}

    for token, cur in p.get("current", {}).items():
        h = holdings.get(token)
        if not h:
            continue
        cur["amount"] = _n(h.get("amount"), cur.get("amount", 0.0))
        cur["symbol"] = h.get("symbol") or cur.get("symbol")
        if _valuation_status(h) == "VALID":
            cur["price_sda"] = _n(h.get("price_sda"))
            cur["value_sda"] = _n(h.get("value_sda"))
            cur["unrealized_pnl_sda"] = cur["value_sda"] - _n(cur.get("cost_sda"))
            cost = _n(cur.get("cost_sda"))
            cur["unrealized_pnl_pct"] = cur["unrealized_pnl_sda"] / cost * 100 if cost else None
            cur["cost_basis_status"] = "DETECTED"
        else:
            cur["price_sda"] = 0.0
            cur["value_sda"] = None
            cur["unrealized_pnl_sda"] = None
            cur["unrealized_pnl_pct"] = None
            cur["cost_basis_status"] = "PRICE_UNKNOWN"

    for token, h in holdings.items():
        if token in p.get("current", {}):
            continue
        valid = _valuation_status(h) == "VALID"
        p.setdefault("current", {})[token] = {
            "amount": _n(h.get("amount")), "cost_sda": None, "avg_cost_sda": None, "lots": [],
            "first_buy_at": "", "last_buy_at": "", "age_days": None,
            "symbol": h.get("symbol") or token[:10] + "...",
            "price_sda": _n(h.get("price_sda")) if valid else 0.0,
            "value_sda": _n(h.get("value_sda")) if valid else None,
            "unrealized_pnl_sda": None, "unrealized_pnl_pct": None,
            "cost_basis_status": "UNKNOWN" if valid else "PRICE_UNKNOWN",
        }

    known = [x for x in p.get("current", {}).values() if x.get("unrealized_pnl_sda") is not None]
    p["open_cost_sda"] = sum(_n(x.get("cost_sda")) for x in known)
    p["open_value_sda"] = sum(_n(x.get("value_sda")) for x in known)
    p["open_unrealized_pnl_sda"] = sum(_n(x.get("unrealized_pnl_sda")) for x in known)
    p["known_open_positions"] = len(known)
    p["known_total_pnl_sda"] = _n(p.get("realized_pnl_sda")) + p["open_unrealized_pnl_sda"]
    return p


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def portfolio_recommendations_v16(portfolio, md, ws, meta, ld):
    """V17: score is confirmation, not a standalone exit trigger.

    BUY execution remains unchanged: engine threshold 75, positive momentum/flow,
    trade-count and liquidity gates. Portfolio recommendations add stricter exits:
    profitable weakening positions can be partially reduced, while losing very-weak
    positions need confirmed deterioration before SELL / EXIT.
    """
    out = []
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
    state = _load_state()
    nxt = {}

    for token, pf in cur.items():
        an = (tokens.get(token, {}) or {}).get("analysis", tokens.get(token, {}) or {})
        s = engine.score(token, an, ws) if isinstance(an, dict) and an else {"confidence": 0, "m1h": 0, "net_1h": 0}
        score = _n(s.get("confidence")); m1h = _n(s.get("m1h")); flow = _n(s.get("net_1h"))
        pnl_raw = pf.get("unrealized_pnl_pct"); pnl = _n(pnl_raw)
        old = state.get(token, {}) if isinstance(state.get(token), dict) else {}
        neg = int(_n(old.get("negative_count")))
        weak = int(_n(old.get("profit_weakening_count")))
        loss_weak = int(_n(old.get("loss_weakening_count")))

        # Core deterioration: score is used together with both negative momentum and flow.
        negative = score < 35 and m1h < 0 and flow < 0
        deep = pnl <= -15 and m1h < 0 and flow < 0

        # Profitable position losing momentum/flow: candidate for scale-out, but only
        # after two consecutive scans. This prevents one noisy scan from selling.
        weakening = pnl > 0 and score < 40 and (m1h < 0 or flow < 0)

        # Losing position with a very weak score and deterioration: this is the new
        # V17 exit path. It still requires 3 confirmations and never triggers from
        # score alone. A larger loss relaxes the score threshold slightly.
        loss_weakening = pnl <= -8 and (score < 20 or (pnl <= -12 and score < 35)) and (m1h < 0 or flow < 0)

        neg = min(5, neg + 1) if (negative or deep) else 0
        weak = min(5, weak + 1) if weakening else 0
        loss_weak = min(5, loss_weak + 1) if loss_weakening else 0
        nxt[token] = {
            "negative_count": neg,
            "profit_weakening_count": weak,
            "loss_weakening_count": loss_weak,
        }

        if pf.get("cost_sda") is None or pnl_raw is None:
            action, reason = "HOLD / NO COST BASIS", "cost basis or valid valuation unavailable"
        elif weak >= 2:
            action, reason = "PARTIAL SELL", "profitable position + weak score/momentum/flow confirmed twice"
        elif neg >= 3:
            action, reason = "SELL / EXIT", "trend + negative SDA flow confirmed 3 times"
        elif loss_weak >= 3:
            action, reason = "SELL / EXIT", "losing position + very weak score + deterioration confirmed 3 times"
        elif score >= 70 and m1h > 0 and flow > 0:
            action, reason = "HOLD / TRAIL", "positive trend and SDA flow"
        else:
            action, reason = "HOLD / WATCH", "no confirmed exit condition"

        out.append({
            "token": token,
            "symbol": pf.get("symbol") or engine.lbl(token, meta),
            "action": action,
            "reason": reason,
            "pnl_pct": pnl_raw,
            "pnl_sda": pf.get("unrealized_pnl_sda"),
            "score": score,
            "m1h": m1h,
            "flow_1h": flow,
        })

    _save_state(nxt)
    order = {"SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2, "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4}
    return sorted(out, key=lambda x: (order.get(x["action"], 9), -x["score"]))


def _fmt_amount(amount):
    x = _n(amount)
    if abs(x) >= 1000000:
        return f"{x:,.0f}"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:,.8f}".rstrip("0").rstrip(".")


def portfolio_message_v16(portfolio, recommendations):
    meta = engine.load(engine.META_FILE, {})
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    lines = ["📊 REAL PORTFOLIO", "", "Current token positions — SDA accumulation", "────────────────────────"]
    for token, pf in sorted(cur.items(), key=lambda x: (x[1].get("symbol") or x[0]).lower()):
        m = meta.get(token, {}) or {}
        symbol = pf.get("symbol") or m.get("symbol") or token[:10] + "..."
        name = m.get("name") or symbol
        val = pf.get("value_sda"); pnl = pf.get("unrealized_pnl_sda"); pct = pf.get("unrealized_pnl_pct")
        value_text = f"{_n(val):.2f} SDA" if val is not None else "UNKNOWN"
        pnl_text = "⚪ P/L UNKNOWN" if pnl is None or pct is None else f"P/L {_n(pnl):+.2f} SDA ({_n(pct):+.2f}%)"
        lines += [f"🪙 {symbol} — {name}", f"   {_fmt_amount(pf.get('amount'))} {symbol}  •  {value_text}", f"   {pnl_text}", ""]

    op = portfolio.get("open_unrealized_pnl_sda"); rp = portfolio.get("realized_pnl_sda"); tp = portfolio.get("known_total_pnl_sda")
    open_cost = _n(portfolio.get("open_cost_sda")); op_pct = _n(op) / open_cost * 100 if op is not None and open_cost else None
    lines += ["────────────────────────"]
    lines.append("⚪ Current open P/L UNKNOWN" if op is None else f"Current open P/L {_n(op):+.2f} SDA" + (f" ({op_pct:+.2f}%)" if op_pct is not None else ""))
    lines.append(f"Historical matched P/L {_n(rp):+.2f} SDA")
    lines.append(f"Known total P/L {_n(tp):+.2f} SDA")
    lines += [f"Matched sells: {int(_n(portfolio.get('matched_sell_count')))}  •  Excluded unmatched: {int(_n(portfolio.get('excluded_unmatched_sell_count')))}", "", "🧭 POSITION ACTION"]
    def _strength(score):
        s = _n(score)
        if s < 20: return "VERY WEAK"
        if s < 40: return "WEAK"
        if s < 60: return "NEUTRAL"
        if s < 75: return "GOOD"
        return "STRONG"
    for r in recommendations:
        action = r.get("action")
        icon = "🔴" if action == "SELL / EXIT" else ("🟠" if action == "PARTIAL SELL" else ("🟢" if action == "HOLD / TRAIL" else "🟡"))
        pnl = r.get("pnl_sda")
        pnl_text = f"{_n(pnl):+.2f} SDA" if pnl is not None else "UNKNOWN"
        score = int(_n(r.get("score")))
        lines.append(f"{icon} {r['symbol']}: {action}  •  P/L {pnl_text}  •  score {score}/100 — {_strength(score)}")
    lines += ["", "ℹ️ Score: 0–19 VERY WEAK • 20–39 WEAK • 40–59 NEUTRAL • 60–74 GOOD • 75–100 STRONG", "ℹ️ Token names from Blockscout metadata.", "ℹ️ Invalid/inconsistent prices are UNKNOWN — never a fake loss.", "🔒 Wallet is READ-ONLY."]
    return "\n".join(lines)


def wallet_message_v16(w):
    w = _sanitize_wallet(w)
    hs = w.get("holdings") or []
    lines = ["👛 REAL WALLET", "", f"💰 SDA: {_n(w.get('native_sda')):.4f}", f"🪙 Token positions: {len(hs)}", "────────────────────────"]
    total = 0.0
    for h in sorted(hs, key=lambda x: (x.get("symbol") or "").lower()):
        symbol = h.get("symbol") or str(h.get("address", ""))[:10] + "..."
        val = h.get("value_sda"); px = _n(h.get("price_sda"))
        if val is not None:
            total += _n(val)
        val_text = f"{_n(val):.2f}" if val is not None else "UNKNOWN"
        lines += [f"🪙 {symbol} — {h.get('name') or symbol}", f"   {_fmt_amount(h.get('amount'))} {symbol}  •  {val_text} SDA", f"   Price: {engine.price(px) + ' SDA' if px > 0 else 'UNKNOWN'}", ""]
    lines += ["────────────────────────", f"📊 Known token value: {total:.2f} SDA"]
    if w.get("native_sda") is not None:
        lines.append(f"💼 TOTAL WALLET VALUE: {_n(w.get('native_sda')) + total:.2f} SDA")
    if w.get("holding_source"):
        lines.append(f"Source: {w['holding_source']}")
    if w.get("error"):
        lines += ["", f"⚠️ {str(w['error'])[:900]}"]
    return "\n".join(lines)


engine.portfolio_history = portfolio_history_v16
engine.portfolio_recommendations = portfolio_recommendations_v16
engine.portfolio_message = portfolio_message_v16
engine.wallet_message = wallet_message_v16
portfolio_history = portfolio_history_v16
portfolio_recommendations = portfolio_recommendations_v16
portfolio_message = portfolio_message_v16
wallet_message = wallet_message_v16

if __name__ == "__main__":
    engine.main()
