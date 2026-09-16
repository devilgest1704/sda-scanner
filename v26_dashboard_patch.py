"""V26 dashboard presentation layer.

Uses the exact same V26 decision function as paper execution. Real wallet stays
read-only. Legacy V21/V25 labels are intentionally removed from the main view.
"""
import v26_pump_hunter as v26


def _n(v, d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d


def _analysis(td):
    if not isinstance(td, dict): return {}
    x = td.get("analysis")
    return x if isinstance(x, dict) else td


def _decision(dashboard, address, analysis, ws):
    try: return v26.decision(address, analysis, ws)
    except Exception: return {"score":0,"pump_score":0,"pump_change":0,"pump_phase":"NO","m15":0,"m1h":0,"m4h":0,"net_1h":0,"volume_1h":0,"trades_1h":0,"buy_ratio":0,"paper_buy_block_reason":"V26 decision error"}


def _label(dashboard, address, meta):
    try: return dashboard.engine.lbl(address, meta)
    except Exception: return str(address)[:12]


def _position_action_section(dashboard, md, ws, meta):
    """Render the current manual Real Wallet Position Action on the main view.

    This intentionally delegates to dashboard._position_recommendations, which
    is the canonical wallet/position policy installed by the later dashboard
    consistency patch. The section is read-only and does not place orders.
    """
    try:
        portfolio = dashboard.load("portfolio_data.json", {"current": {}})
        builder = getattr(dashboard, "_position_recommendations", None)
        rows = builder(md, ws, meta, portfolio) if callable(builder) else []
    except Exception:
        rows = []

    lines = ["", "🧭 POSITION ACTION • REAL WALLET", "────────────────────────"]
    if not rows:
        lines.append("⚪ No open real-wallet positions")
        return lines

    for row in rows:
        action = str(row.get("action") or "HOLD / WATCH")
        icon = {
            "EMERGENCY SELL": "🚨",
            "SELL / EXIT": "🔴",
            "PARTIAL SELL": "🟠",
            "HOLD / TRAIL": "🟢",
            "HOLD / PUMP": "🟢",
            "HOLD / WATCH": "🟡",
            "HOLD / NO COST BASIS": "🟡",
            "HOLD / MARKET DATA N/A": "🟡",
        }.get(action, "🟡")
        pnl_sda = row.get("pnl_sda")
        pnl = "UNKNOWN" if pnl_sda is None else f"{_n(pnl_sda):+.2f} SDA"
        score = "N/A" if row.get("score") is None else f"{_n(row.get('score')):.0f}/100"
        symbol = row.get("symbol") or row.get("token") or "UNKNOWN"
        lines.append(f"{icon} {symbol}: {action} • P/L {pnl} • score {score}")
        reason = str(row.get("reason") or "")
        if reason:
            lines.append(f"   {reason}")
    return lines


def _top_buy(dashboard, md, ws, meta, rows=None, snapshot=None):
    ranked=[]
    for address,td in (md.get("tokens",{}) or {}).items():
        an=_analysis(td)
        if not an or _n(an.get("price_in_sda"))<=0: continue
        d=_decision(dashboard,address,an,ws); ranked.append((d,str(address)))
    ranked.sort(key=lambda x:(_n(x[0].get("pump_score")),_n(x[0].get("pump_change"))),reverse=True)
    lines=["🔥 TOP BUY CANDIDATES • V26 PUMP-HUNTER","",f"ENTRY ≥ {v26.ENTRY_SCORE:.0f} • WATCH ≥ {v26.WATCH_SCORE:.0f} • previous-scan confirmation","────────────────────────"]
    if not ranked:
        lines.append("⚪ No market candidates")
    else:
        for i,(d,address) in enumerate(ranked[:5],1):
            score=_n(d.get("pump_score")); change=_n(d.get("pump_change")); phase=str(d.get("pump_phase") or "NO")
            icon="🟢 ENTRY" if phase=="ENTRY" else ("🟡 WATCH" if phase=="WATCH" else "⚪ NO")
            lines.append(f"{i}. {icon} • {_label(dashboard,address,meta)} • {score:.0f}/100")
            lines.append(f"   Δ {change:+.1f} • 15M {_n(d.get('m15')):+.2f}% • 1H {_n(d.get('m1h')):+.2f}% • 4H {_n(d.get('m4h')):+.2f}%")
            lines.append(f"   Flow {_n(d.get('net_1h')):+.0f} SDA • Vol {_n(d.get('volume_1h')):.0f} • Trades {_n(d.get('trades_1h')):.0f} • Buy ratio {_n(d.get('buy_ratio')):.2f}x")
            if phase!="ENTRY": lines.append(f"   ⏳ {d.get('paper_buy_block_reason','waiting for confirmation')}")
    lines.extend(_position_action_section(dashboard, md, ws, meta))
    return "\n".join(lines)


def _market_debug(dashboard, snapshot=None):
    md=dashboard.load("market_data.json",{"tokens":{}}); ws=dashboard.load("whale_data.json",{}); meta=dashboard.load("token_metadata.json",{})
    ranked=[]
    for address,td in (md.get("tokens",{}) or {}).items():
        an=_analysis(td)
        if not an or _n(an.get("price_in_sda"))<=0: continue
        ranked.append((_decision(dashboard,address,an,ws),str(address)))
    ranked.sort(key=lambda x:(_n(x[0].get("pump_score")),_n(x[0].get("pump_change"))),reverse=True)
    entry=sum(1 for d,_ in ranked if d.get("pump_phase")=="ENTRY"); watch=sum(1 for d,_ in ranked if d.get("pump_phase")=="WATCH")
    lines=["🐞 MARKET DEBUG • V26 PUMP-HUNTER","",f"Tokens analyzed: {len(ranked)}",f"🟢 ENTRY: {entry}",f"🟡 WATCH: {watch}",f"Entry threshold: {v26.ENTRY_SCORE:.0f}","Confirmation: previous scan required","────────────────────────"]
    for i,(d,address) in enumerate(ranked[:10],1):
        phase=str(d.get("pump_phase") or "NO"); status="🟢 ENTRY" if phase=="ENTRY" else ("🟡 WATCH" if phase=="WATCH" else "⚪ NO")
        lines.append(f"{i}. {_label(dashboard,address,meta)} • {status} • score {_n(d.get('pump_score')):.0f} • Δ {_n(d.get('pump_change')):+.1f}")
        lines.append(f"   M15/M1H/M4H {_n(d.get('m15')):+.2f}/{_n(d.get('m1h')):+.2f}/{_n(d.get('m4h')):+.2f}% | flow {_n(d.get('net_1h')):+.0f} | vol {_n(d.get('volume_1h')):.0f} | trades {_n(d.get('trades_1h')):.0f}")
        lines.append(f"   buy ratio {_n(d.get('buy_ratio')):.2f}x | trade ratio {_n(d.get('trade_ratio')):.2f}x | {d.get('paper_buy_block_reason','ready')}")
    return "\n".join(lines)


def _position_recommendations(dashboard, md, ws, meta, portfolio):
    tokens=md.get("tokens",{}) or {}; current=portfolio.get("current",{}) if isinstance(portfolio,dict) else {}; out=[]
    for token,pf in current.items():
        pf=pf if isinstance(pf,dict) else {}; address=str(pf.get("address") or pf.get("token_address") or token).lower(); td=tokens.get(address,{}) or {}; an=_analysis(td); d=_decision(dashboard,address,an,ws) if an else {}
        score=_n(d.get("pump_score"),-1); phase=str(d.get("pump_phase") or "NO"); entry=_n(pf.get("entry_price")); cur=_n(an.get("price_in_sda")); pnl_sda=pf.get("unrealized_pnl_sda")
        pnl_pct=_n(pf.get("unrealized_pnl_pct"), ((cur/entry-1)*100 if entry and cur else 0)); weak=int(_n(pf.get("pump_weak_count")))
        if pnl_pct<=-7: action="EMERGENCY SELL"
        elif pnl_pct>0 and weak>=3: action="SELL / EXIT"
        elif phase=="ENTRY" and pnl_pct>=0: action="HOLD / PUMP"
        elif phase=="WATCH": action="HOLD / WATCH"
        elif score>=40: action="HOLD / MONITOR"
        else: action="HOLD / WEAK"
        out.append({"symbol":pf.get("symbol") or pf.get("label") or _label(dashboard,address,meta),"action":action,"pnl_sda":_n(pnl_sda,0),"score":score if score>=0 else None,"phase":phase,"pump_change":_n(d.get("pump_change"))})
    return out


def _real_trading_report(dashboard):
    md=dashboard.load("market_data.json",{"tokens":{}}); ws=dashboard.load("whale_data.json",{}); meta=dashboard.load("token_metadata.json",{}); portfolio=dashboard.load("portfolio_data.json",{"current":{}}); current=portfolio.get("current",{}) if isinstance(portfolio,dict) else {}
    lines=["🧭 POSITION ACTION • V26 PUMP-HUNTER","────────────────────────"]
    for address,pf in current.items():
        if not isinstance(pf,dict): continue
        key=str(address).lower(); an=_analysis((md.get("tokens",{}) or {}).get(key,{})); d=_decision(dashboard,key,an,ws) if an else {}; score=_n(d.get("pump_score"),0); phase=str(d.get("pump_phase") or "NO"); pnl=_n(pf.get("unrealized_pnl_pct"),0); weak=int(_n(pf.get("pump_weak_count")))
        action="🚨 EMERGENCY SELL" if pnl<=-7 else ("🔴 SELL / EXIT" if pnl>0 and weak>=3 else ("🟢 HOLD / PUMP" if phase=="ENTRY" else "🟡 HOLD / WATCH"))
        lines.append(f"{action} • {pf.get('symbol') or pf.get('label') or key[:10]} • P/L {pnl:+.2f}% • score {score:.0f}")
        lines.append(f"   phase {phase} • Δ {_n(d.get('pump_change')):+.1f} • M1H {_n(d.get('m1h')):+.2f}% • flow {_n(d.get('net_1h')):+.0f}")
    if len(lines)==2: lines.append("⚪ No open positions")
    lines += ["","👁 READ-ONLY • V26 paper policy; no real order is executed"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard,"_v26_dashboard_patched",False): return dashboard
    dashboard.top_buy=lambda md,ws,meta,rows=None,snapshot=None:_top_buy(dashboard,md,ws,meta,rows,snapshot)
    dashboard.market_debug_report=lambda snapshot=None:_market_debug(dashboard,snapshot)
    dashboard._position_recommendations=lambda md,ws,meta,portfolio:_position_recommendations(dashboard,md,ws,meta,portfolio)
    dashboard.real_trading_report=lambda:_real_trading_report(dashboard)
    dashboard._v26_dashboard_patched=True
    return dashboard
