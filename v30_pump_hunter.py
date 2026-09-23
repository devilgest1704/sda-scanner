"""V30 CLEAN PUMP HUNTER - paper trading only.
Single entry model: ignition -> confirmation -> breakout.
Entry and exit logic are intentionally separated.
"""
import json, os
from datetime import datetime, timezone, timedelta

VERSION="V30.0"
STATE_FILE="v30_pump_state.json"
SL_PCT=0.025
MAX_OPEN=5
MAX_BUYS_PER_RUN=1
ENTRY_SCORE=62.0
MIN_QUALITY=45.0
MIN_IMPULSE=3.0
COOLDOWN_HOURS=4.0

def n(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d

def now(): return datetime.now(timezone.utc).isoformat()

_RUNTIME=None
def load_state():
    global _RUNTIME
    if isinstance(_RUNTIME,dict): return _RUNTIME
    try:
        with open(STATE_FILE,encoding="utf-8") as f:x=json.load(f)
        _RUNTIME=x if isinstance(x,dict) else {}
    except Exception:
        _RUNTIME={}
        # Vercel's serverless filesystem is not durable. For read-only
        # dashboard decisions, fall back to the scanner's checkpointed V30
        # state in the public repository so impulse survives deployment swaps.
        if not os.path.exists(STATE_FILE):
            try:
                import urllib.request
                url="https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/v30_pump_state.json"
                with urllib.request.urlopen(url,timeout=4) as r:
                    remote=json.loads(r.read().decode("utf-8"))
                if isinstance(remote,dict): _RUNTIME=remote
            except Exception:
                pass
    return _RUNTIME

def save_state(s):
    global _RUNTIME
    _RUNTIME=s
    try:
        tmp=STATE_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(s,f,indent=2,ensure_ascii=False)
        os.replace(tmp,STATE_FILE)
    except Exception:pass

def flow(a,w="1h"):
    x=(a.get("flow",{}) or {}).get(w,{}) if isinstance(a,dict) else {}
    return x if isinstance(x,dict) else {}

def metrics(a):
    m=a.get("momentum",{}) or {}
    f=flow(a); f15=flow(a,"15m")
    bv=n(f.get("buy_volume")); sv=n(f.get("sell_volume"))
    bc=n(f.get("buy_count")); sc=n(f.get("sell_count"))
    return {
      "price":n(a.get("price_in_sda")),
      "m1":n(m.get("1h_pct")),"m15":n(m.get("15m_pct")),"m4":n(m.get("4h_pct")),
      "flow":n(f.get("net_flow")),"flow15":n(f15.get("net_flow")),
      "vol":bv+sv,"trades":bc+sc,"buy_ratio":bv/max(sv,1.0),
      "trade_ratio":bc/max(sc,1.0),"accel":n(a.get("volume_acceleration_15m_pct"))
    }

def score(address,a,persist=True):
    st=load_state(); hist=st.setdefault("history",{}); key=str(address).lower()
    c=metrics(a); rec=hist.get(key,{})
    # Keep the sample before the latest scanner observation separately.
    # Dashboard reads are read-only and must not erase impulse history.
    p=rec.get("prev") or rec.get("last",{})
    d1=max(0,c["m1"]-n(p.get("m1"))); d15=max(0,c["m15"]-n(p.get("m15")))
    df=c["flow"]-n(p.get("flow")); dv=max(0,c["vol"]-n(p.get("vol")))
    # Quality rewards early momentum, fresh flow and real activity, not absolute 1h spike alone.
    momentum=max(0,min(28,c["m1"]*1.8+max(0,c["m15"])*1.5))
    flow_score=max(0,min(24,c["flow"]/400*8+c["flow15"]/200*5))
    pressure=max(0,min(18,(c["buy_ratio"]-1)*10))
    activity=max(0,min(14,(c["trades"]/10)*7+(c["vol"]/1000)*7))
    acceleration=max(0,min(10,c["accel"]/20+5))
    quality=momentum+flow_score+pressure+activity+acceleration
    impulse=min(20,max(0,d1*2+d15*1.5+max(0,df)/250+min(1,dv/max(n(p.get("vol"),100),100))*5))
    ratio=c["flow"]/max(c["vol"],1)
    price_flow_ok=not(c["m1"]>=12 and ratio<0.18)
    # Three lifecycle lanes.
    ignition=(0<c["m1"]<=8 and c["m15"]>=0.3 and c["flow15"]>=40 and ratio>=0.20)
    confirmation=(8<c["m1"]<=18 and c["m15"]>=1.0 and c["m4"]>=-0.5 and ratio>=0.22)
    breakout=(18<c["m1"]<=35 and c["m15"]>=2.0 and c["m4"]>=0.5 and d15>=0.3 and ratio>=0.30)
    lane="IGNITION" if ignition else ("CONFIRMATION" if confirmation else ("BREAKOUT" if breakout else "NO"))
    fresh=bool(p) and (d1>=0.35 or d15>=0.15) and (df>=50 or dv>0)
    # Liquidity gate: keep the hard 500 SDA baseline, but allow smaller
    # early pumps when there is enough real trade activity and flow. This
    # avoids rejecting candidates such as a 400-500 SDA move solely because
    # absolute volume is still building.
    liquidity_ok=(c["vol"]>=500) or (
        c["vol"]>=300 and c["trades"]>=10 and c["flow"]>=100 and
        c["buy_ratio"]>=1.15 and ratio>=0.25
    )
    buy=(
      lane!="NO" and fresh and quality>=MIN_QUALITY and impulse>=MIN_IMPULSE
      and c["flow"]>0 and c["flow15"]>=0 and c["trades"]>=5 and liquidity_ok
      and c["buy_ratio"]>=1.10 and c["m15"]>=-0.25 and price_flow_ok
    )
    # Late spikes need materially stronger confirmation.
    if c["m1"]>18 and (c["m15"]<2 or c["flow15"]<=0): buy=False
    # Keep lifecycle phase visible even when a gate blocks the actual BUY.
    # The dashboard can then distinguish "confirmation but blocked" from
    # "not in a pump lane yet".
    phase=lane if lane!="NO" else ("WATCH" if quality>=40 and c["flow"]>0 else "NO")
    if persist:
        old=hist.get(key,{})
        hist[key]={"prev":old.get("last") or old.get("prev") or {},
                   "last":c,"score":round(min(100,quality+impulse),1),"quality":round(quality,1),
                   "impulse":round(impulse,1),"phase":phase,"updated_at":now()}
        st["updated_at"]=now();save_state(st)
    return c,round(min(100,quality+impulse),1),round(quality,1),round(impulse,1),phase

def cooldown_active(address):
    x=load_state().get("cooldowns",{}).get(str(address).lower())
    if not x:return False
    try:return datetime.fromisoformat(x.replace("Z","+00:00"))>datetime.now(timezone.utc)
    except:return False

def decision(address,a,ws=None,persist=True):
    c,s,q,i,phase=score(address,a,persist=persist)
    cd=cooldown_active(address)
    # Re-evaluate the same BUY gates here so dashboard and paper engine have
    # one source of truth. Do not reference local variables from score().
    ratio=c["flow"]/max(c["vol"],1)
    price_flow_ok=not(c["m1"]>=12 and ratio<0.18)
    lane_ok=phase in ("IGNITION","CONFIRMATION","BREAKOUT")
    liquidity_ok=(c["vol"]>=500) or (
        c["vol"]>=300 and c["trades"]>=10 and c["flow"]>=100 and
        c["buy_ratio"]>=1.15 and ratio>=0.25
    )
    fresh_hist=load_state().get("history",{}).get(str(address).lower(),{})
    p=fresh_hist.get("prev") or fresh_hist.get("last") or {}
    d1=max(0,c["m1"]-n(p.get("m1")))
    d15=max(0,c["m15"]-n(p.get("m15")))
    df=c["flow"]-n(p.get("flow"))
    dv=max(0,c["vol"]-n(p.get("vol")))
    fresh=bool(p) and (d1>=0.35 or d15>=0.15) and (df>=50 or dv>0)
    buy=(
        lane_ok and fresh and q>=MIN_QUALITY and i>=MIN_IMPULSE
        and c["flow"]>0 and c["flow15"]>=0 and c["trades"]>=5
        and liquidity_ok and c["buy_ratio"]>=1.10
        and c["m15"]>=-0.25 and price_flow_ok
    )
    if c["m1"]>18 and (c["m15"]<2 or c["flow15"]<=0):
        buy=False
    blocked=(not buy) or cd or s<ENTRY_SCORE
    failures=[]
    if not lane_ok: failures.append("no lifecycle lane")
    if not fresh: failures.append("no fresh impulse")
    if s<ENTRY_SCORE: failures.append(f"score {s:.0f}<{ENTRY_SCORE:.0f}")
    if q<MIN_QUALITY: failures.append(f"quality {q:.0f}<{MIN_QUALITY:.0f}")
    if i<MIN_IMPULSE: failures.append(f"impulse +{i:.1f}<{MIN_IMPULSE:.1f}")
    if c["flow"]<=0: failures.append("flow<=0")
    if c["flow15"]<0: failures.append("15m flow<0")
    if c["trades"]<5: failures.append(f"trades {c['trades']:.0f}<5")
    if not liquidity_ok: failures.append(f"liquidity vol {c['vol']:.0f}, flow {c['flow']:.0f}")
    if c["buy_ratio"]<1.10: failures.append(f"buy ratio {c['buy_ratio']:.2f}<1.10")
    if c["m15"]<-0.25: failures.append(f"M15 {c['m15']:+.2f}<-0.25%")
    if not price_flow_ok: failures.append("price/flow mismatch")
    if c["m1"]>18 and (c["m15"]<2 or c["flow15"]<=0): failures.append("late spike confirmation")
    if cd: failures.insert(0,"cooldown")
    reason="READY" if not blocked else f"{phase} / "+"; ".join(failures[:3])
    eff=s if not blocked else min(s,ENTRY_SCORE-1)
    return {"score":eff,"confidence":eff,"buy_score":eff,"market_score":eff,
      "m1h":c["m1"],"m15":c["m15"],"m4h":c["m4"],"net_1h":c["flow"],"whale_net":c["flow"],
      "trades_1h":c["trades"],"volume_1h":c["vol"],"buy_ratio":c["buy_ratio"],"trade_ratio":c["trade_ratio"],
      "eligible_for_buy":not blocked,"pump_score":s,"pump_quality":q,"pump_change":i,"pump_phase":phase,
      "paper_buy_blocked":blocked,"paper_buy_block_reason":reason,
      "paper_prediction":{"ready":False,"role":"advisory"},"paper_prediction_role":"advisory",
      "technical_bull":0,"technical_bear":0,"technical_evidence":[],
      "buy_score_components":{"momentum":round(c["m1"],1),"flow":round(c["flow"],1),"activity":round(c["trades"],1),
                              "prediction":0.0,"liquidity":0.0},
      "v30":{"version":VERSION,"entry_score":ENTRY_SCORE,"sl_pct":SL_PCT,"max_open":MAX_OPEN,
             "max_buys":MAX_BUYS_PER_RUN,"phase":phase}}

def age_hours(pos):
    raw=str(pos.get("opened_at") or "")
    try:
        d=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return max(0,(datetime.now(timezone.utc)-d).total_seconds()/3600)
    except:return 0

def patch(main_module,engine):
    global _RUNTIME
    original_main=getattr(engine,"main",None)
    original_create=getattr(engine,"create",None)
    # Ensure the clean strategy owns the actual paper engine decision.
    def create(a,an,s,meta,liq,investment=None):
        z=original_create(a,an,s,meta,liq,investment) if callable(original_create) else {}
        e=n(z.get("entry_price"),n(an.get("price_in_sda")))
        z.update({"v30_mode":"CLEAN-PUMP-HUNTER","pump_peak_price":e,"pump_mfe_pct":0.0,
                  "pump_weak_count":0,"pump_age_scans":0,"sl_pct":SL_PCT,
                  "sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),
                  "tp1_pct":0.99,"tp2_pct":0.99,"trail_pct":0.0,
                  "entry_pump_score":n(s.get("pump_score",s.get("confidence"))),
                  "entry_pump_change":n(s.get("pump_change")),"entry_phase":s.get("pump_phase","NO")})
        return z

    def auto_exit(p,tokens,ws):
        events=[]
        for address,pos in list((p.get("positions") or {}).items()):
            td=(tokens or {}).get(address,{}) or {}
            a=td.get("analysis",td) if isinstance(td,dict) else {}
            cur=n(a.get("price_in_sda"))
            if not pos or cur<=0:continue
            entry=n(pos.get("entry_price"))
            if entry<=0:continue
            s=decision(address,a,ws); roi=(cur-entry)/entry*100
            peak=max(n(pos.get("pump_peak_price"),entry),cur); pos["pump_peak_price"]=peak
            mfe=(peak-entry)/entry*100; pos["pump_mfe_pct"]=mfe
            pos["pump_age_scans"]=int(n(pos.get("pump_age_scans"))+1)
            age=age_hours(pos)
            # Profit protection: once positive, stop giving the trade back.
            stop=n(pos.get("sl"),entry*(1-SL_PCT))
            if mfe>=3: stop=max(stop,entry*1.005)
            if mfe>=5: stop=max(stop,peak*0.97)
            if mfe>=10: stop=max(stop,peak*0.95)
            if mfe>=20: stop=max(stop,peak*0.92)
            if mfe>=40: stop=max(stop,peak*0.90)
            if mfe>=70: stop=max(stop,peak*0.88)
            pos["sl"]=stop
            weak=(s["m15"]<0 and s["net_1h"]<=0) or s["pump_score"]<38
            pos["pump_weak_count"]=int(n(pos.get("pump_weak_count"))+1) if weak else max(0,int(n(pos.get("pump_weak_count")))-1)
            if (cur-entry)/entry*100<=-4:
                r=engine.close(p,address,cur,"V30 EMERGENCY/HARD STOP")
            elif cur<=stop:
                r=engine.close(p,address,cur,"V30 TRAILING/STOP")
            elif roi<=-2.5 and pos["pump_weak_count"]>=2:
                r=engine.close(p,address,cur,"V30 EARLY WEAKNESS")
            elif age>=3 and mfe<2 and pos["pump_weak_count"]>=4 and roi<1:
                r=engine.close(p,address,cur,"V30 STALE")
            elif roi>1 and mfe>=3 and pos["pump_weak_count"]>=4:
                r=engine.close(p,address,cur,"V30 PUMP BREAKDOWN")
            else:r=None
            if r:
                if "STOP" in str(r.get("close_reason","")) or "WEAK" in str(r.get("close_reason","")) or "STALE" in str(r.get("close_reason","")) or "BREAKDOWN" in str(r.get("close_reason","")):
                    st=load_state();st.setdefault("cooldowns",{})[str(address).lower()]=(datetime.now(timezone.utc)+timedelta(hours=COOLDOWN_HOURS)).isoformat();save_state(st)
                events.append(f"V30 {r.get('close_reason','EXIT')} {r.get('label',address)} | ROI {roi:+.2f}% | MFE {mfe:+.2f}%")
        return events

    def paper_decision(address,analysis,ws):
        d=decision(address,analysis,ws)
        return {**d,"data":d,"prediction":d["paper_prediction"],"blocked":d["paper_buy_blocked"],
                "reason":d["paper_buy_block_reason"],"score_band":d["pump_phase"]}

    engine.score=decision
    engine.create=create
    engine.BUY_THRESHOLD=ENTRY_SCORE
    engine.MAX_OPEN_POSITIONS=MAX_OPEN
    engine.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN
    engine.SL_PCT=SL_PCT
    engine._auto_exit=auto_exit
    main_module.paper_decision=paper_decision
    if callable(original_main):
        def wrapped_main(*args,**kwargs):
            _RUNTIME=None
            return original_main(*args,**kwargs)
        engine.main=wrapped_main
    main_module._v30_patched=True

def market_debug(dashboard,snapshot=None):
    md=dashboard.load("market_data.json",{"tokens":{}}) if snapshot is None else snapshot.get("md",{})
    tokens=md.get("tokens",{}) if isinstance(md,dict) else {}
    rows=[]
    for address,td in tokens.items():
        a=td.get("analysis",td) if isinstance(td,dict) else {}
        if not isinstance(a,dict) or n(a.get("price_in_sda"))<=0:continue
        d=decision(address,a)
        rows.append((d["pump_score"],address,d))
    rows.sort(reverse=True)
    return "\n".join([f"V30 CLEAN • loaded {len(tokens)} • ready {sum(1 for _,_,d in rows[:5] if not d['paper_buy_blocked'])}"]+
      [f"{i}. {d['pump_phase']} {a} score {d['pump_score']:.0f} impulse +{d['pump_change']:.1f} M15 {d['m15']:+.1f}% M1H {d['m1h']:+.1f}% flow {d['net_1h']:+.0f}" for i,(_,a,d) in enumerate(rows[:5],1)])
