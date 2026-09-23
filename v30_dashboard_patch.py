"""V30 dashboard presentation layer.

Keeps the Telegram dashboard on the same decision engine as paper trading.
Read-only: never submits real orders.
"""
import v30_pump_hunter as v30
import v26_dashboard_patch as v26


def _n(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d


def _analysis(td):
    if not isinstance(td,dict): return {}
    x=td.get("analysis")
    return x if isinstance(x,dict) else td


def _decision(dashboard,address,analysis,ws):
    try:return v30.decision(address,analysis,ws)
    except Exception as exc:
        return {"score":0,"pump_score":0,"pump_change":0,"pump_phase":"NO",
                "m15":0,"m1h":0,"m4h":0,"net_1h":0,"volume_1h":0,
                "trades_1h":0,"buy_ratio":0,"trade_ratio":0,
                "paper_buy_blocked":True,
                "paper_buy_block_reason":f"V30 decision error: {type(exc).__name__}"}


def _label(dashboard,address,meta):
    try:return dashboard.engine.lbl(address,meta)
    except:return str(address)[:12]


def _merged_market(dashboard,md):
    if not isinstance(md,dict):md={"tokens":{}}
    base=md.get("tokens") if isinstance(md.get("tokens"),dict) else {}
    try: compact=dashboard.load("market_analysis.json",{"tokens":{}})
    except Exception: compact={}
    extra=compact.get("tokens",{}) if isinstance(compact,dict) else {}
    if not isinstance(extra,dict) or not extra:return md
    merged=dict(md); tokens=dict(base); tokens.update(extra); merged["tokens"]=tokens
    for k,v in compact.items():
        if k!="tokens":merged[k]=v
    return merged


def _top_buy(dashboard,md,ws,meta,rows=None,snapshot=None):
    md=_merged_market(dashboard,md)
    ranked=[]
    for address,td in (md.get("tokens",{}) or {}).items():
        an=_analysis(td)
        if not an or _n(an.get("price_in_sda"))<=0:continue
        d=_decision(dashboard,address,an,ws)
        ranked.append((d,str(address)))
    ranked.sort(key=lambda x:(_n(x[0].get("pump_score")),_n(x[0].get("pump_change"))),reverse=True)

    lines=[
        "🔥 TOP BUY CANDIDATES • V30 CLEAN PUMP-HUNTER","",
        f"BUY ≥ {v30.ENTRY_SCORE:.0f} • WATCH ≥ 40 • fresh impulse + flow confirmation",
        "────────────────────────"
    ]
    if not ranked:
        lines.append("⚪ No market candidates")
    else:
        for i,(d,address) in enumerate(ranked[:5],1):
            score=_n(d.get("pump_score")); change=_n(d.get("pump_change")); phase=str(d.get("pump_phase") or "NO")
            blocked=bool(d.get("paper_buy_blocked"))
            if not blocked and phase in ("IGNITION","CONFIRMATION","BREAKOUT"):
                status="🟢 BUY"
            elif phase=="WATCH":
                status="🟡 WATCH"
            else:
                status="⚪ NO"
            lines.append(f"{i}. {status} • {_label(dashboard,address,meta)} • {score:.0f}/100")
            lines.append(
                f"   Phase {phase} • Δ +{change:.1f} • 15M {_n(d.get('m15')):+.2f}% • "
                f"1H {_n(d.get('m1h')):+.2f}% • 4H {_n(d.get('m4h')):+.2f}%"
            )
            lines.append(
                f"   Flow {_n(d.get('net_1h')):+.0f} SDA • Vol {_n(d.get('volume_1h')):.0f} • "
                f"Trades {_n(d.get('trades_1h')):.0f} • Buy ratio {_n(d.get('buy_ratio')):.2f}x"
            )
            if blocked:
                lines.append(f"   ⏳ {d.get('paper_buy_block_reason','waiting for confirmation')}")
            else:
                lines.append(f"   🚀 V30 ready • quality {_n(d.get('pump_quality')):.0f} • impulse +{_n(d.get('pump_change')):.1f}")
    try:
        lines.extend(v26._position_action_section(dashboard,md,ws,meta))
    except Exception:
        pass
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard,"_v30_dashboard_patched",False):return dashboard

    # Re-route all V26 dashboard position decisions to the same V30 engine.
    v26._decision=lambda dashboard,address,analysis,ws:_decision(dashboard,address,analysis,ws)
    dashboard.top_buy=lambda md,ws,meta,rows=None,snapshot=None:_top_buy(dashboard,md,ws,meta,rows,snapshot)

    def market_debug_report(snapshot=None):
        md=_merged_market(dashboard,dashboard.load("market_data.json",{"tokens":{}}))
        ws=dashboard.load("whale_data.json",{}); meta=dashboard.load("token_metadata.json",{})
        ranked=[]
        for address,td in (md.get("tokens",{}) or {}).items():
            an=_analysis(td)
            if not an or _n(an.get("price_in_sda"))<=0:continue
            ranked.append((_decision(dashboard,address,an,ws),str(address)))
        ranked.sort(key=lambda x:(_n(x[0].get("pump_score")),_n(x[0].get("pump_change"))),reverse=True)
        ready=sum(1 for d,_ in ranked if not d.get("paper_buy_blocked"))
        lines=[
            "🐞 MARKET DEBUG • V30 CLEAN PUMP-HUNTER","",
            f"Tokens analyzed: {len(ranked)}",
            f"🟢 BUY-ready: {ready}",
            f"Entry score: {v30.ENTRY_SCORE:.0f}",
            "────────────────────────"
        ]
        for i,(d,address) in enumerate(ranked[:10],1):
            phase=str(d.get("pump_phase") or "NO")
            status="🟢 BUY" if not d.get("paper_buy_blocked") else ("🟡 WATCH" if phase=="WATCH" else "⚪ NO")
            lines.append(f"{i}. {_label(dashboard,address,meta)} • {status} • score {_n(d.get('pump_score')):.0f} • phase {phase} • Δ +{_n(d.get('pump_change')):.1f}")
            lines.append(f"   M15/M1H/M4H {_n(d.get('m15')):+.2f}/{_n(d.get('m1h')):+.2f}/{_n(d.get('m4h')):+.2f}% | flow {_n(d.get('net_1h')):+.0f} | vol {_n(d.get('volume_1h')):.0f} | trades {_n(d.get('trades_1h')):.0f}")
            lines.append(f"   buy ratio {_n(d.get('buy_ratio')):.2f}x | quality {_n(d.get('pump_quality')):.0f} | impulse +{_n(d.get('pump_change')):.1f} | {d.get('paper_buy_block_reason','ready')}")
        return "\n".join(lines)

    dashboard.market_debug_report=market_debug_report
    dashboard._v30_dashboard_patched=True
    return dashboard
