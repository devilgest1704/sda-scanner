"""Dashboard integration for V23/V24/V25 pump-hunter shadow data."""
import json

def _num(v,d=0.):
    try:return d if v is None else float(v)
    except(TypeError,ValueError):return d

def _v25_state():
    try:
        with open("v25_learner_state.json",encoding="utf-8") as f:x=json.load(f)
        return x.get("paper_hunter",{}) if isinstance(x,dict) else {}
    except Exception:return {}
def _v25(d):return (d or {}).get("v25") or ((d or {}).get("data") or {}).get("v25") or {}
def _v23(d):return (d or {}).get("v23") or ((d or {}).get("data") or {}).get("v23_prediction") or {}
def _decision(address,analysis,ws):
    try:
        import main as scanner;return scanner.paper_decision(address,analysis,ws)
    except Exception:return {}
def _volume_1h(an,data=None):
    """Use the same canonical scanner flow as V25: 1H buy_volume + sell_volume.

    Accept either an analysis object or a paper_decision/data wrapper. This avoids
    the previous bug where the dashboard passed data['analysis'] through the wrong
    level and consequently displayed VOL 1H as zero.
    """
    try:
        import v25_paper_hunter
        return v25_paper_hunter.volume_1h_sda(an,data)
    except Exception:
        for src in (an,data):
            if not isinstance(src,dict):continue
            aa=src.get("analysis") if isinstance(src.get("analysis"),dict) else src
            f=aa.get("flow",{}).get("1h",{}) if isinstance(aa,dict) else {}
            if isinstance(f,dict):
                bv=f.get("buy_volume");sv=f.get("sell_volume")
                if bv is not None or sv is not None:return _num(bv)+_num(sv)
        return 0.
def _v25_gate(d):
    """Mirror the V25 hunter's entry eligibility using the canonical market 1h volume."""
    data=(d or {}).get("data") or {};an=data.get("analysis") if isinstance(data.get("analysis"),dict) else data
    flow=an.get("flow",{}).get("1h",{}) if isinstance(an,dict) else {}
    score=_num((d or {}).get("score",data.get("buy_score",data.get("confidence"))))
    volume=_volume_1h(an,data)
    trades=_num(data.get("trades_1h")) or _num(flow.get("buy_count"))+_num(flow.get("sell_count"))
    bear=int((d or {}).get("technical_bear",data.get("technical_bear")) or 0)
    m1=_num(data.get("m1h"));m15=_num(data.get("m15"));m4=_num(data.get("m4h"));net=_num(data.get("net_1h"))
    return score>=45 and volume>=250 and trades>=5 and m1>=3 and m15>=-.75 and m4>=0 and net>0 and bear==0
def _v25_status(d):
    v=_v25(d);ev=_num(v.get("expected_pl_sda"));
    if _v25_gate(d):return ("HUNT",True) if ev>0 else ("WATCH",True)
    return "REJECT",False
def _gate_reasons(d):
    try:
        import v25_paper_hunter
        data=(d or {}).get("data") or {};an=data.get("analysis") if isinstance(data.get("analysis"),dict) else data;flow=an.get("flow",{}).get("1h",{}) if isinstance(an,dict) else {};score=_num((d or {}).get("score",data.get("buy_score",data.get("confidence"))));volume=v25_paper_hunter.volume_1h_sda(an,data);trades=_num(data.get("trades_1h")) or _num(flow.get("buy_count"))+_num(flow.get("sell_count"));bear=int((d or {}).get("technical_bear",data.get("technical_bear")) or 0)
        return v25_paper_hunter._gate_reasons(score,volume,trades,_num(data.get("m1h")),_num(data.get("m15")),_num(data.get("m4h")),_num(data.get("net_1h")),bear)
    except Exception:return []
def patch_dashboard(dashboard):
    if getattr(dashboard,"_sda_v25_dashboard_patched",False):return dashboard
    original_buy=getattr(dashboard,"_buy_gate_rows",None);original_position=getattr(dashboard,"_position_recommendations",None);original_main=getattr(dashboard,"main_dashboard",None)
    if not original_buy:return dashboard
    def rows_v25(md,ws,meta,limit=5):
        base=original_buy(md,ws,meta,max(limit,10));out=[]
        for r in base:
            d=_decision(r.get("address"),r.get("analysis") or {},ws);data=d.get("data") or {};v=_v25(d);ev=_num(v.get("expected_pl_sda"));mfe=_num(v.get("expected_mfe"));mae=_num(v.get("expected_mae"));score=_num(d.get("score",r.get("score")));status,eligible=_v25_status(d);reasons=_gate_reasons(d)
            x=dict(r);x.update({"decision":d,"v25":v,"v23":_v23(d),"score":score,"v25_eligible":eligible,"v25_status":status,"v25_gate_reasons":reasons,"v25_volume_1h":_volume_1h(r.get("analysis") or data,data),"v25_expected_pl":ev,"v25_expected_mfe":mfe,"v25_expected_mae":mae,"v25_p10":_num(v.get("p10")),"v25_p20":_num(v.get("p20")),"v25_p30":_num(v.get("p30")),"v25_rank":(1 if status=="HUNT" else 0 if status=="WATCH" else -1)*1000+score+min(15,max(0,mfe))*.5+max(-10,min(10,ev))});out.append(x)
        out.sort(key=lambda x:(1 if x["v25_status"]=="HUNT" else 0 if x["v25_status"]=="WATCH" else -1,x["v25_rank"],x["score"]),reverse=True);return out[:limit]
    def _watch_addresses():
        st=_v25_state();watch=st.get("watch") or {};return {str(a).lower() for a in watch} if isinstance(watch,dict) else set()
    def _v25_display_status(r,watch_addresses=None):
        status=r.get("v25_status")
        if status=="HUNT":return "HUNT"
        if status=="WATCH":
            a=str(r.get("address") or "").lower();return "WATCHING" if watch_addresses is not None and a in watch_addresses else "WATCH_CANDIDATE"
        return "REJECT"
    def top_v25(md,ws,meta,rows=None,snapshot=None):
        rows=rows_v25(md,ws,meta,5);watch_addresses=_watch_addresses();lines=["🔥 TOP BUY CANDIDATES • MAX-WIN + V25 PUMP-HUNTER",""]
        for i,r in enumerate(rows,1):
            d=r["decision"];v=r["v25"];pv=r["v23"];core=not bool(d.get("blocked"));display_status=_v25_display_status(r,watch_addresses);icon={"HUNT":"🟣 V25 HUNT","WATCHING":"🟡 V25 WATCHING","WATCH_CANDIDATE":"🟡 V25 WATCH CANDIDATE","REJECT":"⚪ V25 REJECT"}[display_status];lines += [f"{i}. {icon} • {'🟢 CORE' if core else '🔴 CORE BLOCK'} • {r['label']} • score {r['score']:.0f}/100",f"   V25 VOL 1H {r['v25_volume_1h']:.0f} SDA • EV {r['v25_expected_pl']:+.2f} SDA • MFE {r['v25_expected_mfe']:+.1f}% • MAE {r['v25_expected_mae']:+.1f}% • P10/P20/P30 {r['v25_p10']:.0%}/{r['v25_p20']:.0%}/{r['v25_p30']:.0%}",f"   V23 P5/P10/P20/P30 {_num(pv.get('p5')):.0%}/{_num(pv.get('p10')):.0%}/{_num(pv.get('p20')):.0%}/{_num(pv.get('p30')):.0%}",f"   Core: {d.get('reason') or 'MAX-WIN gates pass'}"]
        return "\n".join(lines)
    def debug_v25(snapshot=None):
        if snapshot is None:
            md=dashboard.load("market_data.json",{"tokens":{}});ws=dashboard.load("whale_data.json",{});meta=dashboard.load("token_metadata.json",{});snapshot={"md":md,"ws":ws,"meta":meta,"id":dashboard._snapshot_id(md,ws,meta),"time":dashboard._snapshot_time(md,ws)}
        md,ws,meta=snapshot["md"],snapshot["ws"],snapshot["meta"];rows=rows_v25(md,ws,meta,5);st=_v25_state();pos=st.get("positions") or {};closed=st.get("closed_trades") or [];watch=st.get("watch") or {};watch_addresses={str(a).lower() for a in watch} if isinstance(watch,dict) else set();realized=sum(_num(x.get("closed_profit_sda")) for x in closed if isinstance(x,dict));lines=["🐞 MARKET DEBUG • V25 PUMP-HUNTER","",f"Snapshot: {snapshot['id']}",f"State time: {snapshot['time']}",f"Loaded tokens: {len(md.get('tokens',{}) or {})}",f"Analyzed tokens: {sum(1 for x in (md.get('tokens',{}) or {}).values() if dashboard._analysis(x))}",f"Whale data entries: {len(ws) if isinstance(ws,dict) else 0}","","CORE: MAX-WIN threshold 78/100 • max 1 BUY/scan","V25: shadow pump hunter • max 3 open • 50 SDA • 6h horizon",f"V25 shadow: {len(pos)} open • {len(closed)} closed • {len(watch_addresses)} WATCHING • realized {realized:+.2f} SDA","────────────────────────","🎯 TOP BUY CANDIDATES • V25-AWARE"]
        for i,r in enumerate(rows,1):
            ds=_v25_display_status(r,watch_addresses);icon={"HUNT":"🟣 V25 HUNT","WATCHING":"🟡 V25 WATCHING","WATCH_CANDIDATE":"🟡 V25 WATCH CANDIDATE","REJECT":"⚪ V25 REJECT"}[ds];reasons=r.get("v25_gate_reasons") or [];gate="PASS" if not reasons else "FAIL: "+", ".join(reasons)
            lines += [f"{i}. {icon} • {'🟢 CORE' if not r['decision'].get('blocked') else '🔴 CORE BLOCK'} • {r['label']} • score {r['score']:.0f}/100",f"   VOL 1H {r['v25_volume_1h']:.0f} SDA / {250:.0f} SDA • EV {r['v25_expected_pl']:+.2f} SDA • MFE {r['v25_expected_mfe']:+.1f}% • MAE {r['v25_expected_mae']:+.1f}% • P10/P20/P30 {r['v25_p10']:.0%}/{r['v25_p20']:.0%}/{r['v25_p30']:.0%}",f"   Gate: {gate}"]
        return "\n".join(lines)
    def position_v25(*args,**kwargs):
        rows=original_position(*args,**kwargs) if original_position else [];md=args[0] if args else kwargs.get("md") or {};ws=args[1] if len(args)>1 else kwargs.get("ws") or {};out=[]
        for r in rows:
            x=dict(r);a=x.get("address") or x.get("token");an=dashboard._analysis((md.get("tokens",{}) or {}).get(a,{}) or {});d=_decision(a,an,ws) if an else {};v=_v25(d);status,_=_v25_status(d);x.update({"v25_status":status,"v25_expected_pl":_num(v.get("expected_pl_sda")),"v25_expected_mfe":_num(v.get("expected_mfe")),"v25_expected_mae":_num(v.get("expected_mae"))});out.append(x)
        return out
    def main_v25(*args,**kwargs):
        text=original_main(*args,**kwargs) if original_main else "";st=_v25_state();return text+"\n\n"+f"🧪 V25 PUMP-HUNTER SHADOW • open {len(st.get('positions',{}) or {})} • closed {len(st.get('closed_trades',[]) or [])} • WATCHING {len(st.get('watch',{}) or {})}"
    dashboard._buy_gate_rows=rows_v25;dashboard.top_buy=top_v25;dashboard.market_debug_report=debug_v25
    if original_position:dashboard._position_recommendations=position_v25
    if original_main:dashboard.main_dashboard=main_v25
    dashboard._sda_v25_dashboard_patched=True;return dashboard
