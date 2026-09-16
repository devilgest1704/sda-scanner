"""V26 dashboard policy: Position Action + Market Debug.

Read-only UI. Uses the exact same V26 scorer as Paper Trading, so the
explanation cannot disagree with execution.
"""
import main as scanner


def _n(v, d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d


def _action_for(pf, analysis, ws):
    d = scanner.paper_decision(str(pf.get("address") or ""), analysis, ws)
    score = _n(d.get("score")); phase = str(d.get("pump_phase") or "NO")
    pnl = pf.get("unrealized_pnl_pct")
    pnl = _n(pnl) if pnl is not None else None
    pos = pf
    weak = int(_n(pos.get("pump_weak_count")))
    mfe = _n(pos.get("pump_mfe_pct"))
    if pnl is not None and pnl <= -7:
        return "EMERGENCY SELL", "V26 hard risk limit reached"
    if pnl is not None and pnl > 0 and weak >= 3:
        return "SELL / EXIT", "pump breakdown confirmed for 3 scans"
    if pnl is not None and pnl > 0 and phase == "ENTRY":
        return "HOLD / PUMP", f"pump active • score {score:.0f} • acceleration +{_n(d.get('pump_change')):.1f} • MFE {mfe:+.1f}%"
    if pnl is not None and pnl > 0 and phase == "WATCH":
        return "HOLD / WATCH", f"profit protected • pump cooling/watch • score {score:.0f}"
    if pnl is not None and pnl < 0 and score >= 45:
        return "HOLD / WATCH", f"setup not broken • score {score:.0f} • waiting for flow/acceleration confirmation"
    return "HOLD / WATCH", f"no confirmed V26 exit • score {score:.0f} • phase {phase}"


def patch_dashboard(dashboard):
    if getattr(dashboard, "_v26_dashboard_patched", False):
        return dashboard
    old_report = getattr(dashboard, "real_trading_report", None)
    old_debug = getattr(dashboard, "market_debug_report", None)

    def real_trading_report():
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = dashboard.load("portfolio_data.json", {"current": {}})
        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        holdings = {str(h.get("address") or "").lower(): h for h in (wallet.get("holdings", []) or []) if isinstance(h, dict)}
        lines = ["🧭 POSITION ACTION • V26 PUMP HUNTER", "────────────────────────"]
        if not current:
            lines.append("⚪ No open positions")
        for address, pf in current.items():
            if not isinstance(pf, dict): continue
            key = str(address).lower()
            td = (md.get("tokens", {}) or {}).get(key, {})
            analysis = td.get("analysis", td) if isinstance(td, dict) else {}
            if not analysis and key in holdings:
                analysis = (md.get("tokens", {}) or {}).get(key, {}) or {}
            d = scanner.paper_decision(key, analysis, ws) if analysis else {"score": None, "pump_phase":"NO"}
            action, reason = _action_for(pf, analysis, ws) if analysis else ("HOLD / MARKET DATA N/A", "market data unavailable")
            label = pf.get("symbol") or pf.get("label") or key[:10] + "..."
            pnl = "N/A" if pf.get("unrealized_pnl_pct") is None else f"{_n(pf.get('unrealized_pnl_pct')):+.2f}%"
            lines.append(f"{action} • {label} • P/L {pnl} • score {_n(d.get('score')):.0f}/100")
            lines.append(f"   {reason}")
        lines += ["", "🧠 Policy: V26 uses acceleration + flow + volume + activity; no fixed TP while a pump is alive.", "👁 READ-ONLY • no real order is executed"]
        return "\n".join(lines)

    def market_debug_report(snapshot=None):
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        rows = []
        for address, td in (md.get("tokens", {}) or {}).items():
            analysis = dashboard._analysis(td)
            if not analysis or _n(analysis.get("price_in_sda")) <= 0: continue
            d = scanner.paper_decision(address, analysis, ws)
            rows.append((d, address, analysis))
        rows.sort(key=lambda x: (_n(x[0].get("score")), _n(x[0].get("pump_change"))), reverse=True)
        ready = sum(1 for d,_,_ in rows if not d.get("blocked"))
        watch = sum(1 for d,_,_ in rows if d.get("pump_phase") == "WATCH")
        lines = ["🐞 MARKET DEBUG • V26 PUMP HUNTER", "", f"Tokens analyzed: {len(rows)}", f"🟢 PUMP ENTRY: {ready}", f"🟡 WATCH: {watch}", "", "TOP PUMP SIGNALS", "────────────────────────"]
        for i,(d,address,analysis) in enumerate(rows[:10],1):
            m=d.get("v26",{}).get("metrics",{})
            phase=d.get("pump_phase")
            status="🟢 ENTRY" if phase=="ENTRY" else ("🟡 WATCH" if phase=="WATCH" else "⚪ NO")
            lines += [f"{i}. {dashboard.engine.lbl(address,meta)} • {status} • score {_n(d.get('score')):.0f} • Δ +{_n(d.get('pump_change')):.1f}",
                       f"   M15/M1H/M4H {_n(d.get('m15')):+.1f}/{_n(d.get('m1h')):+.1f}/{_n(d.get('m4h')):+.1f}% | flow {_n(d.get('net_1h')):+.0f} | vol {_n(d.get('volume_1h')):.0f} | trades {_n(d.get('trades_1h')):.0f}",
                       f"   buy/sell vol {(_n(m.get('buy_ratio'))):.2f}x | trade ratio {(_n(m.get('trade_ratio'))):.2f}x | reason: {d.get('paper_buy_block_reason')}"]
        lines += ["", "🔬 V26 does not veto a new pump because M4H is negative.", "🔬 Predictor V23/V24/V25 remains advisory/learning data; it is not a hard entry veto."]
        return "\n".join(lines)

    dashboard.real_trading_report = real_trading_report
    dashboard.market_debug_report = market_debug_report
    dashboard._v26_dashboard_patched = True
    return dashboard
