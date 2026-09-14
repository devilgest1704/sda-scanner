"""V25 PUMP-HUNTER shadow paper portfolio.

This is intentionally separate from positions.json. It experiments with
pump-oriented entries/exits without changing the live MAX-WIN paper account.
The same market snapshot and canonical scanner decision are used for scoring.
"""
from __future__ import annotations
import json, os
from datetime import datetime, timezone, timedelta

STATE_FILE = "v25_learner_state.json"
STATE_KEY = "paper_hunter"
INVESTMENT_SDA = 50.0
FEE_RATE = 0.01
SLIPPAGE_RATE = 0.001
MAX_OPEN = 3
MAX_NEW_PER_SCAN = 1
HORIZON_HOURS = 6
MIN_SCORE = 45.0
MIN_VOLUME = 250.0
MIN_TRADES = 5.0
MIN_M1H = 3.0
MAX_TECH_BEAR = 0

def _num(v, default=0.0):
    try: return default if v is None else float(v)
    except (TypeError, ValueError): return default

def _now(): return datetime.now(timezone.utc)
def _iso(dt): return dt.astimezone(timezone.utc).isoformat()

def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x=json.load(f)
        if isinstance(x,dict) and isinstance(x.get(STATE_KEY),dict): return x[STATE_KEY]
        return {"version":"V25-PUMP-HUNTER","positions":{},"closed_trades":[]}
    except Exception:
        return {"version":"V25-PUMP-HUNTER","positions":{},"closed_trades":[]}

def _save(x):
    base={}
    try:
        with open(STATE_FILE,encoding="utf-8") as f: base=json.load(f)
        if not isinstance(base,dict): base={}
    except Exception: pass
    base[STATE_KEY]=x
    tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(base,f,indent=2,ensure_ascii=False)
    os.replace(tmp,STATE_FILE)

def _analysis(td):
    if isinstance(td,dict) and isinstance(td.get("analysis"),dict): return td["analysis"]
    return td if isinstance(td,dict) else {}

def _weak(score):
    return (_num(score.get("confidence")) < 45 and _num(score.get("m1h")) < 0 and _num(score.get("net_1h")) < 0)

def _candidate(scanner,address,analysis,whale):
    try: d=scanner.paper_decision(address,analysis,whale)
    except Exception: return None
    data=d.get("data") or {}; pred=d.get("v25") or data.get("v25") or {}
    score=_num(d.get("score",data.get("buy_score",data.get("confidence"))))
    f=(analysis.get("flow",{}).get("1h",{}) or {})
    volume=_num(data.get("volume_1h",analysis.get("volume_1h"))); trades=_num(data.get("trades_1h")) or (_num(f.get("buy_count"))+_num(f.get("sell_count")))
    bull=int(d.get("technical_bull",data.get("technical_bull")) or 0); bear=int(d.get("technical_bear",data.get("technical_bear")) or 0)
    m1=_num(data.get("m1h")); m15=_num(data.get("m15")); m4=_num(data.get("m4h")); flow=_num(data.get("net_1h"))
    eligible=(score>=MIN_SCORE and volume>=MIN_VOLUME and trades>=MIN_TRADES and m1>=MIN_M1H and m15>=-0.75 and m4>=0 and flow>0 and bear<=MAX_TECH_BEAR)
    return {"decision":d,"score":score,"v25":pred,"volume":volume,"trades":trades,"bull":bull,"bear":bear,"m1h":m1,"m15":m15,"m4h":m4,"flow":flow,"core_blocked":bool(d.get("blocked")),"eligible":eligible}

def _close_position(row,price,reason,now):
    entry=_num(row.get("entry_price")); inv=_num(row.get("investment_sda"),INVESTMENT_SDA)
    gross=inv*(price/entry) if entry>0 else 0; proceeds=gross*(1-SLIPPAGE_RATE)*(1-FEE_RATE); cost=inv*(1+FEE_RATE); profit=proceeds-cost
    return {"version":"V25-PUMP-HUNTER","address":row.get("address"),"symbol":row.get("symbol"),"opened_at":row.get("opened_at"),"closed_at":_iso(now),"entry_price":entry,"final_price":price,"investment_sda":inv,"closed_profit_sda":round(profit,6),"closed_roi_pct":round(profit/inv*100 if inv else 0,6),"close_reason":reason,"mfe_pct":row.get("mfe_pct",0.0),"mae_pct":row.get("mae_pct",0.0),"threshold_times":row.get("threshold_times",{})}

def update():
    """Run one shadow scan. Returns the complete V25 paper state."""
    try:
        import main as scanner
        pe=scanner._paper; md=pe.load(pe.MARKET_FILE,{"tokens":{}}); tokens=md.get("tokens",{}) if isinstance(md,dict) else {}; whale=pe.load(pe.WHALE_FILE,{}); meta=pe.load(pe.META_FILE,{})
        state=_load(); positions=state.get("positions",{}) if isinstance(state.get("positions"),dict) else {}; closed=state.get("closed_trades",[]) if isinstance(state.get("closed_trades"),list) else []; now=_now(); events=[]
        for address,row in list(positions.items()):
            an=_analysis(tokens.get(address,{}) or {}); price=_num(an.get("price_in_sda"));
            if price<=0: continue
            entry=_num(row.get("entry_price")); peak=max(_num(row.get("peak_price"),entry),price); trough=min(_num(row.get("trough_price"),entry),price); row["peak_price"]=peak; row["trough_price"]=trough; row["mfe_pct"]=(peak/entry-1)*100 if entry else 0; row["mae_pct"]=(trough/entry-1)*100 if entry else 0
            hits=row.setdefault("threshold_times",{});
            for target in (5,10,20,30):
                if row["mfe_pct"]>=target and str(target) not in hits: hits[str(target)]=_iso(now)
            row["last_price"]=price; row["last_seen_at"]=_iso(now); d=_candidate(scanner,address,an,whale) or {}; score=d.get("score",0); m1=d.get("m1h",0); flow=d.get("flow",0); bear=d.get("bear",0); roi=(price/entry-1)*100 if entry else 0; row["last_score"]=score; row["last_m1h"]=m1; row["last_flow"]=flow; row["last_roi_pct"]=roi
            weak=_weak(d); row["weak_count"]=min(5,_num(row.get("weak_count"))+1) if weak else 0
            emergency=roi<=-15 and score<35 and m1<0 and flow<0 and bear>0; partial=roi>10 and row["weak_count"]>=2 and not row.get("partial_done")
            if emergency:
                tr=_close_position(row,price,"V25 EMERGENCY EXIT",now); closed.append(tr); del positions[address]; events.append(("SELL",tr))
            elif partial:
                half=dict(row); half["investment_sda"]=_num(row.get("investment_sda"),INVESTMENT_SDA)*0.5; tr=_close_position(half,price,"V25 PARTIAL SELL",now); closed.append(tr); row["investment_sda"]=half["investment_sda"]; row["partial_done"]=True; row["protected"]=True; events.append(("PARTIAL",tr))
            elif row.get("partial_done") and roi>20 and price<=row["peak_price"]*0.92:
                tr=_close_position(row,price,"V25 TRAILING EXIT",now); closed.append(tr); del positions[address]; events.append(("SELL",tr))
            started=None
            try: started=datetime.fromisoformat(str(row.get("opened_at")).replace("Z","+00:00"))
            except Exception: pass
            if address in positions and started and now-started>=timedelta(hours=HORIZON_HOURS):
                tr=_close_position(row,price,"V25 6H OBSERVATION EXIT",now); closed.append(tr); del positions[address]; events.append(("EXPIRY",tr))
        core=pe.load(pe.POSITIONS_FILE,{"positions":{}}); core_positions=core.get("positions",{}) if isinstance(core,dict) else {}; candidates=[]
        for raw,td in tokens.items():
            address=str(raw).lower()
            if address in positions or address in core_positions: continue
            an=_analysis(td); price=_num(an.get("price_in_sda"));
            if price<=0: continue
            c=_candidate(scanner,address,an,whale)
            if not c or not c["eligible"]: continue
            if not c["core_blocked"] and c["score"]>=78: continue
            ev=_num((c["v25"] or {}).get("expected_pl_sda")); mfe=_num((c["v25"] or {}).get("expected_mfe")); rank=c["score"]+min(15,max(0,mfe))*0.5+max(-10,min(10,ev)); candidates.append((rank,address,an,c,price))
        candidates.sort(reverse=True,key=lambda x:x[0]); slots=max(0,MAX_OPEN-len(positions))
        for _,address,an,c,price in candidates[:min(slots,MAX_NEW_PER_SCAN)]:
            label=str(scanner.lbl(address,meta) if hasattr(scanner,"lbl") else address); positions[address]={"version":"V25-PUMP-HUNTER","address":address,"symbol":label,"opened_at":_iso(now),"entry_price":price,"investment_sda":INVESTMENT_SDA,"peak_price":price,"trough_price":price,"mfe_pct":0.0,"mae_pct":0.0,"threshold_times":{},"partial_done":False,"protected":False,"weak_count":0,"entry_score":c["score"],"entry_metrics":c["decision"].get("data") or {},"v25_prediction":c["v25"],"core_blocked":c["core_blocked"]}; events.append(("BUY",positions[address]))
        state={"version":"V25-PUMP-HUNTER","updated_at":_iso(now),"positions":positions,"closed_trades":closed[-500:],"events":events[-20:],"config":{"investment_sda":INVESTMENT_SDA,"max_open":MAX_OPEN,"max_new_per_scan":MAX_NEW_PER_SCAN,"horizon_hours":HORIZON_HOURS}}; _save(state); return state
    except Exception as exc:
        state=_load(); state["last_error"]=str(exc); state["updated_at"]=_iso(_now()); _save(state); return state

def summary(state=None):
    state=state or _load(); pos=state.get("positions",{}) or {}; closed=state.get("closed_trades",[]) or []; realized=sum(_num(x.get("closed_profit_sda")) for x in closed); open_pnl=0
    for x in pos.values():
        e=_num(x.get("entry_price")); p=_num(x.get("last_price"),e); inv=_num(x.get("investment_sda"),INVESTMENT_SDA)
        if e>0: open_pnl += inv*(p/e)*(1-SLIPPAGE_RATE)*(1-FEE_RATE)-inv*(1+FEE_RATE)
    total=realized+open_pnl; return {"open":len(pos),"closed":len(closed),"realized_pnl_sda":realized,"open_pnl_sda":open_pnl,"total_pnl_sda":total,"roi_pct":total/(INVESTMENT_SDA*max(1,len(closed)+len(pos)))*100,"events":state.get("events",[])}
