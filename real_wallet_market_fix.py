"""Final Real Wallet market-data resolver.

The real wallet is read from wallet_data.json holdings. Paper portfolio.current
is only used when available for cost-basis/P&L enrichment. Read-only.
"""
import sys
import v26_pump_hunter as v26


def _n(v, default=0.0):
    try: return default if v is None else float(v)
    except Exception: return default


def _analysis(td):
    if not isinstance(td, dict): return {}
    x = td.get("analysis")
    return x if isinstance(x, dict) else td


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


def _wallet_rows(dashboard):
    md = _merge_market(dashboard)
    ws = dashboard.load("whale_data.json", {})
    meta = dashboard.load("token_metadata.json", {})
    portfolio = dashboard.load("portfolio_data.json", {"current": {}})
    return v26._wallet_position_rows(dashboard, md, ws, meta, portfolio)


def real_trading_report(dashboard):
    rows = _wallet_rows(dashboard)
    wallet = dashboard.load("wallet_data.json", {})
    sda_balance = _n(wallet.get("native_sda"), _n(wallet.get("sda_balance_sda"), _n(wallet.get("sda_balance"))))
    known_value = sum(_n(r.get("value_sda")) for r in rows)
    total_wallet = _n(wallet.get("total_value_sda"), sda_balance + known_value)
    open_pnl = sum(_n(r.get("pnl_sda")) for r in rows if r.get("pnl_sda") is not None)
    portfolio = dashboard.load("portfolio_data.json", {})
    realized = _n(portfolio.get("realized_pnl_sda")) if isinstance(portfolio, dict) else 0.0

    lines = ["👛 REAL WALLET & POSITIONS", "────────────────────────"]
    if not rows:
        lines.append("⚪ No live wallet holdings")
    else:
        for r in rows:
            pnl = "UNKNOWN" if r.get("pnl_sda") is None else f"{_n(r.get('pnl_sda')):+.2f} SDA"
            icon = "🟢" if r.get("pnl_sda") is not None and _n(r.get("pnl_sda")) > 0 else ("🔴" if r.get("pnl_sda") is not None and _n(r.get("pnl_sda")) < 0 else "⚪")
            lines.append(f"{icon} {r.get('symbol')} • {r.get('action')} • P/L {pnl}")
            lines.append(f"   Value: {_n(r.get('value_sda')):.2f} SDA • score {'N/A' if r.get('score') is None else f\"{_n(r.get('score')):.0f}/100\"}")
    lines += ["", "────────────────────────", f"📊 Known token value: {known_value:.2f} SDA", f"💼 TOTAL WALLET VALUE: {total_wallet:.2f} SDA", "", "💹 P/L SUMMARY", f"{'🟢' if open_pnl > 0 else '🔴' if open_pnl < 0 else '⚪'} Current open P/L {open_pnl:+.2f} SDA", f"{'🟢' if realized > 0 else '🔴' if realized < 0 else '⚪'} Historical realized P/L {realized:+.2f} SDA", f"{'🟢' if realized + open_pnl > 0 else '🔴' if realized + open_pnl < 0 else '⚪'} Total P/L {realized + open_pnl:+.2f} SDA", "", "👁 READ-ONLY • No real order is executed"]
    return "\n".join(lines)


def real_statistics_report(dashboard):
    return real_trading_report(dashboard)


def patch_dashboard(dashboard):
    dashboard.real_trading_report = lambda: real_trading_report(dashboard)
    dashboard.real_statistics_report = lambda: real_statistics_report(dashboard)
    dashboard.market_debug_report = lambda snapshot=None: v26._market_debug(dashboard, snapshot)
    pa = sys.modules.get("position_action_v20")
    if pa is not None: pa._real_statistics_report = real_statistics_report
    dashboard._real_wallet_market_fix_patched = True
    return dashboard
