"""V30 CLEAN PUMP HUNTER - paper trading only.
Single entry model: ignition -> confirmation -> breakout.
Entry and exit logic are intentionally separated.
"""
import json, os
from datetime import datetime, timezone, timedelta

VERSION="V30.8-UNIFIED-GATES"
STATE_FILE="v30_pump_state.json"
SL_PCT=0.025
MAX_OPEN=4
MAX_BUYS_PER_RUN=1
ENTRY_SCORE=68.0
MIN_QUALITY=52.0
MIN_IMPULSE=8.0
COOLDOWN_HOURS=6.0
MAX_ENTRY_DATA_AGE_SEC=900.0
MAX_EXIT_DATA_AGE_SEC=900.0
EXECUTION_STATE_FILE="v30_execution_state.json"
AGENT_CONFIG_FILE="v30_agent_config.json"

def load_agent_config():
    defaults={"entry_score":ENTRY_SCORE,"quality":MIN_QUALITY,"impulse":MIN_IMPULSE,"buy_ratio":1.10,"trades":5}
    try:
        with open(AGENT_CONFIG_FILE,encoding="utf-8") as f:
            x=json.load(f)
        cfg=x.get("filters",{}) if isinstance(x,dict) else {}
        return {k:n(cfg.get(k),v) for k,v in defaults.items()}
    except Exception:
        return defaults

def n(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d

def now(): return datetime.now(timezone.utc).isoformat()

def data_age_seconds(value):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return max(0.0,(datetime.now(timezone.utc)-dt).total_seconds())
    except Exception:
        return None

_RUNTIME=None
def load_state():
    global _RUNTIME
    if isinstance(_RUNTIME,dict): return _RUNTIME
    # Vercel deployments contain a frozen copy of repository JSON files. The
    # scanner checkpoints live V30 state back to GitHub every few minutes.
    # Always prefer that live checkpoint on Vercel so dashboard decisions do
    # not compare two identical frozen snapshots and report false STALE data.
    if os.environ.get("VERCEL") == "1":
        try:
            import urllib.request
            url="https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/v30_pump_state.json"
            with urllib.request.urlopen(url,timeout=4) as r:
                remote=json.loads(r.read().decode("utf-8"))
            if isinstance(remote,dict):
                _RUNTIME=remote
                return _RUNTIME
        except Exception:
            pass
    try:
        with open(STATE_FILE,encoding="utf-8") as f:x=json.load(f)
        _RUNTIME=x if isinstance(x,dict) else {}
    except Exception:
        _RUNTIME={}
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
      "trade_ratio":bc/max(sc,1.0),"accel":n(a.get("volume_acceleration_15m_pct")),
      "last_transaction":a.get("last_transaction"),
      "data_age_sec":data_age_seconds(a.get("last_transaction"))
    }

def score(address,a,persist=True):
    st=load_state(); hist=st.setdefault("history",{}); key=str(address).lower()
    c=metrics(a); rec=hist.get(key,{})
    # Keep the sample before the latest scanner observation separately.
    # Dashboard reads are read-only and must not erase impulse history.
    last=rec.get("last") or {}
    prev=rec.get("prev") or {}
    same_last=bool(last) and all(abs(n(c.get(k))-n(last.get(k)))<1e-9 for k in ("price","m1","m15","m4","flow","flow15","vol","trades"))
    p=(prev if same_last else last) if not persist else last
    # Repeated evaluation of the same market sample must not shift history.
    # Reuse the scanner's result, including its impulse, for dashboard reads.
    # Reuse cached scores only when the market snapshot AND transaction
    # timestamp are unchanged. A newer transaction must be evaluated.
    same_transaction=str(c.get("last_transaction") or "")==str(last.get("last_transaction") or "")
    if same_last and same_transaction and "impulse" in rec and persist:
        return c,n(rec.get("score")),n(rec.get("quality")),n(rec.get("impulse")),rec.get("phase","NO")
    d1=max(0,c["m1"]-n(p.get("m1"))); d15=max(0,c["m15"]-n(p.get("m15")))
    df=c["flow"]-n(p.get("flow")); dv=max(0,c["vol"]-n(p.get("vol")))
    # Quality rewards early momentum, fresh flow and real activity, not absolute 1h spike alone.
    momentum=max(0,min(28,c["m1"]*1.8+max(0,c["m15"])*1.5))
    flow_score=max(0,min(24,c["flow"]/400*8+c["flow15"]/200*5))
    pressure=max(0,min(18,(c["buy_ratio"]-1)*10))
    activity=max(0,min(14,(c["trades"]/10)*7+(c["vol"]/1000)*7))
    acceleration=max(0,min(10,c["accel"]/20+5))
    quality=momentum+flow_score+pressure+activity+acceleration
    raw_impulse=min(20,max(0,d1*2+d15*1.5+max(0,df)/250+min(1,dv/max(n(p.get("vol"),100),100))*5))
    # Shadow snapshot impulse: diagnostic forward signal only. It fixes the
    # delta=0 observability blind spot without changing Champion BUY eligibility.
    snapshot_impulse=min(20,max(0,
        max(0,c["m1"])*0.8 + max(0,c["m15"])*0.8 +
        max(0,c["flow15"])/100 + min(4,c["trades"]/5) +
        min(4,c["vol"]/250)
    )) if isinstance(c.get("data_age_sec"),(int,float)) and c["data_age_sec"]<=MAX_ENTRY_DATA_AGE_SEC else 0.0
    # Bootstrap a first observation after a worker restart. Without a prior
    # persisted sample, delta-based impulse is mathematically zero even when
    # the current market snapshot is already showing a real, liquid pump.
    # This remains subject to all normal lifecycle, quality, flow, liquidity,
    # buy-ratio and score gates.
    if not p:
        bootstrap=min(20,max(0,
            c["m1"]*1.2 + max(0,c["m15"])*0.8 +
            max(0,c["flow"])/250 + min(5,c["vol"]/250) +
            max(0,c["buy_ratio"]-1)*2
        ))
        raw_impulse=max(raw_impulse,bootstrap)
    # Momentum rebound without traded liquidity is not an actionable pump.
    # Keep raw impulse for diagnostics, but do not reward sparse/stale ticks.
    impulse=raw_impulse if (
        bool(p) and c["trades"]>=5 and c["vol"]>=100 and c["flow"]>0
        and c["m1"]>0 and c["m15"]>0
    ) else 0.0
    ratio=c["flow"]/max(c["vol"],1)
    price_flow_ok=not(c["m1"]>=12 and ratio<0.18)
    # Three lifecycle lanes.
    ignition=(0<c["m1"]<=8 and c["m15"]>=0.8 and c["flow15"]>=60 and c["m4"]>=-0.25 and ratio>=0.25)
    confirmation=(8<c["m1"]<=12 and c["m15"]>=1.5 and c["m4"]>=-0.25 and c["flow15"]>=40 and ratio>=0.25)
    breakout=(12<c["m1"]<=35 and c["m15"]>=2.0 and c["m4"]>=0.5 and d15>=0.3 and ratio>=0.30)
    lane="IGNITION" if ignition else ("CONFIRMATION" if confirmation else ("BREAKOUT" if breakout else "NO"))
    fresh=bool(p) and (d1>=0.35 or d15>=0.15 or df>=50 or dv>0)
    # Liquidity gate: keep the hard 500 SDA baseline, but allow smaller
    # early pumps when there is enough real trade activity and flow. This
    # avoids rejecting candidates such as a 400-500 SDA move solely because
    # absolute volume is still building.
    liquidity_ok=(c["vol"]>=500) or (
        c["vol"]>=300 and c["trades"]>=10 and c["flow"]>=100 and
        c["buy_ratio"]>=1.15 and ratio>=0.25
    )
    ignition_buy=(
      lane=="IGNITION" and quality>=48 and impulse>=8
      and c["flow"]>=150 and c["trades"]>=8 and c["m15"]>=0
      and liquidity_ok and c["buy_ratio"]>=1.10 and price_flow_ok
    )
    confirmation_buy=(
      lane=="CONFIRMATION" and fresh and quality>=52 and impulse>=5
      and c["flow"]>=125 and c["flow15"]>=25 and c["trades"]>=7 and liquidity_ok
      and c["buy_ratio"]>=1.12 and c["m15"]>=1.5 and price_flow_ok
    )
    # BREAKOUT is a holding/management phase, never a new entry.
    breakout_buy=False
    buy=ignition_buy or confirmation_buy
    # Late spikes need materially stronger confirmation.
    if c["m1"]>12: buy=False
    # Keep lifecycle phase visible even when a gate blocks the actual BUY.
    # The dashboard can then distinguish "confirmation but blocked" from
    # "not in a pump lane yet".
    early_watch=(impulse>=4 and c["m1"]>0 and c["m15"]>0 and c["flow"]>0 and c["trades"]>=3)
    phase=lane if lane!="NO" else ("WATCH" if early_watch or (quality>=40 and c["flow"]>0) else "NO")
    # Diagnostic-only sample status; never changes BUY/SELL eligibility.
    if same_last:
        sample_status="STALE"
    elif impulse>=4:
        sample_status="IMPULSE"
    elif (c["m1"]>=8 or c["m15"]>=5 or c["flow"]>=500) and not fresh:
        sample_status="MATURE"
    elif fresh:
        sample_status="FRESH"
    else:
        sample_status="UNKNOWN"
    if persist:
        old=hist.get(key,{})
        hist[key]={"prev":old.get("last") or old.get("prev") or {},
                   "last":c,"score":round(min(100,quality+impulse),1),"quality":round(quality,1),
                   "impulse":round(impulse,1),"raw_impulse":round(raw_impulse,1),"fresh":fresh,"sample_status":sample_status,"phase":phase,
                   "last_transaction":c.get("last_transaction"),"data_age_sec":c.get("data_age_sec"),
                   "updated_at":now()}
        st["updated_at"]=now();save_state(st)
    return c,round(min(100,quality+impulse),1),round(quality,1),round(impulse,1),phase

def cooldown_active(address):
    x=load_state().get("cooldowns",{}).get(str(address).lower())
    if not x:return False
    try:return datetime.fromisoformat(x.replace("Z","+00:00"))>datetime.now(timezone.utc)
    except:return False

def entry_gate(c,phase,quality,delta_impulse,snapshot_impulse,fresh,agent_cfg,data_fresh_for_entry):
    """Single source of truth for Champion paper-entry eligibility."""
    min_quality=agent_cfg["quality"]; min_impulse=agent_cfg["impulse"]
    min_buy_ratio=agent_cfg["buy_ratio"]; min_trades=agent_cfg["trades"]
    ratio=c["flow"]/max(c["vol"],1)
    price_flow_ok=not(c["m1"]>=12 and ratio<0.18)
    liquidity_ok=(c["vol"]>=500) or (
        c["vol"]>=300 and c["trades"]>=10 and c["flow"]>=100 and
        c["buy_ratio"]>=1.15 and ratio>=0.25
    )
    entry_impulse=max(delta_impulse,snapshot_impulse) if data_fresh_for_entry and phase=="CONFIRMATION" else delta_impulse
    # Selective 8-9 trade ignition; ordinary entries remain unchanged.
    early_ignition=(8<=c["trades"]<10 and quality>=60
        and c["vol"]>=650 and c["flow"]>=300 and c["flow15"]>=100
        and c["buy_ratio"]>=1.5 and c["m1"]>0 and 1.5<=c["m15"]<=8)
    ignition=(
        phase=="IGNITION" and quality>=min_quality and entry_impulse>=min_impulse
        and c["flow"]>=180 and c["flow15"]>=60
        and (c["trades"]>=max(10,min_trades) or early_ignition)
        and c["m15"]>=0.8
        and liquidity_ok and c["buy_ratio"]>=max(1.10,min_buy_ratio) and price_flow_ok
    )
    confirmation=(
        phase=="CONFIRMATION" and fresh and quality>=max(55,min_quality) and entry_impulse>=max(6,min_impulse)
        and c["flow"]>=150 and c["flow15"]>=40 and c["trades"]>=max(8,min_trades)
        and liquidity_ok and c["buy_ratio"]>=max(1.12,min_buy_ratio)
        and c["m15"]>=1.5 and price_flow_ok
    )
    buy=(ignition or confirmation) and data_fresh_for_entry
    if c["m1"]>12 and (c["m15"]<2 or c["flow15"]<=0): buy=False
    return {"buy":buy,"entry_impulse":entry_impulse,"liquidity_ok":liquidity_ok,
            "price_flow_ok":price_flow_ok,"ratio":ratio}

def decision(address,a,ws=None,persist=True):
    c,s,q,i,phase=score(address,a,persist=persist)
    cd=cooldown_active(address)
    agent_cfg=load_agent_config()
    entry_score=agent_cfg["entry_score"]; min_quality=agent_cfg["quality"]; min_impulse=agent_cfg["impulse"]
    min_buy_ratio=agent_cfg["buy_ratio"]; min_trades=agent_cfg["trades"]
    data_age=c.get("data_age_sec")
    data_fresh_for_entry=isinstance(data_age,(int,float)) and data_age<=MAX_ENTRY_DATA_AGE_SEC
    snapshot_impulse=min(20,max(0,
        max(0,c["m1"])*0.8 + max(0,c["m15"])*0.8 +
        max(0,c["flow15"])/100 + min(4,c["trades"]/5) +
        min(4,c["vol"]/250)
    )) if data_fresh_for_entry else 0.0
    # V30.5 paper-only impulse fix:
    # Champion's persisted delta impulse can legitimately be 0 when the current
    # fresh market sample is identical to the last persisted scanner sample.
    # Shadow already proved that the same fresh snapshot contains actionable
    # momentum. Preserve the MIN_IMPULSE protection, but let paper entry use
    # the stronger of delta impulse and fresh snapshot impulse. score() and
    # persisted history stay untouched, so this cannot contaminate scanner state.
    # V30.6 selective impulse: IGNITION must prove a real move between
    # persisted samples; snapshot-only momentum is allowed only after the
    # lifecycle has reached CONFIRMATION. This prevents a saturated snapshot
    # impulse from turning a one-tick burst into a new IGNITION entry.
    delta_impulse=i
    fresh_hist=load_state().get("history",{}).get(str(address).lower(),{})
    last=fresh_hist.get("last") or {}
    same_last=bool(last) and all(abs(n(c.get(k))-n(last.get(k)))<1e-9 for k in ("price","m1","m15","m4","flow","flow15","vol","trades"))
    p=(fresh_hist.get("prev") or {}) if (persist or same_last) else last
    d1=max(0,c["m1"]-n(p.get("m1")))
    d15=max(0,c["m15"]-n(p.get("m15")))
    df=c["flow"]-n(p.get("flow"))
    dv=max(0,c["vol"]-n(p.get("vol")))
    fresh=bool(p) and (d1>=0.35 or d15>=0.15 or df>=50 or dv>0)
    if same_last and "fresh" in fresh_hist:
        fresh=bool(fresh_hist["fresh"])
    gate=entry_gate(c,phase,q,delta_impulse,snapshot_impulse,fresh,agent_cfg,data_fresh_for_entry)
    entry_impulse=gate["entry_impulse"]
    liquidity_ok=gate["liquidity_ok"]
    price_flow_ok=gate["price_flow_ok"]
    ratio=gate["ratio"]
    lane_ok=phase in ("IGNITION","CONFIRMATION","BREAKOUT")
    buy=gate["buy"]
    # V31 profit-first BUY-only gate (paper trading). Preserve every V30 exit.
    # Missing, nonfinite or negative age fails closed.
    import math
    v31_age_ok=(isinstance(data_age,(int,float)) and not isinstance(data_age,bool)
                and math.isfinite(data_age) and 0<=data_age<=300)
    v31_trades_ok=(isinstance(c.get("trades"),(int,float))
                   and math.isfinite(c["trades"]) and c["trades"]>=12)
    # Paper-only controlled expansion: exceptionally fresh, liquid early pump.
    # All existing lifecycle, score, impulse, cooldown and exit gates remain.
    v31_early_ok=(v31_age_ok and isinstance(c.get("trades"),(int,float))
                  and math.isfinite(c["trades"]) and 8<=c["trades"]<12
                  and c["vol"]>=500 and c["flow"]>=200 and c["flow15"]>=60
                  and c["buy_ratio"]>=1.3 and c["m1"]>0 and c["m15"]>=1.5)
    v31_buy_ok=(v31_age_ok and v31_trades_ok) or v31_early_ok
    thin_activity=c["trades"]<10
    entry_score_required=max(entry_score,74) if thin_activity else entry_score
    late_spike=c["m1"]>12
    blocked=(not buy) or cd or s<entry_score_required or not data_fresh_for_entry or not v31_buy_ok or late_spike
    failures=[]
    if not v31_age_ok: failures.append("V31 BUY gate: data age missing or >300s")
    if not (v31_trades_ok or v31_early_ok): failures.append("V31 BUY gate: trades 1h <12 (no early exception)")
    if phase not in ("IGNITION","CONFIRMATION"): failures.append("no eligible entry lane")
    if phase=="IGNITION" and delta_impulse<min_impulse:
        failures.append(f"no delta impulse +{delta_impulse:.1f}<{min_impulse:.1f}")
    elif phase=="CONFIRMATION" and not fresh and entry_impulse<min_impulse:
        failures.append("no fresh impulse")
    if s<entry_score_required: failures.append(f"score {s:.0f}<{entry_score_required:.0f}")
    if late_spike: failures.append("late entry: M1H>12%")
    if q<min_quality: failures.append(f"quality {q:.0f}<{min_quality:.0f}")
    if entry_impulse<MIN_IMPULSE: failures.append(f"impulse +{entry_impulse:.1f}<{MIN_IMPULSE:.1f}")
    if c["flow"]<=0: failures.append("flow<=0")
    if c["flow15"]<0: failures.append("15m flow<0")
    if c["trades"]<min_trades: failures.append(f"trades {c['trades']:.0f}<{min_trades:.0f}")
    if not liquidity_ok: failures.append(f"liquidity vol {c['vol']:.0f}, flow {c['flow']:.0f}")
    if c["buy_ratio"]<min_buy_ratio: failures.append(f"buy ratio {c['buy_ratio']:.2f}<{min_buy_ratio:.2f}")
    if c["m15"]<-0.25: failures.append(f"M15 {c['m15']:+.2f}<-0.25%")
    if not price_flow_ok: failures.append("price/flow mismatch")
    if c["m1"]>12: failures.append("late entry: M1H>12%")
    if not data_fresh_for_entry: failures.insert(0,f"stale market data {data_age:.0f}s" if isinstance(data_age,(int,float)) else "missing market data age")
    if cd: failures.insert(0,"cooldown")
    # Diagnostic-only: report every active gate, including impulse. Do not
    # change eligibility or thresholds when adding observability.
    gate_failures=[]
    if not v31_age_ok: gate_failures.append("v31_age")
    if not (v31_trades_ok or v31_early_ok): gate_failures.append("v31_activity")
    if phase not in ("IGNITION","CONFIRMATION"): gate_failures.append("lifecycle")
    if phase=="IGNITION" and delta_impulse<min_impulse: gate_failures.append("impulse")
    elif phase=="CONFIRMATION" and (not fresh or entry_impulse<max(6,min_impulse)): gate_failures.append("impulse")
    if s<entry_score_required: gate_failures.append("score")
    if late_spike: gate_failures.append("late_spike")
    if q<min_quality: gate_failures.append("quality")
    if not liquidity_ok: gate_failures.append("liquidity")
    if c["buy_ratio"]<min_buy_ratio: gate_failures.append("buy_ratio")
    if c["trades"]<min_trades: gate_failures.append("trades")
    if not price_flow_ok: gate_failures.append("price_flow")
    if not data_fresh_for_entry: gate_failures.append("stale_data")
    if cd: gate_failures.append("cooldown")
    reason="READY" if not blocked else f"{phase} / "+"; ".join(failures[:3])

    # V30.4 Shadow diagnostics: explain every failed confirmation gate.
    # Diagnostic only; this does not alter Champion or Shadow eligibility.
    shadow_failures=[]
    if phase!="CONFIRMATION": shadow_failures.append(f"phase {phase} (requires CONFIRMATION)")
    if not data_fresh_for_entry:
        shadow_failures.append(f"stale data {data_age:.0f}s" if isinstance(data_age,(int,float)) else "missing data age")
    if q<55: shadow_failures.append(f"quality {q:.0f}<55")
    if snapshot_impulse<6: shadow_failures.append(f"snapshot impulse +{snapshot_impulse:.1f}<6.0")
    if c["flow"]<150: shadow_failures.append(f"flow {c['flow']:.0f}<150")
    if c["flow15"]<40: shadow_failures.append(f"flow15 {c['flow15']:.0f}<40")
    if c["trades"]<8: shadow_failures.append(f"trades {c['trades']:.0f}<8")
    if not liquidity_ok: shadow_failures.append(f"low liquidity vol {c['vol']:.0f}, flow {c['flow']:.0f}")
    if c["buy_ratio"]<1.12: shadow_failures.append(f"buy ratio {c['buy_ratio']:.2f}<1.12")
    if c["m15"]<1.5: shadow_failures.append(f"M15 {c['m15']:+.2f}<+1.50%")
    if not price_flow_ok: shadow_failures.append("price/flow mismatch")
    shadow_ready=not shadow_failures
    shadow_reason="READY" if shadow_ready else "; ".join(shadow_failures[:4])

    # V30.4 EARLY shadow lane: measure strong WATCH setups before the strict
    # confirmation lifecycle. Diagnostic only -- never feeds Champion BUY.
    early_shadow_failures=[]
    if phase!="WATCH": early_shadow_failures.append(f"phase {phase} (requires WATCH)")
    if not data_fresh_for_entry: early_shadow_failures.append("stale data")
    if q<55: early_shadow_failures.append(f"quality {q:.0f}<55")
    if snapshot_impulse<8: early_shadow_failures.append(f"snapshot impulse +{snapshot_impulse:.1f}<8.0")
    if c["flow"]<180: early_shadow_failures.append(f"flow {c['flow']:.0f}<180")
    if c["flow15"]<40: early_shadow_failures.append(f"flow15 {c['flow15']:.0f}<40")
    if c["trades"]<8: early_shadow_failures.append(f"trades {c['trades']:.0f}<8")
    if not liquidity_ok: early_shadow_failures.append("low liquidity")
    if c["buy_ratio"]<1.12: early_shadow_failures.append(f"buy ratio {c['buy_ratio']:.2f}<1.12")
    if c["m15"]<=0: early_shadow_failures.append(f"M15 {c['m15']:+.2f}<=0")
    if c["m1"]<=0: early_shadow_failures.append(f"M1H {c['m1']:+.2f}<=0")
    if c["m1"]>12: early_shadow_failures.append(f"late M1H {c['m1']:+.2f}>+12%")
    if not price_flow_ok: early_shadow_failures.append("price/flow mismatch")
    shadow_early_ready=not early_shadow_failures
    shadow_early_reason="READY" if shadow_early_ready else "; ".join(early_shadow_failures[:4])

    eff=s if not blocked else min(s,ENTRY_SCORE-1)
    return {"score":eff,"confidence":eff,"buy_score":eff,"market_score":eff,
      "m1h":c["m1"],"m15":c["m15"],"m4h":c["m4"],"net_1h":c["flow"],"whale_net":c["flow"],
      "trades_1h":c["trades"],"volume_1h":c["vol"],"buy_ratio":c["buy_ratio"],"trade_ratio":c["trade_ratio"],
      "v31_buy_gate_passed":v31_buy_ok,"v31_early_exception_passed":v31_early_ok,"v31_buy_gate_age_passed":v31_age_ok,"v31_buy_gate_trades_passed":v31_trades_ok,
      "eligible_for_buy":not blocked,"pump_score":s,"pump_quality":q,"pump_change":entry_impulse,"pump_delta_impulse":delta_impulse,"pump_phase":phase,
      "paper_buy_blocked":blocked,"paper_buy_block_reason":reason,"paper_buy_gate_failures":gate_failures,
      "shadow_snapshot_impulse":round(snapshot_impulse,1),
      "shadow_confirmation_ready":shadow_ready,
      "shadow_block_reason":shadow_reason,
      "shadow_early_ready":shadow_early_ready,
      "shadow_early_reason":shadow_early_reason,
      "paper_prediction":{"ready":False,"role":"advisory"},"paper_prediction_role":"advisory",
      "data_age_sec":data_age,"data_fresh_for_entry":data_fresh_for_entry,
      "technical_bull":0,"technical_bear":0,"technical_evidence":[],
      "buy_score_components":{"momentum":round(c["m1"],1),"flow":round(c["flow"],1),"activity":round(c["trades"],1),
                              "prediction":0.0,"liquidity":0.0},
      "v30":{"version":VERSION,"entry_score":entry_score,"agent_config":agent_cfg,"sl_pct":SL_PCT,"max_open":MAX_OPEN,
             "max_buys":MAX_BUYS_PER_RUN,"phase":phase,"max_entry_data_age_sec":MAX_ENTRY_DATA_AGE_SEC}}

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
        z.update({"v30_mode":"CLEAN-PUMP-HUNTER-V30.3","pump_peak_price":e,"pump_mfe_pct":0.0,
                  "pump_weak_count":0,"pump_age_scans":0,"sl_pct":SL_PCT,
                  "sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),
                  "tp1_pct":0.99,"tp2_pct":0.99,"trail_pct":0.0,
                  "entry_pump_score":n(s.get("pump_score",s.get("confidence"))),
                  "entry_pump_change":n(s.get("pump_change")),"entry_phase":s.get("pump_phase","NO")})
        z["pump_entry_snapshot"]={
            "at":z.get("opened_at"),"price_sda":e,"reference_price_sda":n(an.get("price_in_sda")),
            "score":n(s.get("pump_score",s.get("confidence"))),
            "quality":n(s.get("pump_quality")),"impulse":n(s.get("pump_change")),
            "phase":s.get("pump_phase"),"m1h":n(s.get("m1h")),
            "m15":n(s.get("m15")),"m4h":n(s.get("m4h")),
            "data_age_sec":n(s.get("data_age_sec")),
            "flow_1h":n(s.get("net_1h")),"volume_1h":n(s.get("volume_1h")),
            "trades_1h":n(s.get("trades_1h"))}
        z["pump_price_samples"]=[]
        z["pump_mae_pct"]=0.0
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
            observed=datetime.now(timezone.utc)
            previous_observed=pos.get('pump_last_observed_at')
            try:
                previous_dt=datetime.fromisoformat(str(previous_observed).replace('Z','+00:00'))
                if previous_dt.tzinfo is None: previous_dt=previous_dt.replace(tzinfo=timezone.utc)
                gap_seconds=max(0,(observed-previous_dt).total_seconds())
            except (TypeError,ValueError):
                gap_seconds=None
            pos['pump_last_observed_at']=observed.isoformat()
            s=decision(address,a,ws); roi=(cur-entry)/entry*100
            data_age=s.get("data_age_sec")
            import math
            data_fresh_for_exit=(isinstance(data_age,(int,float)) and not isinstance(data_age,bool)
                                 and math.isfinite(data_age) and 0<=data_age<=MAX_EXIT_DATA_AGE_SEC)
            # Never execute an exit or advance trailing-stop/weakness state using
            # a stale price. Emit a throttled warning and retain the position
            # until a new transaction gives us an observable execution price.
            if not data_fresh_for_exit:
                pos["pump_exit_data_stale"]=True
                pos["pump_exit_data_age_sec"]=data_age
                last_alert=pos.get("pump_stale_alert_at")
                try:
                    alert_dt=datetime.fromisoformat(str(last_alert).replace("Z","+00:00"))
                    if alert_dt.tzinfo is None: alert_dt=alert_dt.replace(tzinfo=timezone.utc)
                    alert_due=(observed-alert_dt).total_seconds()>=900
                except (TypeError,ValueError):
                    alert_due=True
                if alert_due:
                    pos["pump_stale_alert_at"]=observed.isoformat()
                    age_label=f"{data_age:.0f}s" if isinstance(data_age,(int,float)) and math.isfinite(data_age) else "unknown"
                    events.append(f"⚠️ PAPER EXIT DATA STALE {pos.get('label',address)} | age {age_label} | no simulated fill")
                continue
            pos["pump_exit_data_stale"]=False
            pos["pump_exit_data_age_sec"]=data_age
            peak=max(n(pos.get("pump_peak_price"),entry),cur); pos["pump_peak_price"]=peak
            mfe=(peak-entry)/entry*100; pos["pump_mfe_pct"]=mfe
            pos["pump_age_scans"]=int(n(pos.get("pump_age_scans"))+1)
            # MFE/MAE are observed at scanner cadence, not tick highs/lows.
            pos["pump_mae_pct"]=min(n(pos.get("pump_mae_pct")),roi)
            samples=pos.setdefault("pump_price_samples",[])
            if not isinstance(samples,list):
                samples=[];pos["pump_price_samples"]=samples
            samples.append({
                "at":observed.isoformat(),"price_sda":cur,"gross_roi_pct":round(roi,4),
                "mfe_pct":round(mfe,4),"mae_pct":round(pos["pump_mae_pct"],4),
                "score":n(s.get("pump_score")),"quality":n(s.get("pump_quality")),
                "impulse":n(s.get("pump_change")),"phase":s.get("pump_phase"),
                "m1h":n(s.get("m1h")),"m15":n(s.get("m15")),
                "m4h":n(s.get("m4h")),"flow_1h":n(s.get("net_1h")),
                "volume_1h":n(s.get("volume_1h")),"trades_1h":n(s.get("trades_1h"))
            })
            if len(samples)>96:del samples[:-96]
            age=age_hours(pos)
            # Profit protection: once positive, stop giving the trade back.
            stop=n(pos.get("sl"),entry*(1-SL_PCT))
            if mfe>=2.5: stop=max(stop,entry*1.002)
            if mfe>=4: stop=max(stop,peak*0.975)
            if mfe>=10: stop=max(stop,peak*0.95)
            if mfe>=20: stop=max(stop,peak*0.92)
            if mfe>=40: stop=max(stop,peak*0.90)
            if mfe>=70: stop=max(stop,peak*0.88)
            pos["sl"]=stop
            weak=(s["m15"]<0 and s["net_1h"]<=0) or s["pump_score"]<38
            pos["pump_weak_count"]=int(n(pos.get("pump_weak_count"))+1) if weak else max(0,int(n(pos.get("pump_weak_count")))-1)
            if not data_fresh_for_exit:
                r=None
            elif (cur-entry)/entry*100<=-3.0:
                r=engine.close(p,address,cur,"V30.3 EMERGENCY/HARD STOP")
            elif cur<=stop:
                r=engine.close(p,address,cur,"V30.3 TRAILING/STOP")
            elif roi<=-1.75 and pos["pump_weak_count"]>=2:
                r=engine.close(p,address,cur,"V30.3 EARLY WEAKNESS")
            elif age>=2 and mfe<2 and pos["pump_weak_count"]>=3 and roi<0.5:
                r=engine.close(p,address,cur,"V30.3 STALE")
            elif roi>0.5 and mfe>=2.5 and pos["pump_weak_count"]>=3:
                r=engine.close(p,address,cur,"V30.3 PUMP BREAKDOWN")
            else:r=None
            if r:
                r['pump_exit_stop_price']=stop
                r['pump_exit_stop_gap_pct']=round((cur/stop-1)*100,3) if stop>0 else None
                r['pump_exit_observation_gap_seconds']=round(gap_seconds,1) if gap_seconds is not None else None
                r['pump_exit_observed_at']=observed.isoformat()
                r['pump_exit_snapshot']={
                    "at":observed.isoformat(),"price_sda":cur,"gross_roi_pct":round(roi,4),
                    "stop_price_sda":stop,"mfe_pct":round(mfe,4),
                    "mae_pct":round(n(pos.get("pump_mae_pct")),4),
                    "score":n(s.get("pump_score")),"quality":n(s.get("pump_quality")),
                    "impulse":n(s.get("pump_change")),"phase":s.get("pump_phase"),
                    "m15":n(s.get("m15")),"flow_1h":n(s.get("net_1h")),
                    "observation_gap_seconds":round(gap_seconds,1) if gap_seconds is not None else None}
                # close() appends a copy; update that copy for persistence.
                closed=p.get("closed_trades") or []
                if closed and closed[-1].get("address")==address:
                    closed[-1].update({
                        "pump_exit_stop_price":r["pump_exit_stop_price"],
                        "pump_exit_stop_gap_pct":r["pump_exit_stop_gap_pct"],
                        "pump_exit_observation_gap_seconds":r["pump_exit_observation_gap_seconds"],
                        "pump_exit_observed_at":r["pump_exit_observed_at"],
                        "pump_exit_snapshot":r["pump_exit_snapshot"]})
                if "STOP" in str(r.get("close_reason","")) or "WEAK" in str(r.get("close_reason","")) or "STALE" in str(r.get("close_reason","")) or "BREAKDOWN" in str(r.get("close_reason","")):
                    st=load_state();st.setdefault("cooldowns",{})[str(address).lower()]=(datetime.now(timezone.utc)+timedelta(hours=COOLDOWN_HOURS)).isoformat();save_state(st)
                events.append(f"V30.3 {r.get('close_reason','EXIT')} {r.get('label',address)} | ROI {roi:+.2f}% | MFE {mfe:+.2f}%")
        return events

    def paper_decision(address,analysis,ws):
        d=decision(address,analysis,ws)
        return {**d,"data":d,"prediction":d["paper_prediction"],"blocked":d["paper_buy_blocked"],
                "reason":d["paper_buy_block_reason"],"score_band":d["pump_phase"]}

    # engine.py re-exports engine_legacy.py symbols. The actual paper runner
    # calls engine_legacy.main(), whose global lookups therefore bypass only
    # changing engine.score/create/_auto_exit. Patch both the public facade and
    # the legacy module so the V30 decision is the decision that actually opens
    # paper positions.
    legacy=getattr(engine,"_legacy",None)
    targets=[engine] + ([legacy] if legacy is not None else [])
    for target in targets:
        target.score=decision
        target.create=create
        target.BUY_THRESHOLD=ENTRY_SCORE
        target.MAX_OPEN_POSITIONS=MAX_OPEN
        target.MAX_NEW_BUYS_PER_RUN=MAX_BUYS_PER_RUN
        target.SL_PCT=SL_PCT
        target._auto_exit=auto_exit
    def execution_hook(event,address,analysis,s,meta):
        try:
            from datetime import datetime, timezone
            import json
            from pathlib import Path
            p=Path(EXECUTION_STATE_FILE)
            state={}
            try:
                state=json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                state={}
            events=state.get("events",[]) if isinstance(state,dict) else []
            label=(analysis.get("symbol") if isinstance(analysis,dict) else None) or str(address)[:10]
            events.append({
                "at":datetime.now(timezone.utc).isoformat(),
                "event":str(event),
                "address":str(address).lower(),
                "label":str(label),
                "score":n(s.get("pump_score",s.get("confidence"))),
                "quality":n(s.get("pump_quality")),
                "impulse":n(s.get("pump_change")),
                "phase":s.get("pump_phase"),
                "data_age_sec":n(s.get("data_age_sec")),
                "meta":meta if isinstance(meta,dict) else {}
            })
            # Preserve funnel history; replacing the whole state erased it on BUY events.\n            state.update({"version":VERSION,"paper_only":True,"events":events[-100:]})
            tmp=p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False),encoding="utf-8")
            tmp.replace(p)
            print(f"V30 EXECUTION • {label} • {event}")
        except Exception as exc:
            print("V30 EXECUTION TELEMETRY ERROR:",repr(exc))

    # Hook the real engine_legacy BUY->create path. Telemetry only; it never changes eligibility.
    for target in targets:
        target.PAPER_EXECUTION_HOOK=execution_hook
    main_module.paper_execution_hook=execution_hook
    main_module.paper_decision=paper_decision
    if callable(original_main):
        def wrapped_main(*args,**kwargs):
            _RUNTIME=None
            return original_main(*args,**kwargs)
        engine.main=wrapped_main
    main_module._v30_patched=True

def write_scan_funnel(market_file="market_analysis.json"):
    """Persist one paper-only V30 decision funnel snapshot for diagnostics."""
    try:
        import json
        from pathlib import Path
        from datetime import datetime, timezone
        src=Path(market_file)
        data=json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
        tokens=data.get("tokens",{}) if isinstance(data,dict) else {}
        total=fresh=champion=shadow=0
        fresh_gate_counts={}
        near_ready=[]
        candidates=[]
        scan_at=datetime.now(timezone.utc).isoformat()
        for address,td in tokens.items():
            an=td.get("analysis") if isinstance(td,dict) and isinstance(td.get("analysis"),dict) else td
            if not isinstance(an,dict) or n(an.get("price_in_sda"))<=0:
                continue
            total+=1
            d=decision(address,an,None,persist=False)
            # Record eligible AND rejected candidates with contemporaneous
            # decision features. Research-only; no BUY/SELL decisions changed.
            candidates.append({
                "at":scan_at,"address":str(address).lower(),
                "score":round(n(d.get("pump_score")),2),
                "quality":round(n(d.get("pump_quality")),2),
                "impulse":round(n(d.get("pump_change")),2),
                "delta_impulse":round(n(d.get("pump_delta_impulse")),2),
                "snapshot_impulse":round(n(d.get("shadow_snapshot_impulse")),2),
                "phase":d.get("pump_phase"),
                "price_sda":n(an.get("price_in_sda")),
                "quote_at":an.get("last_transaction") or td.get("last_transaction"),
                "trades_1h":n(d.get("trades_1h")),
                "buy_ratio":n(d.get("buy_ratio")),
                "flow_1h":n(d.get("net_1h")),
                "flow_15m":n(an.get("net_15m") or an.get("net_flow_15m") or an.get("flow_15m")),
                "volume_1h":n(d.get("volume_1h")),
                "m15":n(d.get("m15")),
                "m1h":n(d.get("m1h")),
                "data_age_sec":round(n(d.get("data_age_sec")),1),
                "eligible":bool(d.get("eligible_for_buy")),
                "gates":list(dict.fromkeys(d.get("paper_buy_gate_failures") or []))
            })
            is_fresh=bool(d.get("data_fresh_for_entry"))
            fresh+=int(is_fresh)
            champion+=int(bool(d.get("eligible_for_buy")))
            shadow+=int(bool(d.get("shadow_confirmation_ready") or d.get("shadow_early_ready")))
            if is_fresh and not d.get("eligible_for_buy"):
                reason=str(d.get("paper_buy_block_reason") or "")
                # Consume structured decision gates; reason text is truncated and
                # cannot reliably represent all active blocking conditions.
                gates=list(dict.fromkeys(d.get("paper_buy_gate_failures") or []))
                for gate in gates:
                    fresh_gate_counts[gate]=fresh_gate_counts.get(gate,0)+1
                near_ready.append({
                    "address":str(address).lower(),
                    "symbol":str(an.get("symbol") or "")[:20],
                    "score":round(n(d.get("pump_score")),1),
                    "quality":round(n(d.get("pump_quality")),1),
                    "impulse":round(n(d.get("pump_change")),1),
                    "phase":d.get("pump_phase"),
                    "data_age_sec":round(n(d.get("data_age_sec")),1),
                    "gates":gates,
                    "reason":reason,
                })
        near_ready.sort(key=lambda x:(len(x["gates"]),-x["score"],-x["quality"]))
        candidates.sort(key=lambda x:-x["score"])
        p=Path(EXECUTION_STATE_FILE)
        try: state=json.loads(p.read_text(encoding="utf-8"))
        except Exception: state={}
        if not isinstance(state,dict): state={}
        events=state.get("events",[]) if isinstance(state.get("events"),list) else []
        funnels=state.get("funnels",[]) if isinstance(state.get("funnels"),list) else []
        funnels.append({
            "at":datetime.now(timezone.utc).isoformat(),
            "tokens":total,"fresh":fresh,
            "champion_ready":champion,"shadow_ready":shadow,
            "fresh_gate_counts":dict(sorted(fresh_gate_counts.items(),key=lambda kv:(-kv[1],kv[0]))),
            "near_ready":near_ready[:5],
            "candidate_snapshots":candidates[:50],
            "ready_hook_events":sum(1 for e in events if str(e.get("event")).upper()=="READY"),
            "created_hook_events":sum(1 for e in events if str(e.get("event")).upper()=="CREATED")
        })
        state.update({"version":VERSION,"paper_only":True,"events":events[-100:],"funnels":funnels[-100:]})
        tmp=p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False),encoding="utf-8")
        tmp.replace(p)
        print(f"V30 FUNNEL • tokens {total} • fresh {fresh} • champion {champion} • shadow {shadow} • gates {fresh_gate_counts}")
    except Exception as exc:
        print("V30 FUNNEL TELEMETRY ERROR:",repr(exc))

def market_debug(dashboard,snapshot=None):
    md=dashboard.load("market_data.json",{"tokens":{}}) if snapshot is None else snapshot.get("md",{})
    tokens=md.get("tokens",{}) if isinstance(md,dict) else {}
    rows=[]
    for address,td in tokens.items():
        a=td.get("analysis",td) if isinstance(td,dict) else {}
        if not isinstance(a,dict) or n(a.get("price_in_sda"))<=0:continue
        d=decision(address,a,persist=False)
        rows.append((d["pump_score"],address,d))
    rows.sort(reverse=True)
    return "\n".join([f"V30 CLEAN • loaded {len(tokens)} • ready {sum(1 for _,_,d in rows[:5] if not d['paper_buy_blocked'])}"]+
      [f"{i}. {d['pump_phase']} {a} score {d['pump_score']:.0f} impulse +{d['pump_change']:.1f} M15 {d['m15']:+.1f}% M1H {d['m1h']:+.1f}% flow {d['net_1h']:+.0f}" for i,(_,a,d) in enumerate(rows[:5],1)])
