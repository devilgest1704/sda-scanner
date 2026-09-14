"""V25 PUMP-HUNTER shadow paper portfolio with persistent WATCH tracking."""
from __future__ import annotations
import json,os
from datetime import datetime,timezone,timedelta
STATE_FILE="v25_learner_state.json";STATE_KEY="paper_hunter";INVESTMENT_SDA=50.;FEE_RATE=.01;SLIPPAGE_RATE=.001;MAX_OPEN=3;MAX_NEW_PER_SCAN=1;HORIZON_HOURS=6;MIN_SCORE=45.;MIN_VOLUME=250.;MIN_TRADES=5.;MIN_M1H=2.;MIN_EV_SDA=0.
def _num(v,d=0.):
 try:return d if v is None else float(v)
 except(TypeError,ValueError):return d
def _now():return datetime.now(timezone.utc)
def _iso(x):return x.astimezone(timezone.utc).isoformat()
def _empty():return {"version":"V25-PUMP-HUNTER","positions":{},"closed_trades":[],"watch":{},"watch_completed":[],"events":[]}
def _load():
 try:
  with open(STATE_FILE,encoding="utf-8") as f:x=json.load(f)
  return x.get(STATE_KEY,_empty()) if isinstance(x,dict) else _empty()
 except Exception:return _empty()
def _save(s):
 try:
  with open(STATE_FILE,encoding="utf-8") as f:b=json.load(f)
  if not isinstance(b,dict):b={}
 except Exception:b={}
 b[STATE_KEY]=s;tmp=STATE_FILE+".tmp"
 with open(tmp,"w",encoding="utf-8") as f:json.dump(b,f,indent=2,ensure_ascii=False)
 os.replace(tmp,STATE_FILE)
def _analysis(td):return td.get("analysis",td) if isinstance(td,dict) and isinstance(td.get("analysis",td),dict) else {}
def volume_1h_sda(an,data=None):
 """Return the scanner's canonical 1H traded SDA volume.

 The dashboard/scanner canonical source is flow.1h.total_volume.
 Prefer the explicit paper-decision volume when present, then canonical
 analysis.flow.1h.total_volume, and only then legacy buy/sell volume fields.
 """
 for src in (data,an):
  if not isinstance(src,dict):continue
  for key in ("volume_1h","volume1h","volume_1h_sda","one_hour_volume_sda"):
   if src.get(key) is not None:
    v=_num(src.get(key))
    if v>0:return v
  aa=_analysis(src)
  for key in ("volume_1h","volume1h","volume_1h_sda","one_hour_volume_sda"):
   if aa.get(key) is not None:
    v=_num(aa.get(key))
    if v>0:return v
  f=aa.get("flow",{}).get("1h",{}) if isinstance(aa,dict) else {}
  if isinstance(f,dict) and f.get("total_volume") is not None:
   return _num(f.get("total_volume"))
  if isinstance(f,dict):
   bv=f.get("buy_volume");sv=f.get("sell_volume")
   if bv is not None or sv is not None:return _num(bv)+_num(sv)
 return 0.
def _gate_reasons(score,volume,trades,m1,m15,m4,flow,bear):
 r=[]
 if score<MIN_SCORE:r.append(f"score {score:.0f}<{MIN_SCORE:.0f}")
 if volume<MIN_VOLUME:r.append(f"volume {volume:.0f}< {MIN_VOLUME:.0f}")
 if trades<MIN_TRADES:r.append(f"trades {trades:.0f}<{MIN_TRADES:.0f}")
 if m1<MIN_M1H:r.append(f"M1H {m1:+.1f}%<{MIN_M1H:+.1f}%")
 if m15<-.75:r.append(f"M15 {m15:+.1f}%<-0.75%")
 if m4<0:r.append(f"M4H {m4:+.1f}%<0")
 if flow<=0:r.append(f"flow {flow:+.0f}<=0")
 if bear!=0:r.append(f"bear {bear}>0")
 return r
def _candidate(scanner,a,an,whale):
 try:d=scanner.paper_decision(a,an,whale)
 except Exception:return None
 data=d.get("data") or {};v=d.get("v25") or data.get("v25") or {};f=an.get("flow",{}).get("1h",{}) if isinstance(an,dict) else {};score=_num(d.get("score",data.get("buy_score",data.get("confidence"))));volume=volume_1h_sda(data,an);trades=_num(data.get("trades_1h")) or _num(f.get("buy_count"))+_num(f.get("sell_count"));bear=int(d.get("technical_bear",data.get("technical_bear")) or 0);m1=_num(data.get("m1h"));m15=_num(data.get("m15"));m4=_num(data.get("m4h"));flow=_num(data.get("net_1h"));ev=_num(v.get("expected_pl_sda"));mfe=_num(v.get("expected_mfe"));mae=_num(v.get("expected_mae"));gate_reasons=_gate_reasons(score,volume,trades,m1,m15,m4,flow,bear);eligible=not gate_reasons
 return {"decision":d,"score":score,"v25":v,"volume":volume,"trades":trades,"bear":bear,"m1h":m1,"m15":m15,"m4h":m4,"flow":flow,"ev":ev,"mfe":mfe,"mae":mae,"gate_reasons":gate_reasons,"eligible":eligible,"core_blocked":bool(d.get("blocked"))}
def _close(row,p,reason,now):
 e=_num(row.get("entry_price"));inv=_num(row.get("investment_sda"),INVESTMENT_SDA);profit=inv*(p/e)*(1-SLIPPAGE_RATE)*(1-FEE_RATE)-inv*(1+FEE_RATE) if e>0 else 0
 return {**row,"closed_at":_iso(now),"final_price":p,"closed_profit_sda":round(profit,6),"closed_roi_pct":round(profit/inv*100 if inv else 0,6),"close_reason":reason}
def _track(row,p,now,c=None):
 e=_num(row.get("entry_price"),p);row["peak_price"]=max(_num(row.get("peak_price"),e),p);row["trough_price"]=min(_num(row.get("trough_price"),e),p);row["mfe_pct"]=(row["peak_price"]/e-1)*100 if e else 0;row["mae_pct"]=(row["trough_price"]/e-1)*100 if e else 0;row["last_price"]=p;row["last_seen_at"]=_iso(now);hits=row.setdefault("threshold_times",{});[hits.setdefault(str(t),_iso(now)) for t in (5,10,20,30) if row["mfe_pct"]>=t]
 if c:row.update({"last_score":c["score"],"last_ev":c["ev"],"last_m1h":c["m1h"],"last_flow":c["flow"],"last_volume_1h":c["volume"],"gate_reasons":c.get("gate_reasons",[])})
def update():
 try:
  import main as scanner
  pe=scanner._paper;md=pe.load(pe.MARKET_FILE,{"tokens":{}});tokens=md.get("tokens",{}) if isinstance(md,dict) else {};whale=pe.load(pe.WHALE_FILE,{});meta=pe.load(pe.META_FILE,{});s=_load();pos=s.setdefault("positions",{});closed=s.setdefault("closed_trades",[]);watch=s.setdefault("watch",{});wc=s.setdefault("watch_completed",[]);now=_now();events=[]
  for a,row in list(pos.items()):
   an=_analysis(tokens.get(a,{}));p=_num(an.get("price_in_sda"));
   if p<=0:continue
   c=_candidate(scanner,a,an,whale) or {};_track(row,p,now,c);roi=(p/_num(row.get("entry_price"))-1)*100 if _num(row.get("entry_price")) else 0;weak=c.get("score",0)<45 and c.get("m1h",0)<0 and c.get("flow",0)<0;row["weak_count"]=min(5,_num(row.get("weak_count"))+1) if weak else 0
   started=None
   try:started=datetime.fromisoformat(str(row.get("opened_at")).replace("Z","+00:00"))
   except Exception:pass
   reason="V25 EMERGENCY EXIT" if roi<=-15 and c.get("score",0)<35 and c.get("m1h",0)<0 and c.get("flow",0)<0 and c.get("bear",0)>0 else "V25 PARTIAL SELL" if roi>10 and row["weak_count"]>=2 and not row.get("partial_done") else "V25 TRAILING EXIT" if row.get("partial_done") and roi>20 and p<=row["peak_price"]*.92 else "V25 6H OBSERVATION EXIT" if started and now-started>=timedelta(hours=HORIZON_HOURS) else None
   if reason:
    if reason=="V25 PARTIAL SELL":half=dict(row);half["investment_sda"]=_num(row.get("investment_sda"),INVESTMENT_SDA)*.5;closed.append(_close(half,p,reason,now));row["investment_sda"]=half["investment_sda"];row["partial_done"]=True;row["protected"]=True;events.append(("PARTIAL",closed[-1]))
    else:closed.append(_close(row,p,reason,now));del pos[a];events.append(("SELL",closed[-1]))
  core=pe.load(pe.POSITIONS_FILE,{"positions":{}});corepos=core.get("positions",{}) if isinstance(core,dict) else {};cands=[]
  for raw,td in tokens.items():
   a=str(raw).lower()
   if a in pos or a in corepos:continue
   an=_analysis(td);p=_num(an.get("price_in_sda"));
   if p<=0:continue
   c=_candidate(scanner,a,an,whale)
   if not c or not c["eligible"] or (not c["core_blocked"] and c["score"]>=78):continue
   if c["ev"]<=MIN_EV_SDA:
    w=watch.get(a)
    if not w:w={"version":"V25-WATCH","address":a,"symbol":str(scanner.lbl(a,meta) if hasattr(scanner,"lbl") else a),"entry_at":_iso(now),"entry_price":p,"peak_price":p,"trough_price":p,"mfe_pct":0.,"mae_pct":0.,"threshold_times":{},"entry_score":c["score"],"entry_ev":c["ev"],"entry_prediction":c["v25"]};watch[a]=w
    _track(w,p,now,c);w["status"]="WATCH";w["reason"]="V25 EV <= 0";w["updated_at"]=_iso(now)
    try:started=datetime.fromisoformat(str(w.get("entry_at")).replace("Z","+00:00"))
    except Exception:started=None
    if started and now-started>=timedelta(hours=HORIZON_HOURS):
     w["completed_at"]=_iso(now);w["final_price"]=p;w["outcome"]={"mfe_pct":w["mfe_pct"],"mae_pct":w["mae_pct"],"final_roi_pct":(p/_num(w.get("entry_price"))-1)*100 if _num(w.get("entry_price")) else 0,"threshold_times":w.get("threshold_times",{})};wc.append(w);del watch[a]
   else:cands.append((c["score"]+min(15,max(0,c["mfe"]))*0.5+max(-10,min(10,c["ev"])),a,an,c,p))
  cands.sort(reverse=True,key=lambda x:x[0]);slots=max(0,MAX_OPEN-len(pos))
  for _,a,an,c,p in cands[:min(slots,MAX_NEW_PER_SCAN)]:
   label=str(scanner.lbl(a,meta) if hasattr(scanner,"lbl") else a);pos[a]={"version":"V25-PUMP-HUNTER","address":a,"symbol":label,"opened_at":_iso(now),"entry_price":p,"investment_sda":INVESTMENT_SDA,"peak_price":p,"trough_price":p,"mfe_pct":0,"mae_pct":0,"threshold_times":{},"partial_done":False,"protected":False,"weak_count":0,"entry_score":c["score"],"entry_metrics":c["decision"].get("data") or {},"v25_prediction":c["v25"],"core_blocked":c["core_blocked"],"gate_reasons":c.get("gate_reasons",[]),"entry_volume_1h":c["volume"]};watch.pop(a,None);events.append(("BUY",pos[a]))
  s.update({"version":"V25-PUMP-HUNTER","updated_at":_iso(now),"positions":pos,"closed_trades":closed[-500:],"watch":watch,"watch_completed":wc[-1000:],"events":events[-20:]});_save(s);return s
 except Exception as exc:
  s=_load();s["last_error"]=str(exc);s["updated_at"]=_iso(_now());_save(s);return s
def summary(state=None):
 s=state or _load();pos=s.get("positions",{});closed=s.get("closed_trades",[]);return {"open":len(pos),"closed":len(closed),"watch":len(s.get("watch",{})),"watch_completed":len(s.get("watch_completed",[])),"realized_pnl_sda":sum(_num(x.get("closed_profit_sda")) for x in closed)}
