"""Final Real Wallet market-data resolver.

The real wallet is read from wallet_data.json holdings. P/L uses the same
FIFO rebuild and live-wallet reconciliation as the dashboard.
"""
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
    merged = dict(md)
    tokens = dict(base)
    if isinstance(extra, dict): tokens.update(extra)
    merged["tokens"] = tokens
    return merged


def _fifo_portfolio(dashboard, portfolio, meta, wallet):
    try:
        import telegram_dashboard_compact as compact
        import main as scanner
        rebuild = getattr(scanner, "_rebuild_fifo", None)
        if callable(rebuild):
            portfolio = rebuild(deepcopy(portfolio), meta)
        return compact._sync_current_to_wallet(wallet, portfolio)
    except Exception as exc:
        print(f"Real Wallet FIFO enrichment error: {exc}")
        return portfolio


def _market_token(tokens, address, symbol):
    if not isinstance(tokens, dict): return {}
    td = tokens.get(address, {})
    if isinstance(td, dict) and td: return td
    want = str(symbol or "").strip().upper()
    for key, value in tokens.items():
        if str(key).strip().lower() == str(address or "").strip().lower() and isinstance(value, dict): return value
        if not isinstance(value, dict): continue
        an = value.get("analysis") if isinstance(value.get("analysis"), dict) else value
        raw = str(value.get("symbol") or an.get("symbol") or "").split("/", 1)[0].strip().upper()
        if raw == want: return value
    return {}


def _enrich_rows(dashboard, rows, md, ws, meta, portfolio):
    wallet = dashboard.load("wallet_data.json", {"holdings": []})
    portfolio = _fifo_portfolio(dashboard, portfolio, meta, wallet)
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
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict): continue
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
        if cost > 0 and value > 0:
            r["pnl_sda"] = value - cost
            r["pnl_pct"] = (value - cost) / cost * 100
        elif r.get("pnl_sda") is None:
            r["pnl_sda"] = None
        r["cost_sda"] = cost
        out.append(r)
    return out, portfolio


def _wallet_rows(dashboard):
    md = _merge_market(dashboard); ws = dashboard.load("whale_data.json", {}); meta = dashboard.load("token_metadata.json", {})
    portfolio = dashboard.load("portfolio_data.json", {"current": {}})
    try:
        rows = v26._wallet_position_rows(dashboard, md, ws, meta, portfolio)
        enriched, _ = _enrich_rows(dashboard, rows, md, ws, meta, portfolio)
        return enriched
    except Exception as exc:
        print(f"Real Wallet rows enrichment error: {exc}")
        wallet = dashboard.load("wallet_data.json", {"holdings": []}); rows = []
        for h in wallet.get("holdings", []) if isinstance(wallet, dict) else []:
            if not isinstance(h, dict): continue
            value = _n(h.get("value_sda")); amount = _n(h.get("amount"))
            if value <= 0 and amount <= 0: continue
            rows.append({"symbol": str(h.get("symbol") or "UNKNOWN").upper(), "address": str(h.get("address") or "").lower(), "amount": amount, "action": "HOLD / MONITOR", "pnl_sda": None, "score": None, "value_sda": value})
        enriched, _ = _enrich_rows(dashboard, rows, md, ws, meta, portfolio)
        return enriched


def _amount_for(dashboard, row):
    if row.get("amount") is not None: return _n(row.get("amount"))
    address = str(row.get("address") or "").strip().lower()
    symbol = str(row.get("symbol") or "").strip().upper()
    wallet = dashboard.load("wallet_data.json", {"holdings": []})
    for h in wallet.get("holdings", []) if isinstance(wallet, dict) else []:
        if not isinstance(h, dict): continue
        ha = str(h.get("address") or "").strip().lower()
        hs = str(h.get("symbol") or "").strip().upper()
        if (address and ha == address) or (symbol and hs == symbol): return _n(h.get("amount"))
    return None


def _qty(amount):
    if amount is None: return ""
    if abs(amount) >= 1000: return f"{amount:,.2f} ks"
    if abs(amount) >= 1: return f"{amount:,.4f} ks"
    return f"{amount:.8f} ks"


def position_action_section(dashboard):
    """Compatibility panel backed by the canonical BOT accounting state."""
    import real_bot_views
    data=real_bot_views._build_dashboard_data(dashboard)
    rows=data.get("positions") or []
    lines=["","🧭 POSITION ACTION • REAL BOT","────────────────────────"]
    if not rows:
        lines.append("⚪ No open BOT positions")
        return lines
    for row in rows:
        pnl=row.get("pnl_sda")
        value=row.get("value_sda")
        icon="🟢" if pnl is not None and pnl>0 else ("🔴" if pnl is not None and pnl<0 else "⚪")
        pnl_text="UNKNOWN" if pnl is None else f"{pnl:+.2f} SDA"
        value_text="UNKNOWN" if value is None else f"{value:.2f} SDA"
        lines.append(f"{icon} {row.get('symbol')} • Value {value_text} • Open P/L {pnl_text}")
    return lines

def real_trading_report(dashboard):
    """Legacy REAL entry point delegates to the unified blockchain-backed menu."""
    import real_bot_views
    text,_keyboard=real_bot_views.render_view(dashboard,"main",refresh=True)
    return text

def real_statistics_report(dashboard):
    """Legacy REAL statistics entry point delegates to the unified state."""
    import real_bot_views
    text,_keyboard=real_bot_views.render_view(dashboard,"stats",refresh=True)
    return text

def patch_dashboard(dashboard):
    dashboard.real_trading_report = lambda: real_trading_report(dashboard)
    dashboard.real_wallet_position_action = lambda: position_action_section(dashboard)
    dashboard.market_debug_report = lambda snapshot=None: v26._market_debug(dashboard, snapshot)
    # Do not replace position_action_v20._real_statistics_report here.
    # Its implementation contains the closed-position history, and this
    # module is loaded after position_action_v20 in the dashboard bootstrap.
    dashboard._real_wallet_market_fix_patched = True
    return dashboard
