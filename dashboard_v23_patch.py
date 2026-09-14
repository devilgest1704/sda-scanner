"""Dashboard integration for the V23/V24/V25 pump-hunter path.

The dashboard uses the same canonical paper_decision() as paper trading.
V25 is shadow-only: it changes ranking/diagnostics and visibility, never the
real wallet and never the MAX-WIN BUY gate.
"""
import json

def _num(v,d=0.0):
    try:return d if v is None else float(v)
    except (TypeError,ValueError):return d

def _v25_state():
    try:
        with open("v25_learner_state.json",encoding="utf-8") as f:x=json.load(f)
        return x.get("paper_hunter",{}) if isinstance(x,dict) else {}
    except Exception:return {}

def _v25(decision):
    if not isinstance(decision,dict):return {}
    return decision.get("v25") or (decision.get("data") or {}).get("v25") or {}

def _v23(decision):
    if not isinstance(decision,dict):return {}
    return decision.get("v23") or (decision.get("data") or {}).get("v23_prediction") or {}

def _paper_decision(address,analysis,ws):
    try:
        import main as scanner
        return scanner.paper_decision(address,analysis,ws)
    except Exception:return {}

def patch_dashboard(dashboard):
    if getattr(dashboard,"_sda_v25_dashboard_patched",False):return dashboard
    original_buy=getattr(dashboard,"_buy_gate_rows",None)
    original_top=getattr(dashboard,"top_buy",None)
    original_debug=getattr(dashboard,"market_debug_report",None)
    original_position=getattr(dashboard,"_position_recommendations",None)
    original_main=getattr(dashboard,"main_dashboard",None)
    if not original_buy:return dashboard

    def buy_gate_rows_v25(md,ws,meta,limit=5):
        base=original_buy(md,ws,meta,max(limit,10));rows=[]
        for row in base:
            d=_paper_decision(row.get("address"),row.get("analysis") or {},ws);data=d.get("data") or {};v=_v25(d);pv=_v23(d)
            score=_num(d.get("score",row.get("score")));m1=_num(data.get("m1h"));m15=_num(data.get("m15"));m4=_num(data.get("m4h"));flow=_num(data.get("net_1h"));trades=_num(data.get("trades_1h")) or _num(row.get("trades"));bear=int(d.get("technical_bear",data.get("technical_bear")) or 0)
            eligible=(row.get("volume_1h",0)>=250 and trades>=5 and m1>=3 and m15>=-0.75 and m4>=0 and flow>0 and bear==0)
            ev=_num(v.get("expected_pl_sda"));mfe=_num(v.get("expected_mfe"));mae=_num(v.get("expected_mae"));rank=score+min(15,max(0,mfe))*0.5+max(-10,min(10,ev))
            x=dict(row);x.update({"score":score,"m1h":m1,"m15":m15,"m4h":m4,"net_1h":flow,"trades":trades,"decision":d,"v25":v,"v23":pv,"v25_eligible":eligible,"v25_expected_pl":ev,"v25_expected_mfe":mfe,"v25_expected_mae":mae,"v25_p10":_num(v.get("p10")),"v25_p20":_num(v.get("p20")),"v25_p30":_num(v.get("p30")),"v25_rank":rank})
            rows.append(x)
        rows.sort(key=lambda r:(1 if r.get("v25_eligible") else 0,r.get("v25_rank",0),r.get("score",0)),reverse=True)
        return rows[:limit]

    def top_buy_v25(md,ws,meta,rows=None,snapshot=None):
        rows=buy_gate_rows_v25(md,ws,meta,5);lines=["🔥 TOP BUY CANDIDATES • MAX-WIN + V25 PUMP-HUNTER",""]
        if snapshot:lines += [f"Snapshot: {snapshot['id']} • {snapshot['time']}",""]
        if not rows:return "\n".join(lines+["⚪ No active candidates (1H volume ≥ 250 SDA)"])
        for i,r in enumerate(rows,1):
            d=r.get("decision") or {};v=r.get("v25") or {};pv=r.get("v23") or {};core=not bool(d.get("blocked"));hunter=bool(r.get("v25_eligible"));status="🟣 V25 PUMP-HUNT" if hunter and not core else ("🟢 CORE + V25" if hunter else ("🟢 CORE BUY" if core else "🟡 WATCH"))
            lines += [f"{i}. {status} {r['label']} • core {r['score']:.0f}/100 • V25 rank {r.get('v25_rank',r['score']):.1f}",f"   M15/M1H/M4H {r.get('m15',0):+.1f}/{r.get('m1h',0):+.1f}/{r.get('m4h',0):+.1f}% • flow {r.get('net_1h',0):+.0f} • vol {r['volume_1h']:.0f} • trades {r['trades']:.0f}",f"   V25: {'ELIGIBLE' if hunter else 'FILTERED'} • EV {r.get('v25_expected_pl',0):+.2f} SDA • MFE {r.get('v25_expected_mfe',0):+.1f}% • MAE {r.get('v25_expected_mae',0):+.1f}% • P10 {r.get('v25_p10',0):.0%}",f"   V23: {'READY' if pv.get('ready') else 'WARMING'} • P5/P10/P20/P30 {_num(pv.get('p5')):.0%}/{_num(pv.get('p10')):.0%}/{_num(pv.get('p20')):.0%}/{_num(pv.get('p30')):.0%}",f"   Core: {d.get('reason') or 'MAX-WIN gates pass'}"]
        return "\n".join(lines)

    def market_debug_v25(snapshot=None):
        if snapshot is None:
            md=dashboard.load("market_data.json",{"tokens":{}});ws=dashboard.load("whale_data.json",{});meta=dashboard.load("token_metadata.json",{});snapshot={"md":md,"ws":ws,"meta":meta,"id":dashboard._snapshot_id(md,ws,meta),"time":dashboard._snapshot_time(md,ws)}
        md=snapshot["md"];ws=snapshot["ws"];meta=snapshot["meta"];rows=buy_gate_rows_v25(md,ws,meta,5);tokens=md.get("tokens",{}) or {};state=_v25_state();hp=state.get("positions") or {};hc=state.get("closed_trades") or [];realized=sum(_num(x.get("closed_profit_sda")) for x in hc if isinstance(x,dict))
        lines=["🐞 MARKET DEBUG • V25 PUMP-HUNTER","",f"Snapshot: {snapshot['id']}",f"State time: {snapshot['time']}",f"Loaded tokens: {len(tokens)}",f"Analyzed tokens: {sum(1 for td in tokens.values() if dashboard._analysis(td))}",f"Whale data entries: {len(ws) if isinstance(ws,dict) else 0}","","CORE: MAX-WIN threshold 78/100 • max 1 BUY/scan","V25: shadow pump hunter • max 3 open • 50 SDA • 6h horizon",f"V25 shadow: {len(hp)} open • {len(hc)} closed • realized {realized:+.2f} SDA","────────────────────────","🎯 TOP BUY CANDIDATES • V25-AWARE"]
        for i,r in enumerate(rows,1):
            d=r.get("decision") or {};v=r.get("v25") or {};pv=r.get("v23") or {};lines += [f"{i}. {'🟣 V25 HUNT' if r.get('v25_eligible') else '⚪ V25 FILTER'} • {'🟢 CORE' if not d.get('blocked') else '🔴 CORE BLOCK'} • {r['label']} • score {r['score']:.0f}/100",f"   INPUT M15/M1H/M4H {r.get('m15',0):+.1f}/{r.get('m1h',0):+.1f}/{r.get('m4h',0):+.1f}% • flow {r.get('net_1h',0):+.0f} • vol {r['volume_1h']:.0f} • trades {r['trades']:.0f}",f"   V23 P5/P10/P20/P30 {_num(pv.get('p5')):.0%}/{_num(pv.get('p10')):.0%}/{_num(pv.get('p20')):.0%}/{_num(pv.get('p30')):.0%} • mean {_num(pv.get('mean_roi')):+.1f}%",f"   V25 EV {_num(v.get('expected_pl_sda')):+.2f} SDA • MFE {_num(v.get('expected_mfe')):+.1f}% • MAE {_num(v.get('expected_mae')):+.1f}% • P10 {_num(v.get('p10')):.0%} • P20 {_num(v.get('p20')):.0%} • P30 {_num(v.get('p30')):.0%}",f"   CORE reason: {d.get('reason') or 'MAX-WIN gates pass'}"]
        return "\n".join(lines)

    def position_rows_v25(*args,**kwargs):
        rows=original_position(*args,**kwargs) if original_position else [];md=args[0] if args else kwargs.get("md") or {};ws=args[1] if len(args)>1 else kwargs.get("ws") or {};out=[]
        for r in rows:
            x=dict(r);addr=x.get("address") or x.get("token");an=dashboard._analysis((md.get("tokens",{}) or {}).get(addr,{}) or {})
            if an:
                d=_paper_decision(addr,an,ws);v=_v25(d);x["v25_expected_pl"]=_num(v.get("expected_pl_sda"));x["v25_expected_mfe"]=_num(v.get("expected_mfe"));x["v25_expected_mae"]=_num(v.get("expected_mae"));x["v25_p10"]=_num(v.get("p10"));x["v25_p20"]=_num(v.get("p20"));x["v25_p30"]=_num(v.get("p30"))
            out.append(x)
        return out

    def main_dashboard_v25(*args,**kwargs):
        text=original_main(*args,**kwargs) if original_main else "";state=_v25_state();pos=state.get("positions") or {};closed=state.get("closed_trades") or [];realized=sum(_num(x.get("closed_profit_sda")) for x in closed if isinstance(x,dict));return text+"\n\n"+f"🧪 V25 PUMP-HUNTER SHADOW • open {len(pos)} • closed {len(closed)} • realized {realized:+.2f} SDA"

    dashboard._buy_gate_rows=buy_gate_rows_v25;dashboard.top_buy=top_buy_v25;dashboard.market_debug_report=market_debug_v25
    if original_position:dashboard._position_recommendations=position_rows_v25
    if original_main:dashboard.main_dashboard=main_dashboard_v25
    dashboard._sda_v25_dashboard_patched=True;return dashboard
