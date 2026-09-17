"""Final Real Wallet market-data resolver.

Wallet positions can be outside the current TOP candidate set. Resolve them
from the full market snapshot, market_analysis, and token metadata. Read-only.
"""
import sys
import v26_pump_hunter as v26


def _n(v, default=0.0):
    try:
        return default if v is None else float(v)
    except Exception:
        return default


def _analysis(td):
    if not isinstance(td, dict):
        return {}
    x = td.get("analysis")
    return x if isinstance(x, dict) else td


def _norm_symbol(value):
    s = str(value or "").strip().upper()
    for sep in ("/", "-", "_"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def _merge_market(dashboard):
    md = dashboard.load("market_data.json", {"tokens": {}})
    compact = dashboard.load("market_analysis.json", {"tokens": {}})
    if not isinstance(md, dict): md = {"tokens": {}}
    base = md.get("tokens") if isinstance(md.get("tokens"), dict) else {}
    extra = compact.get("tokens", {}) if isinstance(compact, dict) else {}
    merged = dict(md); tokens = dict(base)
    if isinstance(extra, dict): tokens.update(extra)
    merged["tokens"] = tokens
    return merged


def _resolve(tokens, token, pf, metadata):
    candidates = [token]
    if isinstance(pf, dict): candidates += [pf.get("address"), pf.get("token_address"), pf.get("token")]
    for candidate in candidates:
        if not candidate: continue
        raw = str(candidate).strip(); low = raw.lower()
        if raw in tokens: return raw, _analysis(tokens[raw])
        if low in tokens: return low, _analysis(tokens[low])
    wanted = _norm_symbol((pf or {}).get("symbol") or (pf or {}).get("label") or token)
    if isinstance(metadata, dict) and wanted:
        for address, meta in metadata.items():
            if not isinstance(meta, dict): continue
            if _norm_symbol(meta.get("symbol")) == wanted:
                raw = str(address).strip(); low = raw.lower()
                if raw in tokens: return raw, _analysis(tokens[raw])
                if low in tokens: return low, _analysis(tokens[low])
    for address, value in tokens.items():
        if not isinstance(value, dict): continue
        a = _analysis(value)
        fields = [value.get("symbol"), value.get("token_symbol"), value.get("base_symbol"), value.get("pair"), value.get("label"), a.get("symbol"), a.get("pair")]
        if any(_norm_symbol(field) == wanted for field in fields if field): return str(address), a
    return "", {}


def _decision(address, analysis, ws):
    if not analysis: return {}
    try: return v26.decision(address, analysis, ws)
    except Exception: return {}


def _pnl(pf, pnl_pct, entry, current_price):
    for key in ("unrealized_pnl_sda", "pnl_sda", "unrealized_profit_sda", "profit_sda"):
        if pf.get(key) is not None: return _n(pf.get(key))
    current_value = next((_n(pf.get(k), None) for k in ("current_value_sda", "value_sda", "market_value_sda", "current_sda") if pf.get(k) is not None), None)
    cost_basis = next((_n(pf.get(k), None) for k in ("cost_basis_sda", "invested_sda", "cost_sda", "entry_value_sda", "position_value_sda") if pf.get(k) is not None), None)
    if current_value is not None and cost_basis is not None: return current_value - cost_basis
    if cost_basis is not None: return cost_basis * pnl_pct / 100.0
    amount = next((_n(pf.get(k), None) for k in ("amount", "token_amount", "quantity", "balance") if pf.get(k) is not None), None)
    if amount is not None and entry and current_price:
        decimals = _n(pf.get("decimals"), 18); units = 10 ** int(decimals) if 0 <= decimals <= 36 else 1e18
        return (current_price - entry) * amount / units
    return None


def _action(pnl_pct, d, pf):
    phase = str(d.get("pump_phase") or "NO"); weak = int(_n(pf.get("pump_weak_count")))
    if pnl_pct <= -7: return "EMERGENCY SELL", "V26 hard stop: loss reached -7%"
    if pnl_pct > 0 and weak >= 3: return "SELL / EXIT", "V26 confirmed weakness: 3 consecutive weak scans"
    if phase == "ENTRY" and pnl_pct >= 0: return "HOLD / PUMP", "V26 pump confirmation is active"
    if phase == "WATCH": return "HOLD / WATCH", "V26 is watching for confirmation"
    return "HOLD / WATCH", "no confirmed V26 exit condition"


def real_trading_report(dashboard):
    md = _merge_market(dashboard); ws = dashboard.load("whale_data.json", {}); metadata = dashboard.load("token_metadata.json", {})
    portfolio = dashboard.load("portfolio_data.json", {"current": {}}); current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = md.get("tokens", {}) or {}; rows = []
    for token, pf in current.items():
        if not isinstance(pf, dict): continue
        address, analysis = _resolve(tokens, token, pf, metadata); d = _decision(address or str(token).lower(), analysis, ws)
        entry = _n(pf.get("entry_price")); current_price = _n(analysis.get("price_in_sda"))
        pnl_pct = _n(pf.get("unrealized_pnl_pct"), ((current_price / entry - 1) * 100 if entry and current_price else 0.0))
        pnl_sda = _pnl(pf, pnl_pct, entry, current_price)
        rows.append({"token": token, "pf": pf, "analysis": analysis, "d": d, "pnl_pct": pnl_pct, "pnl_sda": pnl_sda})
    rows.sort(key=lambda r: (r["pnl_sda"] is None, -(r["pnl_sda"] if r["pnl_sda"] is not None else r["pnl_pct"])))

    lines = ["👛 REAL WALLET & PORTFOLIO", "────────────────────────"]
    for row in rows:
        pf = row["pf"]; analysis = row["analysis"]; pnl_pct = row["pnl_pct"]; pnl_sda = row["pnl_sda"]
        symbol = pf.get("symbol") or pf.get("label") or str(row["token"])[:10]
        amount = next((pf.get(k) for k in ("amount", "token_amount", "quantity", "balance") if pf.get(k) is not None), None)
        value = next((pf.get(k) for k in ("current_value_sda", "value_sda", "market_value_sda", "current_sda") if pf.get(k) is not None), None)
        price = _n(analysis.get("price_in_sda")) if analysis else 0.0
        icon = "🟢" if pnl_sda is not None and pnl_sda > 0 else ("🔴" if pnl_sda is not None and pnl_sda < 0 else "⚪")
        pnl_text = "N/A SDA" if pnl_sda is None else f"{pnl_sda:+.2f} SDA"
        lines.append(f"{icon} {symbol} • P/L {pnl_text} ({pnl_pct:+.2f}%)")
        lines.append(f"   {_n(amount):g} • {_n(value):.2f} SDA • Price: {price:.6f} SDA")

    wallet = dashboard.load("wallet_data.json", {}); known_value = sum(_n(r["pf"].get("current_value_sda")) for r in rows if r["pf"].get("current_value_sda") is not None)
    total_wallet = _n(wallet.get("total_value_sda"), known_value + _n(wallet.get("sda_balance_sda"), wallet.get("sda_balance"))); sda_balance = _n(wallet.get("sda_balance_sda"), wallet.get("sda_balance"))
    realized = _n(portfolio.get("realized_pnl_sda")); open_pnl = sum(_n(r["pnl_sda"]) for r in rows if r["pnl_sda"] is not None)
    cost_basis = sum(next((_n(r["pf"].get(k)) for k in ("cost_basis_sda", "invested_sda", "cost_sda", "entry_value_sda", "position_value_sda") if r["pf"].get(k) is not None), 0.0) for r in rows)
    lines += ["", "────────────────────────", f"💰 SDA: {sda_balance:.4f}", f"📊 Known token value: {known_value:.2f} SDA", f"💼 TOTAL WALLET VALUE: {total_wallet:.2f} SDA", "", "💹 P/L SUMMARY", f"{'🟢' if open_pnl > 0 else '🔴' if open_pnl < 0 else '⚪'} Current open P/L {open_pnl:+.2f} SDA", f"{'🟢' if realized > 0 else '🔴' if realized < 0 else '⚪'} Historical realized P/L {realized:+.2f} SDA", f"{'🟢' if realized + open_pnl > 0 else '🔴' if realized + open_pnl < 0 else '⚪'} Total P/L {realized + open_pnl:+.2f} SDA", f"Matched positions: {len(rows)} • Cost basis: {cost_basis:.2f} SDA", f"🪙 Token positions: {len(rows)}", "", "👁 READ-ONLY • No real order is executed"]
    return "\n".join(lines)


def real_statistics_report(dashboard):
    portfolio = dashboard.load("portfolio_data.json", {}); meta = dashboard.load("token_metadata.json", {})
    import main as scanner; fifo = getattr(scanner, "_rebuild_fifo", None)
    if callable(fifo):
        try: portfolio = fifo(portfolio, meta)
        except Exception: pass
    trades = portfolio.get("trades", []) if isinstance(portfolio, dict) else []; current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    sells = [x for x in trades if isinstance(x, dict) and str(x.get("side") or "").upper() == "SELL" and dashboard.engine.num(x.get("matched_amount")) > 0]
    profits = [dashboard.engine.num(x.get("matched_proceeds_sda")) - dashboard.engine.num(x.get("cost_basis_sda")) for x in sells]
    wins = [x for x in profits if x > 0]; losses = [x for x in profits if x < 0]; realized = dashboard.engine.num(portfolio.get("realized_pnl_sda")); open_pnl = sum(dashboard.engine.num(x.get("unrealized_pnl_sda")) for x in current.values() if isinstance(x, dict))
    pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0); pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
    def trade_time(t):
        for key in ("timestamp", "time", "closed_at", "sold_at", "created_at", "date"):
            if t.get(key) not in (None, ""): return str(t.get(key))
        return "time N/A"
    def trade_symbol(t): return str(t.get("symbol") or t.get("label") or t.get("token_symbol") or t.get("token") or t.get("address") or "UNKNOWN")
    lines = ["📈 REAL STATISTICS", "", f"🟢 Open positions: {len(current)}", f"📁 Closed trades: {len(profits)}", "────────────────────────", f"Realized P/L: {realized:+.2f} SDA", f"Open P/L: {open_pnl:+.2f} SDA", f"Cumulative P/L: {realized + open_pnl:+.2f} SDA", f"Win rate: {(len(wins) / len(profits) * 100 if profits else 0):.1f}%", f"Profit factor: {pf_text}", "", "📜 TRADE HISTORY", "────────────────────────"]
    if not sells: lines.append("⚪ No closed real-wallet trades recorded")
    else:
        for trade, pnl in reversed(list(zip(sells, profits))):
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪"); lines.append(f"{icon} {trade_symbol(trade)} • P/L {pnl:+.2f} SDA"); lines.append(f"   {trade_time(trade)}")
    lines += ["", "👁 READ-ONLY • No real order is executed"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard, "_real_wallet_market_fix_patched", False): return dashboard
    dashboard.real_trading_report = lambda: real_trading_report(dashboard)
    dashboard.real_statistics_report = lambda: real_statistics_report(dashboard)
    dashboard.market_debug_report = lambda snapshot=None: v26._market_debug(dashboard, snapshot)
    pa = sys.modules.get("position_action_v20")
    if pa is not None: pa._real_statistics_report = real_statistics_report
    dashboard._real_wallet_market_fix_patched = True
    return dashboard
