"""V28 adaptive pump hunter for paper trading.

V28 keeps the asymmetric "let winners run" idea but changes the weak point
identified in the V27 trade history: too many low-quality entries and oversized
hard-stop losses. Entry is now a two-stage momentum/flow trigger; exits adapt
to early weakness, stale positions and realized pump strength.

Paper-only. Real wallet remains read-only.
"""
import json, os, math
from datetime import datetime, timezone, timedelta

STATE_FILE="v28_pump_state.json"
_RUNTIME_STATE=None
SL_PCT=.04
MAX_OPEN=5
MAX_BUYS_PER_RUN=1
ENTRY_SCORE=72.
WATCH_SCORE=48.
ENTRY_CHANGE=5.
MIN_M15=-1.5
MIN_TRADES=5
MIN_VOLUME=750.
MAX_VOL_ACCEL_DROP=-100.
COOLDOWN_HOURS=6.

EARLY_SL_PCT=.025
EARLY_WEAK_COUNT=2
STALE_HOURS=2.5
STALE_MFE_PCT=2.
STALE_WEAK_COUNT=4

TRAIL_5=.97
TRAIL_10=.95
TRAIL_20=.92
TRAIL_40=.90
TRAIL_70=.88

def _num(v,d=0.):
    try:return d if v is None else float(v)
    except:return d

def _now():return datetime.now(timezone.utc).isoformat()

def _load():
    global _RUNTIME_STATE
    if isinstance(_RUNTIME_STATE,dict):
        return _RUNTIME_STATE
    try:
        with open("positions.json",encoding="utf-8") as f:
            p=json.load(f)
        s=p.get("_v28_state") if isinstance(p,dict) else None
        if isinstance(s,dict):
            _RUNTIME_STATE=s
            return s
    except Exception:
        pass
    try:
        with open(STATE_FILE,encoding="utf-8") as f:
            x=json.load(f)
        if isinstance(x,dict):
            _RUNTIME_STATE=x
            return x
    except Exception:
        pass
    _RUNTIME_STATE={}
    return _RUNTIME_STATE

def _save(x):
    global _RUNTIME_STATE
    _RUNTIME_STATE=x
    try:
        tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(x,f,indent=2,ensure_ascii=False)
        os.replace(tmp,STATE_FILE)
    except Exception:
        pass

def _flow(a,w="1h"):
    f=(a.get("flow",{}) or {}).get(w,{}) if isinstance(a,dict) else {}
    return f if isinstance(f,dict) else {}

def _metrics(a):
    m=a.get("momentum",{}) or {}
    f1=_flow(a); f15=_flow(a,"15m")
    bv=_num(f1.get("buy_volume")); sv=_num(f1.get("sell_volume"))
    bc=_num(f1.get("buy_count")); sc=_num(f1.get("sell_count"))
    return {
        "price":_num(a.get("price_in_sda")),
        "m1":_num(m.get("1h_pct")),
        "m15":_num(m.get("15m_pct")),
        "m4":_num(m.get("4h_pct")),
        "flow":_num(f1.get("net_flow")),
        "flow15":_num(f15.get("net_flow")),
        "vol":bv+sv,
        "trades":bc+sc,
        "buy_ratio":bv/max(sv,1.),
        "trade_ratio":bc/max(sc,1.),
        "vol_accel":_num(a.get("volume_acceleration_15m_pct")),
    }

def _delta(c,p,k):
    return _num(c.get(k))-_num(p.get(k)) if p else 0.

def _cooldown_until(state,key):
    return str((state.get("cooldowns") or {}).get(key) or "")

def _cooldown_active(state,key):
    raw=_cooldown_until(state,key)
    if not raw:return False
    try:return datetime.fromisoformat(raw.replace("Z","+00:00"))>datetime.now(timezone.utc)
    except:return False

def _score(address,analysis):
    state=_load(); state["strategy_version"]="V28.3"; hist=state.setdefault("history",{})
    key=str(address).lower(); cur=_metrics(analysis)
    prev=hist.get(key,{}).get("last",{}); old=hist.get(key,{})
    if prev and all(abs(_num(cur.get(k))-_num(prev.get(k)))<1e-12 for k in cur):
        return cur,_num(old.get("last_score")), _num(old.get("last_change")), str(old.get("phase") or "NO")

    # Base quality: positive short-term momentum + buying pressure + activity.
    momentum=min(32,max(0,cur["m1"]*1.7+max(0,cur["m15"])*1.2))
    flow=min(22,max(0,cur["flow"]/450*10+cur["flow15"]/300*5))
    buy=min(16,max(0,(cur["buy_ratio"]-1)*8))
    activity=min(12,max(0,math.log(max(1,cur["vol"]/300))*4+math.log(max(1,cur["trades"]/3))*4))
    accel=min(8,max(0,cur["vol_accel"]/20+4))
    trend=min(6,max(0,cur["m4"]*.5))
    quality=momentum+flow+buy+activity+accel+trend

    # Trigger = new pressure, not merely a high absolute score.
    d1=max(0,_delta(cur,prev,"m1"))
    d15=max(0,_delta(cur,prev,"m15"))
    df=max(0,_delta(cur,prev,"flow"))
    dv=max(0,_delta(cur,prev,"vol"))/max(100,_num(prev.get("vol"),100))
    dt=max(0,_delta(cur,prev,"trades"))/max(3,_num(prev.get("trades"),3))
    change=min(20,max(0,d1*1.8+d15*1.2+df/max(50,abs(_num(prev.get("flow")))+50)*4+dv*8+dt*3))

    score=quality+change
    if cur["flow"]<0:score-=14
    if cur["buy_ratio"]<1.:score-=10
    if cur["m1"]<0 and cur["m15"]<0:score-=14
    if cur["m15"]<-3:score-=6
    if cur["vol_accel"]<=-100:score-=3
    score=max(0,min(100,score))

    trigger=(
        prev and score>=ENTRY_SCORE and change>=ENTRY_CHANGE
        and cur["flow"]>0 and cur["flow15"]>=0
        and cur["m1"]>0 and cur["m15"]>=MIN_M15
        and cur["trades"]>=MIN_TRADES and cur["vol"]>=MIN_VOLUME
        and cur["buy_ratio"]>=1.15
        and (cur["vol_accel"]>MAX_VOL_ACCEL_DROP or (cur["m1"]>=4 and cur["flow"]>0 and cur["buy_ratio"]>=1.5))
    )
    phase="ENTRY" if trigger else ("WATCH" if score>=WATCH_SCORE else "NO")
    hist[key]={"last":cur,"last_score":round(score,2),"last_change":round(change,2),
               "phase":phase,"updated_at":_now(),
               "impulse_components":{
                   "d1h_pct":round(d1,4),"d15m_pct":round(d15,4),
                   "flow_delta":round(df,4),"volume_ratio_delta":round(dv,4),
                   "trade_ratio_delta":round(dt,4)
               }}
    state["updated_at"]=_now(); _save(state)
    state["updated_at"]=_now(); _save(state)
    return cur,round(score,1),round(change,1),phase

def decision(address,analysis,whale_state=None):
    cur,score,change,phase=_score(address,analysis)
    state=_load(); key=str(address).lower()
    cooldown=_cooldown_active(state,key)
    blocked=phase!="ENTRY" or cooldown
    if cooldown:
        reason=f"V28 cooldown until {_cooldown_until(state,key)}"
    elif phase=="ENTRY":
        reason="V28 PUMP ENTRY: momentum + flow + activity + acceleration gates passed"
    else:
        reason=f"V28 {phase}; score {score:.0f}, trigger +{change:.1f}"
    return {
        "score":score,"confidence":score,"buy_score":score,"market_score":score,
        "m1h":cur["m1"],"m15":cur["m15"],"m4h":cur["m4"],
        "net_1h":cur["flow"],"whale_net":cur["flow"],
        "trades_1h":cur["trades"],"volume_1h":cur["vol"],
        "buy_ratio":cur["buy_ratio"],"trade_ratio":cur["trade_ratio"],
        "eligible_for_buy":cur["trades"]>0,
        "pump_score":score,"pump_change":change,"pump_phase":phase,
        "paper_buy_blocked":blocked,"paper_buy_block_reason":reason,
        "paper_prediction":{"ready":False,"role":"advisory"},
        "paper_prediction_role":"advisory",
        "technical_bull":0,"technical_bear":0,"technical_evidence":[],
        "buy_score_components":{
            "momentum":round(min(100,max(0,50+cur["m1"]*5)),1),
            "flow":round(min(100,max(0,50+cur["flow"]/20)),1),
            "activity":round(min(100,max(0,40+cur["trades"]*2)),1),
            "prediction":0.,"liquidity":0.
        },
        "v23_prediction":{"ready":False},
        "v24":{"version":"V24","ready":False},
        "v25":{"version":"V25","ready":False,"learning_mode":"shadow"},
        "v26":{"version":"V26-PUMP-HUNTER","score":score,"change":change,
               "phase":phase,"metrics":cur,"cooldown":cooldown},
        "v27":{"version":"V27-PROFIT-LAYER","paper_only":True},
        "v28":{
            "version":"V28-ADAPTIVE-PUMP-HUNTER","paper_only":True,
            "entry_score":ENTRY_SCORE,"entry_change":ENTRY_CHANGE,
            "hard_stop_pct":SL_PCT,"early_sl_pct":EARLY_SL_PCT,
            "stale_hours":STALE_HOURS,
            "trail_5":1-TRAIL_5,"trail_10":1-TRAIL_10,
            "trail_20":1-TRAIL_20,"trail_40":1-TRAIL_40,
            "trail_70":1-TRAIL_70
        }
    }

def _position_age_hours(pos):
    raw=str(pos.get("opened_at") or pos.get("created_at") or pos.get("entry_time") or "")
    if not raw:return 0.
    try:
        dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        return max(0,(datetime.now(timezone.utc)-dt).total_seconds()/3600)
    except:return 0.

def _market_debug(dashboard,snapshot=None):
    md=dashboard.load("market_data.json",{"tokens":{}}) if snapshot is None else snapshot.get("md",{})
    ws=dashboard.load("whale_data.json",{}) if snapshot is None else snapshot.get("ws",{})
    meta=dashboard.load("token_metadata.json",{}) if snapshot is None else snapshot.get("meta",{})
    tokens=md.get("tokens",{}) if isinstance(md,dict) else {}
    rows=[]
    for address,td in tokens.items():
        analysis=td.get("analysis",td) if isinstance(td,dict) else {}
        if not isinstance(analysis,dict) or _num(analysis.get("price_in_sda"))<=0:continue
        cur=_metrics(analysis)
        if cur["vol"]<250:continue
        d=decision(address,analysis,ws)
        rows.append({"address":address,"label":dashboard.engine.lbl(address,meta),"d":d,"m":cur})
    rows.sort(key=lambda r:(_num(r["d"].get("pump_score")),r["m"]["vol"],r["m"]["trades"]),reverse=True)
    rows=rows[:5]
    ready=sum(1 for r in rows if r["d"].get("pump_phase")=="ENTRY" and not r["d"].get("paper_buy_blocked"))
    lines=[
        "🐞 MARKET DEBUG • V28.3 ADAPTIVE PUMP HUNTER","",
        f"Loaded tokens: {len(tokens)}",
        "V28: quality + acceleration entry; asymmetric exit; paper-only",
        f"ENTRY: score ≥ {ENTRY_SCORE:.0f} • real impulse ≥ +{ENTRY_CHANGE:.0f} • M15 ≥ {MIN_M15:.1f}%",
        f"Activity: trades ≥ {MIN_TRADES} • volume ≥ {MIN_VOLUME:.0f} SDA • buy/sell ≥ 1.15 • vol accel = confirmation • history persisted",
        f"Risk: max {MAX_OPEN} open • max {MAX_BUYS_PER_RUN}/scan • hard stop -{SL_PCT*100:.0f}% • cooldown {COOLDOWN_HOURS:.0f}h",
        f"Exit: early -{EARLY_SL_PCT*100:.1f}% • stale {STALE_HOURS:.1f}h • trails 5/10/20/40/70 = 3/5/8/10/12%",
        "────────────────────────",f"🚀 V28 ENTRY READY in TOP {len(rows)}: {ready}",
        "","🎯 TOP V28 CANDIDATES","────────────────────────"
    ]
    if not rows:lines.append("⚪ No active candidates")
    for i,r in enumerate(rows,1):
        d=r["d"];m=r["m"];score=_num(d.get("pump_score"));change=_num(d.get("pump_change"))
        phase=str(d.get("pump_phase") or "NO")
        status="🟢 ENTRY READY" if phase=="ENTRY" and not d.get("paper_buy_blocked") else ("🟡 WATCH" if phase=="WATCH" else "🔴 NO ENTRY")
        lines += [
            f"{i}. {r['label']} • {status} • score {score:.0f}/100 • impulse +{change:.1f}",
            f"   M15/M1H/M4H {m['m15']:+.1f}%/{m['m1']:+.1f}%/{m['m4']:+.1f}% • flow {m['flow']:+.0f} SDA • vol {m['vol']:.0f} • trades {m['trades']:.0f}",
            f"   buy/sell {m['buy_ratio']:.2f} • trade ratio {m['trade_ratio']:.2f} • vol accel {m['vol_accel']:+.1f}%"
        ]
    return "\n".join(lines)

def patch(main_module,engine_module):
    global _RUNTIME_STATE
    _original_engine_main=getattr(engine_module,"main",None)

    # IMPORTANT: engine.main() ultimately resolves save() from engine_legacy.
    # Persisting V28 state only after main() returned was fragile because another
    # save() path could overwrite positions.json. Hook the canonical save path
    # instead, so every normal positions.json write carries the latest V28 state.
    original_saves={}
    for _target in [engine_module, getattr(engine_module,"_legacy",None)]:
        if _target is None or getattr(_target,"_v28_save_patched",False):
            continue
        _orig_save=getattr(_target,"save",None)
        if not callable(_orig_save):
            continue
        original_saves[id(_target)]=(_target,_orig_save)
        def _v28_save(filename,data,_orig_save=_orig_save):
            global _RUNTIME_STATE
            if str(filename)==str(getattr(engine_module,"POSITIONS_FILE","positions.json")) and isinstance(data,dict) and isinstance(_RUNTIME_STATE,dict):
                data=dict(data)
                data["_v28_state"]=_RUNTIME_STATE
            return _orig_save(filename,data)
        _target.save=_v28_save
        _target._v28_save_patched=True

    def _v28_main(*args,**kwargs):
        global _RUNTIME_STATE
        _RUNTIME_STATE=None
        result=_original_engine_main(*args,**kwargs)
        # Defensive final flush for callers that bypass the normal save path.
        try:
            with open(engine_module.POSITIONS_FILE,encoding="utf-8") as f:
                p=json.load(f)
            if isinstance(p,dict) and isinstance(_RUNTIME_STATE,dict):
                p["_v28_state"]=_RUNTIME_STATE
                tmp=engine_module.POSITIONS_FILE+".v28tmp"
                with open(tmp,"w",encoding="utf-8") as f:
                    json.dump(p,f,indent=2,ensure_ascii=False)
                os.replace(tmp,engine_module.POSITIONS_FILE)
        except Exception:
            pass
        return result
    if callable(_original_engine_main):
        engine_module.main=_v28_main
    original_create=getattr(engine_module,"create",None)
    legacy=getattr(engine_module,"_legacy",None)
    targets=[engine_module]+([legacy] if legacy is not None else [])

    def paper_decision(address,analysis,ws):
        d=decision(address,analysis,ws)
        return {**d,"data":d,"prediction":d["paper_prediction"],
                "blocked":d["paper_buy_blocked"],"reason":d["paper_buy_block_reason"],
                "score_band":"V28 PUMP ENTRY" if not d["paper_buy_blocked"] else d["pump_phase"]}

    def score(address,analysis,ws):return decision(address,analysis,ws)

    def create(a,an,s,meta,liq,investment=None):
        z=original_create(a,an,s,meta,liq,investment) if callable(original_create) else {}
        e=_num(z.get("entry_price"),_num(an.get("price_in_sda")))
        z.update({
            "v26_mode":"PUMP-HUNTER","v27_mode":"PROFIT-LAYER",
            "v28_mode":"ADAPTIVE-PUMP-HUNTER",
            "pump_peak_price":e,"pump_mfe_pct":0.,"pump_weak_count":0,
            "pump_age_scans":0,"sl_pct":SL_PCT,"tp1_pct":9.99,"tp2_pct":9.99,
            "trail_pct":0.,"sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),
            "tp1":e*10.99,"tp2":e*10.99,
            "risk_profile":"V28-ADAPTIVE-PUMP",
            "entry_pump_score":_num(s.get("pump_score",s.get("confidence"))),
            "entry_pump_change":_num(s.get("pump_change")),
            "entry_phase":s.get("pump_phase","ENTRY")
        })
        return z

    def auto_exit(p,tokens,ws):
        events=[]
        for address in list((p.get("positions") or {}).keys()):
            pos=p["positions"].get(address)
            td=(tokens or {}).get(address,{}) or {}
            analysis=td.get("analysis",td) if isinstance(td,dict) else {}
            current=_num(analysis.get("price_in_sda"))
            if not pos or current<=0:continue

            s=decision(address,analysis,ws)
            score=_num(s.get("pump_score"))
            entry=_num(pos.get("entry_price"))
            roi=(current-entry)/entry*100 if entry else 0
            peak=max(_num(pos.get("pump_peak_price"),entry),current)
            pos["pump_peak_price"]=peak
            pos["pump_mfe_pct"]=(peak-entry)/entry*100 if entry else 0
            pos["pump_age_scans"]=int(_num(pos.get("pump_age_scans"))+1)
            age_h=_position_age_hours(pos)

            if roi>=5:pos["sl"]=max(_num(pos.get("sl")),peak*TRAIL_5)
            if roi>=10:pos["sl"]=max(_num(pos.get("sl")),peak*TRAIL_10)
            if roi>=20:pos["sl"]=max(_num(pos.get("sl")),peak*TRAIL_20)
            if roi>=40:pos["sl"]=max(_num(pos.get("sl")),peak*TRAIL_40)
            if roi>=70:pos["sl"]=max(_num(pos.get("sl")),peak*TRAIL_70)

            weak=(s.get("m1h",0)<=0 and s.get("net_1h",0)<=0) or score<42
            if weak:
                pos["pump_weak_count"]=int(_num(pos.get("pump_weak_count"))+1)
            else:
                pos["pump_weak_count"]=max(0,int(_num(pos.get("pump_weak_count")))-1)

            stop=_num(pos.get("sl"))
            if current<=stop:
                hard=roi<=-SL_PCT*100
                reason="V28 HARD STOP" if hard else "V28 TRAILING STOP"
                r=engine_module.close(p,address,current,reason)
                if r:
                    if hard:
                        state=_load()
                        state.setdefault("cooldowns",{})[str(address).lower()]=(
                            datetime.now(timezone.utc)+timedelta(hours=COOLDOWN_HOURS)
                        ).isoformat()
                        _save(state)
                    events.append(f"🔴 {reason} {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | age {age_h:.1f}h | score {score:.0f}")
            elif roi<=-EARLY_SL_PCT*100 and pos["pump_weak_count"]>=EARLY_WEAK_COUNT and (
                score<42 or (s.get("m1h",0)<=0 and s.get("net_1h",0)<=0)
            ):
                r=engine_module.close(p,address,current,"V28 EARLY WEAKNESS")
                if r:events.append(f"🟠 V28 EARLY EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | weak {pos['pump_weak_count']} | score {score:.0f}")
            elif age_h>=STALE_HOURS and pos["pump_mfe_pct"]<STALE_MFE_PCT and pos["pump_weak_count"]>=STALE_WEAK_COUNT and roi<1.5:
                r=engine_module.close(p,address,current,"V28 STALE POSITION")
                if r:events.append(f"⚪ V28 STALE EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | age {age_h:.1f}h")
            elif roi>1.0 and pos["pump_mfe_pct"]>=3 and pos["pump_weak_count"]>=4:
                r=engine_module.close(p,address,current,"V28 PUMP BREAKDOWN")
                if r:events.append(f"🟠 V28 PUMP EXIT {r['label']} | ROI {roi:+.2f}% | MFE {pos['pump_mfe_pct']:+.2f}% | score {score:.0f}")
        return events

    main_module.paper_decision=paper_decision
    for target in targets:
        if getattr(target,"_v28_engine_patched",False):continue
        target.score=score;target.create=create
        target.BUY_THRESHOLD=ENTRY_SCORE
        target.MAX_OPEN_POSITIONS=MAX_OPEN
        target.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN
        target.SL_PCT=SL_PCT
        target._auto_exit=auto_exit
        target._v28_engine_patched=True
    if hasattr(main_module,"engine") and not getattr(main_module.engine,"_v28_engine_patched",False):
        main_module.engine.score=score;main_module.engine.create=create
        main_module.engine.BUY_THRESHOLD=ENTRY_SCORE
        main_module.engine.MAX_OPEN_POSITIONS=MAX_OPEN
        main_module.engine.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN
        main_module.engine.SL_PCT=SL_PCT
        main_module.engine._auto_exit=auto_exit
        main_module.engine._v28_engine_patched=True
    main_module._v28_patched=True
