"""Final Real Wallet market-data resolver.

Wallet positions can be outside the current TOP candidate set. Resolve them
from the full market snapshot, market_analysis, and token metadata. Read-only.
"""
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
    if not isinstance(md, dict):
        md = {"tokens": {}}
    base = md.get("tokens") if isinstance(md.get("tokens"), dict) else {}
    extra = compact.get("tokens", {}) if isinstance(compact, dict) else {}
    merged = dict(md)
    tokens = dict(base)
    if isinstance(extra, dict):
        tokens.update(extra)
    merged["tokens"] = tokens
    return merged


def _resolve(tokens, token, pf, metadata):
    # 1) Address is authoritative whenever the portfolio already contains it.
    candidates = [token]
    if isinstance(pf, dict):
        candidates += [pf.get("address"), pf.get("token_address"), pf.get("token")]
    for candidate in candidates:
        if not candidate:
            continue
        raw = str(candidate).strip()
        low = raw.lower()
        if raw in tokens:
            return raw, _analysis(tokens[raw])
        if low in tokens:
            return low, _analysis(tokens[low])

    # 2) Portfolio keys are often symbols. token_metadata is the canonical
    # symbol -> address bridge; market_analysis itself intentionally has no
    # symbol field and therefore cannot be resolved by symbol alone.
    wanted = _norm_symbol((pf or {}).get("symbol") or (pf or {}).get("label") or token)
    if isinstance(metadata, dict) and wanted:
        for address, meta in metadata.items():
            if not isinstance(meta, dict):
                continue
            if _norm_symbol(meta.get("symbol")) == wanted:
                raw = str(address).strip()
                low = raw.lower()
                if raw in tokens:
                    return raw, _analysis(tokens[raw])
                if low in tokens:
                    return low, _analysis(tokens[low])

    # 3) Last-resort symbol scan for market snapshots that carry symbol data.
    for address, value in tokens.items():
        if not isinstance(value, dict):
            continue
        a = _analysis(value)
        fields = [value.get("symbol"), value.get("token_symbol"), value.get("base_symbol"), value.get("pair"), value.get("label"), a.get("symbol"), a.get("pair")]
        if any(_norm_symbol(field) == wanted for field in fields if field):
            return str(address), a
    return "", {}


def _decision(address, analysis, ws):
    if not analysis:
        return {}
    try:
        return v26.decision(address, analysis, ws)
    except Exception:
        return {}


def _action(pnl_pct, d, pf):
    phase = str(d.get("pump_phase") or "NO")
    weak = int(_n(pf.get("pump_weak_count")))
    if pnl_pct <= -7:
        return "EMERGENCY SELL", "V26 hard stop: loss reached -7%"
    if pnl_pct > 0 and weak >= 3:
        return "SELL / EXIT", "V26 confirmed weakness: 3 consecutive weak scans"
    if phase == "ENTRY" and pnl_pct >= 0:
        return "HOLD / PUMP", "V26 pump confirmation is active"
    if phase == "WATCH":
        return "HOLD / WATCH", "V26 is watching for confirmation"
    return "HOLD / WATCH", "no confirmed V26 exit condition"


def real_trading_report(dashboard):
    md = _merge_market(dashboard)
    ws = dashboard.load("whale_data.json", {})
    metadata = dashboard.load("token_metadata.json", {})
    portfolio = dashboard.load("portfolio_data.json", {"current": {}})
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    tokens = md.get("tokens", {}) or {}
    lines = ["🧭 POSITION ACTION • REAL WALLET", "────────────────────────", f"Market snapshot: {len(tokens)} tokens (market_data + market_analysis)"]
    if not current:
        lines.append("⚪ No open real-wallet positions")
    for token, pf in current.items():
        if not isinstance(pf, dict):
            continue
        address, analysis = _resolve(tokens, token, pf, metadata)
        d = _decision(address or str(token).lower(), analysis, ws)
        score = d.get("pump_score")
        phase = str(d.get("pump_phase") or "NO")
        entry = _n(pf.get("entry_price"))
        current_price = _n(analysis.get("price_in_sda"))
        pnl_pct = _n(pf.get("unrealized_pnl_pct"), ((current_price / entry - 1) * 100 if entry and current_price else 0.0))
        action, reason = _action(pnl_pct, d, pf) if analysis else ("HOLD / MARKET DATA N/A", "market data not resolved")
        icon = {"EMERGENCY SELL":"🚨", "SELL / EXIT":"🔴", "HOLD / PUMP":"🟢", "HOLD / WATCH":"🟡", "HOLD / MARKET DATA N/A":"🟡"}.get(action, "🟡")
        score_text = "N/A" if score is None else f"{_n(score):.0f}/100"
        symbol = pf.get("symbol") or pf.get("label") or str(token)[:10]
        lines.append(f"{icon} {symbol}: {action} • P/L {pnl_pct:+.2f}% • score {score_text}")
        if analysis:
            lines.append(f"   phase {phase} • Δ {_n(d.get('pump_change')):+.1f} • M1H {_n(d.get('m1h')):+.2f}% • flow {_n(d.get('net_1h')):+.0f} SDA")
        lines.append(f"   {reason}")
    lines += ["", "👁 READ-ONLY • V26 paper policy; no real order is executed"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard, "_real_wallet_market_fix_patched", False):
        return dashboard
    dashboard.real_trading_report = lambda: real_trading_report(dashboard)
    dashboard._real_wallet_market_fix_patched = True
    return dashboard
