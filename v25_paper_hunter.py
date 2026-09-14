"""V25 PUMP-HUNTER shadow paper portfolio."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone, timedelta
STATE_FILE="v25_learner_state.json"; STATE_KEY="paper_hunter"
INVESTMENT_SDA=50.0; FEE_RATE=.01; SLIPPAGE_RATE=.001; MAX_OPEN=3; MAX_NEW_PER_SCAN=1; HORIZON_HOURS=6
MIN_SCORE=45.; MIN_VOLUME=250.; MIN_TRADES=5.; MIN_M1H=3.; MAX_TECH_BEAR=0; MIN_EV_SDA=0.0

def _num(v,d=0.):
    try:return d if v is None else float(v)
    except(TypeError,ValueError):return d
def _now():return datetime.now(timezone.utc)
def _iso(dt):return dt.astimezone(timezone.utc).isoformat()
def _empty():return {"version":"V25-PUMP-HUNTER","positions":{},"closed_trades":[],"watch":{},"events":[]}
def _load():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:x=json.load(f)
        return x.get(STATE_KEY,_empty()) if isinstance(x,dict) else _empty()
    except Exception:return _empty()
def _save(s):
    try:
        with open(STATE_FILE,encoding="utf-8") as f:base=json.load(f)
        if not isinstance(base,dict):base={}
    except Exception:base={}
    base[STATE_KEY]=s; tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:json.dump(base,f,indent=2,ensure_ascii=False)
    os.replace(tmp,STATE_FILE)
def _analysis(td):return td.get("analysis",td) if isinstance(td,dict) and isinstance(td.get("analysis",td),dict) else {}
def _candidate(scanner,address,analysis,whale):
    try:d=scanner.paper_decision(address,analysis,whale)
    except Exception:return None
    data=d.get("data") or {}; v=d.get("v25") or data.get("v25") or {}; score=_num(d.get("score",data.get("buy_score",data.get("confidence"))))
    f=analysis.get("flow",{}).get("1h",{}) if isinstance(analysis,dict) else {}; volume=_num(data.get("volume_1h",analysis.get("volume_1h"))); trades=_num(data.get("trades_1h")) or _num(f.get("buy_count"))+_num(f.get("sell_count")); bear=int(d.get("technical_bear",data.get("technical_bear")) or 0)
    m1=_num(data.get("m1h"));m15=_num(data.get("m15"));m4=_num(data.get("m4h"));flow=_num(data.get("net_1h"));ev=_num(v.get("expected_pl_sda"));mfe=_num(v.get("expected_mfe"));mae=_num(v.get("expected_mae"))
    eligible=score>=MIN_SCORE and volume>=MIN_VOLUME and trades>=MIN_TRADES and m1>=MIN_M1H and m15>=-.75 and m4>=0 and flow>0 and bear<=MAX_TECH_BEAR
    return {"decision":d,"score":score,"v25":v,"volume":volume,"trades":trades,"bear":bear,"m1h":m1,"m15":m15,"m4h":m4,"flow":flow,"ev":ev,"mfe":mfe,"mae":mae,"core_blocked":bool(d.get("blocked")),"eligible":eligible}
def _close(row,price,reason,now):
    e=_num(row.get("entry_price"));inv=_num(row.get("investment_sda"),INVESTMENT_SDA);gross=inv*(price/e) if e>0 else 0;profit=gross*(1-SLIPPAGE_RATE)*(1-FEE_RATE)-inv*(1+FEE_RATE)
    return {"version":"V25-PUMP-HUNTER","address":row.get("address"),"symbol":row.get("symbol"),"opened_at":row.get("opened_at"),"closed_at":_iso(now),"entry_price":e,"final_price":price,"investment_sda":inv,"closed_profit_sda":round(profit,6),"closed_roi_pct":round(profit/inv*100 if inv else 0,6),"close_reason":reason,"mfe_pct":row.get("mfe_pct",0),"mae_pct":row.get("mae_pct",0),"threshold_times":row.get("threshold_times",{})}
def update():
    try:
        import main as scanner
        pe=scanner._paper;md=pe.load(pe.MARKET_FILE,{"tokens":{}});tokens=md.get("tokens",{}) if isinstance(md,dict) else {};whale=pe.load(pe.WHALE_FILE,{});meta=pe.load(pe.META_FILE,{})
        s=_load();pos=s.setdefault("positions",{});closed=s.setdefault("closed_trades",[]);watch=s.setdefault("watch",{});now=_now();events=[]
        for address,row in list(pos.items()):
            an=_analysis(tokens.get(address,{}));price=_num(an.get("price_in_sda"));
            if price<=0:continue
            e=_num(row.get("entry_price"));row["peak_price"]=max(_num(row.get("peak_price"),e),price);row["trough_price"]=min(_num(row.get("trough_price"),e),price);row["mfe_pct"]=(row["peak_price"]/e-1)*100 if e else 0;row["mae_pct"]=(row["trough_price"]/e-1)*100 if e else 0;row["last_price"]=price
            hits=row.setdefault("threshold_times",{});[hits.setdefault(str(t),_iso(now)) for t in (5,10,20,30) if row["mfe_pct"]>=t]
            c=_candidate(scanner,address,an,whale) or {};score=c.get("score",0);m1=c.get("m1h",0);flow=c.get("flow",0);bear=c.get("bear",0);roi=(price/e-1)*100 if e else 0;weak=score<45 and m1<0 and flow<0;row["weak_count"]=min(5,_num(row.get("weak_count"))+1) if weak else 0
            emergency=roi<=-15 and score<35 and m1<0 and flow<0 and bear>0;partial=roi>10 and row["weak_count"]>=2 and not row.get("partial_done");started=None
            try:started=datetime.fromisoformat(str(row.get("opened_at")).replace("Z","+00:00"))
            except Exception:pass
            if emergency or partial or (started and now-started>=timedelta(hours=HORIZON_HOURS)):
                reason="V25 EMERGENCY EXIT" if emergency else "V25 PARTIAL SELL" if partial else "V25 6H OBSERVATION EXIT";tr=_close(row,price,reason,now);closed.append(tr)
                if partial:row["investment_sda"]=_num(row.get("investment_sda"),INVESTMENT_SDA)*.5;row["partial_done"]=True;row["protected"]=True
                else:del pos[address]
                events.append(("PARTIAL" if partial else "SELL",tr))
            elif row.get("partial_done") and roi>20 and price<=row["peak_price"]*.92:
                tr=_close(row,price,"V25 TRAILING EXIT",now);closed.append(tr);del pos[address];events.append(("SELL",tr))
        core=pe.load(pe.POSITIONS_FILE,{"positions":{}});corepos=core.get("positions",{}) if isinstance(core,dict) else {};cands=[]
        for raw,td in tokens.items():
            a=str(raw).lower()
            if a in pos or a in corepos:continue
            an=_analysis(td);price=_num(an.get("price_in_sda"));
            if price<=0:continue
            c=_candidate(scanner,a,an,whale)
            if not c or not c["eligible"]:continue
            if not c["core_blocked"] and c["score"]>=78:continue
            rank=c["score"]+min(15,max(0,c["mfe"]))*0.5+max(-10,min(10,c["ev"]))
            if c["ev"]<=MIN_EV_SDA:watch[a]={"address":a,"updated_at":_iso(now),"status":"WATCH","reason":"V25 EV <= 0","score":c["score"],"expected_pl_sda":c["ev"],"expected_mfe":c["mfe"],"expected_mae":c["mae"]}
            else:cands.append((rank,a,an,c,price))
        cands.sort(reverse=True,key=lambda x:x[0]);slots=max(0,MAX_OPEN-len(pos))
        for _,a,an,c,price in cands[:min(slots,MAX_NEW_PER_SCAN)]:
            label=str(scanner.lbl(a,meta) if hasattr(scanner,"lbl") else a);pos[a]={"version":"V25-PUMP-HUNTER","address":a,"symbol":label,"opened_at":_iso(now),"entry_price":price,"investment_sda":INVESTMENT_SDA,"peak_price":price,"trough_price":price,"mfe_pct":0,"mae_pct":0,"threshold_times":{},"partial_done":False,"protected":False,"weak_count":0,"entry_score":c["score"],"entry_metrics":c["decision"].get("data") or {},"v25_prediction":c["v25"],"core_blocked":c["core_blocked"]};watch.pop(a,None);events.append(("BUY",pos[a]))
        s={"version":"V25-PUMP-HUNTER","updated_at":_iso(now),"positions":pos,"closed_trades":closed[-500:],"watch":watch,"events":events[-20:],"config":{"investment_sda":INVESTMENT_SDA,"max_open":MAX_OPEN,"max_new_per_scan":MAX_NEW_PER_SCAN,"horizon_hours":HORIZON_HOURS,"min_ev_sda":MIN_EV_SDA}};_save(s);return s
    except Exception as exc:
        s=_load();s["last_error"]=str(exc);s["updated_at"]=_iso(_now());_save(s);return s
def summary(state=None):
    s=state or _load();pos=s.get("positions",{});closed=s.get("closed_trades",[]);realized=sum(_num(x.get("closed_profit_sda")) for x in closed);openp=0
    for x in pos.values():
        e=_num(x.get("entry_price"));p=_num(x.get("last_price"),e);inv=_num(x.get("investment_sda"),INVESTMENT_SDA)
        if e>0:openp+=inv*(p/e)*(1-SLIPPAGE_RATE)*(1-FEE_RATE)-inv*(1+FEE_RATE)
    total=realized+openp;return {"open":len(pos),"closed":len(closed),"watch":len(s.get("watch",{})),"realized_pnl_sda":realized,"open_pnl_sda":openp,"total_pnl_sda":total}
