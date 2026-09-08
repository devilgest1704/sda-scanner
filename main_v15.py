# SDA Scanner V15 — valuation hardening + clearer wallet P/L
# Imports the current main.py (V14) and adds a conservative safety layer.
import math
import main as v14
import engine

# Keep all V14 accounting/recommendation behavior, but reject clearly corrupt
# stablecoin valuations such as USDX -> ~0 SDA.
_original_wallet_snapshot = engine.wallet_snapshot
_original_portfolio_history = engine.portfolio_history


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except Exception:
        return default


def _valuation_is_sane(h):
    if not isinstance(h, dict):
        return False
    px = _num(h.get("price_sda"), 0.0)
    value = h.get("value_sda")
    amount = _num(h.get("amount"), 0.0)
    if not (math.isfinite(px) and px > 0 and value is not None and amount > 0):
        return False
    value = _num(value, 0.0)
    if not math.isfinite(value) or value < 0:
        return False

    # A stable/USD-pegged token priced at fractions of a cent in SDA is almost
    # certainly a bad/missing market quote. Do not turn it into a fake -100% P/L.
    symbol = str(h.get("symbol") or "").upper()
    if ("USD" in symbol or symbol in {"USDX", "USDT", "USDC", "DAI", "USD1"}) and not (0.01 <= px <= 10.0):
        return False

    # Value must agree with amount × price. A large mismatch means the quote or
    # balance representation is inconsistent, so valuation is safer as UNKNOWN.
    expected = amount * px
    if expected > 0:
        ratio = value / expected
        if not (0.80 <= ratio <= 1.20):
            return False
    return True


def wallet_snapshot_v15(md, meta):
    w = _original_wallet_snapshot(md, meta)
    for h in w.get("holdings", []) if isinstance(w, dict) else []:
        if not _valuation_is_sane(h):
            h["valuation_status"] = "UNKNOWN"
            h["price_sda"] = 0.0
            h["value_sda"] = None
    if isinstance(w, dict):
        w["total_token_value_sda"] = sum(
            _num(h.get("value_sda")) for h in w.get("holdings", [])
            if h.get("value_sda") is not None
        )
        if w.get("native_sda") is not None:
            w["total_value_sda"] = _num(w.get("native_sda")) + w["total_token_value_sda"]
    return w


engine.wallet_snapshot = wallet_snapshot_v15


def portfolio_history_v15(wallet, md, meta, ld, previous=None, wallet_obj=None):
    p = _original_portfolio_history(wallet, md, meta, ld, previous, wallet_obj)
    if not isinstance(p, dict):
        return p

    w = wallet_obj if isinstance(wallet_obj, dict) else wallet_snapshot_v15(md, meta)
    holdings = {str(h.get("address", "")).lower(): h for h in w.get("holdings", []) if h.get("address")}
    for token, h in holdings.items():
        if not _valuation_is_sane(h):
            continue
        # The V14 portfolio history has already rebuilt FIFO and aligned the
        # wallet. Re-apply the final sanity decision to prevent a corrupt quote.
        cur = p.get("current", {}).get(token)
        if isinstance(cur, dict):
            cur["price_sda"] = _num(h.get("price_sda"))
            cur["value_sda"] = _num(h.get("value_sda"))
            cost = cur.get("cost_sda")
            if cost is not None and _num(cost) > 0:
                cur["unrealized_pnl_sda"] = cur["value_sda"] - _num(cost)
                cur["unrealized_pnl_pct"] = cur["unrealized_pnl_sda"] / _num(cost) * 100
    # Explicitly clear invalid valuations from the V14 result.
    for token, cur in p.get("current", {}).items():
        h = holdings.get(token)
        if h is not None and not _valuation_is_sane(h):
            cur["price_sda"] = 0.0
            cur["value_sda"] = None
            cur["unrealized_pnl_sda"] = None
            cur["unrealized_pnl_pct"] = None
            cur["cost_basis_status"] = "PRICE_UNKNOWN"
    known = [x for x in p.get("current", {}).values() if x.get("unrealized_pnl_sda") is not None]
    p["open_cost_sda"] = sum(_num(x.get("cost_sda")) for x in known)
    p["open_value_sda"] = sum(_num(x.get("value_sda")) for x in known)
    p["open_unrealized_pnl_sda"] = sum(_num(x.get("unrealized_pnl_sda")) for x in known)
    p["known_open_positions"] = len(known)
    p["known_total_pnl_sda"] = _num(p.get("realized_pnl_sda")) + p["open_unrealized_pnl_sda"]
    return p


engine.portfolio_history = portfolio_history_v15


def _pnl_marker(pnl):
    if pnl is None:
        return "⚪"
    return "🟢" if _num(pnl) > 0 else ("🔴" if _num(pnl) < 0 else "⚪")


def portfolio_message_v15(portfolio, recommendations):
    # Reuse V14's complete layout, then make P/L lines visually explicit.
    meta = engine.load(engine.META_FILE, {})
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    lines = ["📊 REAL PORTFOLIO", "", "Current token positions — SDA accumulation", "────────────────────────"]
    for token, pf in sorted(cur.items(), key=lambda x: (x[1].get("symbol") or x[0]).lower()):
        m = meta.get(str(token).lower(), {}) if isinstance(meta, dict) else {}
        symbol = pf.get("symbol") or m.get("symbol") or str(token)[:10] + "..."
        name = m.get("name") or symbol
        amount = v14._fmt_amount(pf.get("amount"))
        val = pf.get("value_sda")
        pnl = pf.get("unrealized_pnl_sda")
        pct = pf.get("unrealized_pnl_pct")
        value_text = f"{_num(val):.2f} SDA" if val is not None else "UNKNOWN"
        pnl_text = f"{_num(pnl):+.2f} SDA ({_num(pct):+.2f}%)" if pnl is not None and pct is not None else "UNKNOWN"
        lines += [f"🪙 {symbol} — {name}", f"   {amount} {symbol}  •  {value_text}", f"   {_pnl_marker(pnl)} P/L {pnl_text}"]

    op = portfolio.get("open_unrealized_pnl_sda")
    rp = portfolio.get("realized_pnl_sda")
    tp = portfolio.get("known_total_pnl_sda")
    lines += ["────────────────────────", f"{_pnl_marker(op)} Current open P/L: {_num(op):+.2f} SDA", f"{_pnl_marker(rp)} Historical matched P/L: {_num(rp):+.2f} SDA", f"{_pnl_marker(tp)} Known total P/L: {_num(tp):+.2f} SDA", f"Matched sells: {int(_num(portfolio.get('matched_sell_count')))}  •  Excluded unmatched: {int(_num(portfolio.get('excluded_unmatched_sell_count')))}", "", "🧭 POSITION ACTION"]
    for r in recommendations:
        action = r["action"]
        icon = "🔴" if action == "SELL / EXIT" else ("🟠" if action == "PARTIAL SELL" else ("🟢" if action == "HOLD / TRAIL" else "🟡"))
        p = r.get("pnl_sda")
        pt = f"{_num(p):+.2f} SDA" if p is not None else "UNKNOWN"
        lines.append(f"{icon} {r['symbol']}: {action}  •  P/L {pt}  •  score {int(_num(r['score']))}/100")
    lines += ["", "ℹ️ Token names from Blockscout metadata.", "ℹ️ Invalid/inconsistent prices are UNKNOWN — never a fake loss.", "🔒 Wallet is READ-ONLY."]
    return "\n".join(lines)


engine.portfolio_message = portfolio_message_v15


# Re-export compatibility names from V14.
portfolio_history = portfolio_history_v15
portfolio_recommendations = v14.portfolio_recommendations_v14
portfolio_message = portfolio_message_v15
wallet_message = v14.wallet_message_v14


if __name__ == "__main__":
    engine.main()
