"""V26 self-learning pump hunter.

Paper-only strategy overlay. It learns from scan-to-scan changes, not just
absolute score. Objective: detect early pump pressure, capture asymmetric
upside and exit on confirmed breakdown rather than fixed take-profit caps.
"""
import json, os, math
from datetime import datetime, timezone
STATE_FILE="v26_pump_state.json"; SL_PCT=.07; MAX_OPEN=6; MAX_BUYS_PER_RUN=2; ENTRY_SCORE=62.; WATCH_SCORE=45.
def _num(v,d=0.):
    try:return d if v is None else float(v)
    except:return d
def _now():return datetime.now(timezone.utc).isoformat()
def _load():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,dict) else {}
    except:return {}
def _save(x):
    t=STATE_FILE+".tmp"
    try:
        with open(t,"w",encoding="utf-8") as f:json.dump(x,f,indent=2,ensure_ascii=False)
        os.replace(t,STATE_FILE)
    except:pass
def _flow(a,w="1h"):
    f=(a.get("flow",{}) or {}).get(w,{}) if isinstance(a,dict) else {}
    return f if isinstance(f,dict) else {}
def _metrics(a):
    m=a.get("momentum",{}) or {};f1=_flow(a);f15=_flow(a,"15m");bv=_num(f1.get("buy_volume"));sv=_num(f1.get("sell_volume"));bc=_num(f1.get("buy_count"));sc=_num(f1.get("sell_count"))
    return {"price":_num(a.get("price_in_sda")),"m1":_num(m.get("1h_pct")),"m15":_num(m.get("15m_pct")),"m4":_num(m.get("4h_pct")),"flow":_num(f1.get("net_flow")),"flow15":_num(f15.get("net_flow")),"vol":bv+sv,"trades":bc+sc,"buy_ratio":bv/max(sv,1.),"trade_ratio":bc/max(sc,1.),"vol_accel":_num(a.get("volume_acceleration_15m_pct"))}
def _delta(c,p,k):return _num(c.get(k))-_num(p.get(k)) if p else 0.
def _score(address,analysis):
    state=_load();hist=state.setdefault("history",{});key=str(address).lower();cur=_metrics(analysis);prev=hist.get(key,{}).get("last",{});old=hist.get(key,{})
    if prev and all(abs(_num(cur.get(k))-_num(prev.get(k)))<1e-12 for k in cur):return cur,_num(old.get("last_score")),_num(old.get("last_change")),str(old.get("phase") or "NO")
    accel=max(0,_delta(cur,prev,"m1"))*2+max(0,_delta(cur,prev,"m15"))*1.5;flow_imp=max(0,_delta(cur,prev,"flow"))/max(50,abs(_num(prev.get("flow")))+50)*100;vm=cur["vol"]/max(100,_num(prev.get("vol"),100));tm=cur["trades"]/max(3,_num(prev.get("trades"),3))
    pressure=min(30,max(0,cur["m1"]*1.5+max(0,cur["m15"])*.7))+min(22,max(0,(cur["buy_ratio"]-.9)*22))+min(18,max(0,cur["flow"]/500*6))+min(15,max(0,math.log(max(1,vm))*10+math.log(max(1,tm))*5))+min(8,max(0,cur["vol_accel"]/10))+min(7,max(0,cur["flow15"]/300*7))
    change=min(20,max(0,accel+flow_imp*.5+max(0,vm-1)*5+max(0,tm-1)*3));score=max(0,min(100,pressure+change))
    if cur["flow"]<0:score-=12
    if cur["buy_ratio"]<.8:score-=10
    if cur["m1"]<0 and cur["m15"]<0:score-=12
    score=max(0,min(100,score));phase="ENTRY" if prev and score>=ENTRY_SCORE and change>=7 and cur["flow"]>0 and cur["m1"]>0 else ("WATCH" if score>=WATCH_SCORE else "NO")
    hist[key]={"last":cur,"last_score":round(score,2),"last_change":round(change,2),"phase":phase,"updated_at":_now()};state["updated_at"]=_now();_save(state);return cur,round(score,1),round(change,1),phase
def decision(address,analysis,whale_state=None):
    cur,score,change,phase=_score(address,analysis)
    # Compatibility fields below are intentionally kept because engine_legacy
    # still renders them while V26 replaces its decision policy.
    return {"score":score,"confidence":score,"buy_score":score,"market_score":score,"m1h":cur["m1"],"m15":cur["m15"],"m4h":cur["m4"],"net_1h":cur["flow"],"whale_net":cur["flow"],"trades_1h":cur["trades"],"volume_1h":cur["vol"],"buy_ratio":cur["buy_ratio"],"trade_ratio":cur["trade_ratio"],"eligible_for_buy":cur["trades"]>0,"pump_score":score,"pump_change":change,"pump_phase":phase,"paper_buy_blocked":phase!="ENTRY","paper_buy_block_reason":"PUMP ENTRY: accelerating flow/volume/activity" if phase=="ENTRY" else f"pump phase {phase}; score {score:.0f}, acceleration +{change:.1f}","paper_prediction":{"ready":False,"role":"advisory"},"paper_prediction_role":"advisory","technical_bull":0,"technical_bear":0,"technical_evidence":[],"buy_score_components":{"momentum":round(min(100,max(0,cur["m1"]*5+50)),1),"flow":round(min(100,max(0,50+cur["flow"]/20)),1),"activity":round(min(100,max(0,40+cur["trades"]*2)),1),"prediction":0.,"liquidity":0.},"v23_prediction":{"ready":False},"v24":{"version":"V24","ready":False},"v25":{"version":"V25","ready":False,"learning_mode":"shadow"},"v26":{"version":"V26-PUMP-HUNTER","score":score,"change":change,"phase":phase,"metrics":cur}}
def patch(main_module,engine_module):
    original_create=getattr(engine_module,"create",None);legacy=getattr(engine_module,"_legacy",None);targets=[engine_module]+([legacy] if legacy is not None else [])
    def paper_decision(address,analysis,ws):
        d=decision(address,analysis,ws);return {**d,"data":d,"prediction":d["paper_prediction"],"blocked":d["paper_buy_blocked"],"reason":d["paper_buy_block_reason"],"score_band":"PUMP ENTRY" if not d["paper_buy_blocked"] else d["pump_phase"]}
    def score(address,analysis,ws):return decision(address,analysis,ws)
    def create(a,an,s,meta,liq,investment=None):
        z=original_create(a,an,s,meta,liq,investment) if callable(original_create) else {};e=_num(z.get("entry_price"),_num(an.get("price_in_sda")));z.update({"v26_mode":"PUMP-HUNTER","pump_peak_price":e,"pump_mfe_pct":0.,"pump_weak_count":0,"pump_age_scans":0,"sl_pct":SL_PCT,"tp1_pct":9.99,"tp2_pct":9.99,"trail_pct":0.,"sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),"tp1":e*10.99,"tp2":e*10.99,"risk_profile":"V26-PUMP","entry_pump_score":_num(s.get("pump_score",s.get("confidence"))),"entry_pump_change":_num(s.get("pump_change")),"entry_phase":s.get("pump_phase","ENTRY")});return z
    def auto_exit(p,tokens,ws):
        events=[]
        for address in list((p.get("positions") or {}).keys()):
            pos=p["positions"].get(address);td=(tokens or {}).get(address,{}) or {};analysis=td.get("analysis",td) if isinstance(td,dict) else {};current=_num(analysis.get("price_in_sda"))
            if not pos or current<=0:continue
            s=decision(address,analysis,ws);score=_num(s.get("pump_score"));entry=_num(pos.get("entry_price"));roi=(current-entry)/entry*100 if entry else 0;peak=max(_num(pos.get("pump_peak_price"),entry),current);pos["pump_peak_price"]=peak;pos["pump_mfe_pct"]=(peak-entry)/entry*100 if entry else 0;pos["pump_age_scans"]=int(_num(pos.get("pump_age_scans"))+1)
            if roi>=15:pos["sl"]=max(_num(pos.get("sl")),peak*.90)
            if roi>=30:pos["sl"]=max(_num(pos.get("sl")),peak*.915)
            if roi>=50:pos["sl"]=max(_num(pos.get("sl")),peak*.93)
            weak=(s.get("m1h",0)<=0 and s.get("net_1h",0)<=0) or score<40;pos["pump_weak_count"]=int(_num(pos.get("pump_weak_count"))+1 if weak else max(0,_num(pos.get("pump_weak_count"))-1));stop=_num(pos.get("sl"))
            if current<=stop and roi<=-SL_PCT*100:
                r=engine_module.close(p,address,current,"V26 HARD STOP")
                if r:events.append(f"🔴 V26 STOP {r['label']} | ROI {roi:+.2f}% | score {score:.0f}")
            elif roi>0 and pos["pump_weak_count"]>=3:
                r=engine_module.close(p,address,current,"V26 PUMP BREAKDOWN")
                if r:events.append(f"🟠 V26 PUMP EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | score {score:.0f}")
        return events
    main_module.paper_decision=paper_decision
    for target in targets:
        if getattr(target,"_v26_engine_patched",False):continue
        target.score=score;target.create=create;target.BUY_THRESHOLD=ENTRY_SCORE;target.MAX_OPEN_POSITIONS=MAX_OPEN;target.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN;target.SL_PCT=SL_PCT;target._auto_exit=auto_exit;target._v26_engine_patched=True
    if hasattr(main_module,"engine") and not getattr(main_module.engine,"_v26_engine_patched",False):
        main_module.engine.score=score;main_module.engine.create=create;main_module.engine.BUY_THRESHOLD=ENTRY_SCORE;main_module.engine.MAX_OPEN_POSITIONS=MAX_OPEN;main_module.engine.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN;main_module.engine.SL_PCT=SL_PCT;main_module.engine._auto_exit=auto_exit;main_module.engine._v26_engine_patched=True
    main_module._v26_patched=True
