"""Final Real Wallet market-data resolver."""
import sys
from copy import deepcopy
import v26_pump_hunter as v26


def _n(v, default=0.0):
    try: return default if v is None else float(v)
    except Exception: return default


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


def _fifo_portfolio(dashboard, portfolio, meta):
    try:
        import main as scanner
        fifo = getattr(scanner, "_rebuild_fifo", None)
        if callable(fifo): return fifo(deepcopy(portfolio), meta)
    except Exception as exc:
        print(f"Real Wallet FIFO enrichment error: {exc}")
    return portfolio


def _market_token(tokens, address, symbol):
    if not isinstance(tokens, dict): return {}
    td = tokens.get(address, {})
    if isinstance(td, dict) and td: return td
    for key, value in tokens.items():
        if str(key).strip().lower() == str(address or "").strip().lower() and isinstance(value, dict): return value
    want = str(symbol or "").strip().upper()
    for value in tokens.values():
        if not isinstance(value, dict): continue
        an = value.get("analysis") if isinstance(value.get("analysis"), dict) else value
        raw = str(value.get("symbol") or an.get("symbol") or "").split("/", 1)[0].strip().upper()
        if raw == want: return value
    return {}


def _enrich_rows(dashboard, rows, md, ws, meta, portfolio):
    portfolio = _fifo_portfolio(dashboard, portfolio, meta)
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    by_address = {}; by_symbol = {}
    for key, pf in current.items() if isinstance(current, dict) else []:
        if not isinstance(pf, dict): continue
        address = str(pf.get("address") or pf.get("token_address") or key).strip().lower()
        symbol = str(pf.get("symbol") or pf.get("label") or "").split("/", 1)[0].strip().upper()
        if address: by_address[address] = pf
        if symbol: by_symbol[symbol] = pf
    tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
    out = []
    for row in rows:
        r = dict(row)
        address = str(r.get("address") or "").strip().lower()
        symbol = str(r.get("symbol") or "").strip().upper()
        pf = by_address.get(address) or by_symbol.get(symbol) or {}
        td = _market_token(tokens, address, symbol)
        an = td.get("analysis") if isinstance(td, dict) and isinstance(td.get("analysis"), dict) else td
        if r.get("score") is None and isinstance(an, dict) and an:
            try:
                d = v26.decision(address, an, ws)
                r["score"] = _n(d.get("pump_score"), -1)
                r["phase"] = str(d.get("pump_phase") or "NO")
                r["pump_change"] = _n(d.get("pump_change"))
            except Exception: pass
        cost = _n(pf.get("cost_sda"))
        value = _n(r.get("value_sda"))
        if r.get("pnl_sda") is None and cost > 0 and value > 0:
            r["pnl_sda"] = value - cost
            r["pnl_pct"] = (value - cost) / cost * 100
        if r.get("pnl_sda") is not None and r.get("pnl_pct") is None and cost > 0:
            r["pnl_pct"] = _n(r.get("pnl_sda")) / cost * 100
        out.append(r)
    return out


def _wallet_rows(dashboard):
    md = _merge_market(dashboard); ws = dashboard.load("whale_data.json", {}); meta = dashboard.load("token_metadata.json", {})
    portfolio = dashboard.load("portfolio_data.json", {"current": {}})
    try:
        rows = v26._wallet_position_rows(dashboard, md, ws, meta, portfolio)
        return _enrich_rows(dashboard, rows, md, ws, meta, portfolio)
    except Exception:
        wallet = dashboard.load("wallet_data.json", {"holdings": []}); rows = []
        for h in wallet.get("holdings", []) if isinstance(wallet, dict) else []:
            if not isinstance(h, dict): continue
            value = _n(h.get("value_sda"))
            if value <= 0: continue
            rows.append({"symbol": str(h.get("symbol") or "UNKNOWN").upper(), "action": "HOLD / MONITOR", "pnl_sda": None, "score": None, "value_sda": value})
        return _enrich_rows(dashboard, rows, md, ws, meta, portfolio)


def position_action_section(dashboard):
    rows = _wallet_rows(dashboard); lines = ["", "🧭 POSITION ACTION • REAL WALLET", "────────────────────────"]
    if not rows: lines.append("⚪ No live wallet holdings"); return lines
    for r in rows:
        pnl = "UNKNOWN" if r.get("pnl_sda") is None else f"{_n(r.get('pnl_sda')):+.2f} SDA"; score = "N/A" if r.get("score") is None else f"{_n(r.get('score')):.0f}/100"; action = str(r.get("action") or "HOLD / MONITOR")
        icon = "🔴" if action.startswith("EMERGENCY") or action.startswith("SELL") else ("🟢" if action == "HOLD / PUMP" else "🟡")
        lines.append(f"{icon} {r.get('symbol')} • {action} • P/L {pnl} • score {score}")
    return lines


def real_trading_report(dashboard):
    rows = _wallet_rows(dashboard); wallet = dashboard.load("wallet_data.json", {})
    sda_balance = _n(wallet.get("native_sda"), _n(wallet.get("sda_balance_sda"), _n(wallet.get("sda_balance"))))
    known_value = sum(_n(r.get("value_sda")) for r in rows) or _n(wallet.get("total_token_value_sda")); total_wallet = _n(wallet.get("total_value_sda"), sda_balance + known_value)
    open_pnl = sum(_n(r.get("pnl_sda")) for r in rows if r.get("pnl_sda") is not None); portfolio = dashboard.load("portfolio_data.json", {}); realized = _n(portfolio.get("realized_pnl_sda")) if isinstance(portfolio, dict) else 0.0
    lines = ["👛 REAL WALLET & POSITIONS", "────────────────────────"]
    for r in rows:
        pnl = "UNKNOWN" if r.get("pnl_sda") is None else f"{_n(r.get('pnl_sda')):+.2f} SDA"; lines.append(f"{'🟢' if r.get('pnl_sda') is not None and _n(r.get('pnl_sda')) > 0 else '🔴' if r.get('pnl_sda') is not None and _n(r.get('pnl_sda')) < 0 else '⚪'} {r.get('symbol')} • {r.get('action')} • P/L {pnl}"); lines.append(f"   Value: {_n(r.get('value_sda')):.2f} SDA")
    if not rows: lines.append("⚪ No live wallet holdings")
    lines += ["", "────────────────────────", f"📊 Known token value: {known_value:.2f} SDA", f"💼 TOTAL WALLET VALUE: {total_wallet:.2f} SDA", "", "💹 P/L SUMMARY", f"{'🟢' if open_pnl > 0 else '🔴' if open_pnl < 0 else '⚪'} Current open P/L {open_pnl:+.2f} SDA", f"{'🟢' if realized > 0 else '🔴' if realized < 0 else '⚪'} Historical realized P/L {realized:+.2f} SDA", f"{'🟢' if realized + open_pnl > 0 else '🔴' if realized + open_pnl < 0 else '⚪'} Total P/L {realized + open_pnl:+.2f} SDA", "", "👁 READ-ONLY • No real order is executed"]
    return "\n".join(lines)


def real_statistics_report(dashboard): return real_trading_report(dashboard)


def patch_dashboard(dashboard):
    dashboard.real_trading_report = lambda: real_trading_report(dashboard); dashboard.real_statistics_report = lambda: real_statistics_report(dashboard); dashboard.real_wallet_position_action = lambda: position_action_section(dashboard); dashboard.market_debug_report = lambda snapshot=None: v26._market_debug(dashboard, snapshot)
    pa = sys.modules.get("position_action_v20")
    if pa is not None: pa._real_statistics_report = real_statistics_report
    dashboard._real_wallet_market_fix_patched = True
    return dashboard
