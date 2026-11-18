"""Independent REAL BUY pipeline; shares V30 market scoring and TP4 temporal rules."""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import real_trader
import real_trading_config as cfg
import real_wallet_reconciliation as wallet_reconciliation
import v30_pump_hunter as strategy
from tools.paper_quote_quality import entry_gate as quote_gate
from tools.temporal_recovery_buy import evaluate_tp4
from tools.v32_hourly_macd import paper_macd_veto

STATE=Path("real_buy_signal_state.json")
VERSION=1
COOLDOWN_HOURS=6

def now():
    return datetime.now(UTC)

def initial_state():
    return {"schema_version":VERSION,"updated_at":now().isoformat(),"history":{},
            "temporal_history":{},"cooldowns":{},"risk_circuit":{},
            "buy_ready":{},"consumed_signal_ids":[]}

def _json_clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    return value

def save_state(state,path=STATE):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(_json_clean(state),indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    os.replace(tmp,path)

def load_state(path=STATE):
    path=Path(path)
    if not path.exists():
        return None,"REAL_SIGNAL_STATE_MISSING"
    try:
        state=json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state,dict) or state.get("schema_version")!=VERSION
            or any(not isinstance(state.get(k),dict) for k in ("history","temporal_history","cooldowns","risk_circuit","buy_ready"))
            or not isinstance(state.get("consumed_signal_ids"),list)):
            return None,"REAL_SIGNAL_STATE_INVALID_SCHEMA"
        return state,None
    except (OSError,ValueError,TypeError):
        return None,"REAL_SIGNAL_STATE_UNREADABLE"

def stamp(value):
    if not isinstance(value,str): return None
    try:
        d=datetime.fromisoformat(value)
        return d.astimezone(UTC) if d.tzinfo else None
    except ValueError: return None

def _capacity(real):
    from decimal import Decimal
    positions=real.get("positions"); pending=real.get("pending_buys")
    if not isinstance(positions,dict) or not isinstance(pending,dict):
        return False,"REAL_POSITION_STATE_INVALID",None
    if str(real.get("syncStatus") or "").upper() != "FRESH":
        return False,"REAL_WALLET_SYNC_NOT_FRESH",None
    synced=stamp(real.get("lastSuccessfulWalletSync"))
    if synced is None or not 0 <= (now()-synced).total_seconds() <= wallet_reconciliation.MAX_SNAPSHOT_AGE_SECONDS:
        return False,"REAL_WALLET_SYNC_STALE",None
    count=len(positions)+len(pending)
    if count>=cfg.MAX_OPEN_POSITIONS: return False,"REAL_POSITION_LIMIT",None
    deployed=real_trader.deployed_cost_basis(real)
    amount=cfg.position_sda()
    if deployed+Decimal(str(amount))>Decimal(str(cfg.TRADING_WALLET_CAPITAL_SDA)):
        return False,"REAL_CAPITAL_LIMIT",None
    try:
        wallet=real_trader.wallet_address()
        balance=real_trader.native_balance(wallet)
    except Exception as exc:  # noqa: BLE001 - RPC failure must fail closed
        return False,"REAL_BALANCE_UNAVAILABLE:"+type(exc).__name__,None
    if balance<int(amount*10**18): return False,"REAL_NATIVE_BALANCE_INSUFFICIENT",None
    return True,"REAL_CAPACITY_AVAILABLE",{"positions":count,"max_positions":cfg.MAX_OPEN_POSITIONS,
        "deployed_sda":float(deployed),"capital_cap_sda":cfg.TRADING_WALLET_CAPITAL_SDA,
        "wallet_balance_sda":balance/10**18}

def _market_gates(token, analysis, signal, temporal, at):
    age=signal["data_age_sec"]; trades=signal["trades_1h"]
    age_ok=isinstance(age,(int,float)) and not isinstance(age,bool) and math.isfinite(age) and 0<=age<=300
    trades_ok=isinstance(trades,(int,float)) and not isinstance(trades,bool) and math.isfinite(trades) and trades>=12
    early_ok=(age_ok and isinstance(trades,(int,float)) and 8<=trades<12
        and float(signal["volume_1h"] or 0)>=500 and float(signal["flow_1h"] or 0)>=200
        and float(signal["flow_15m"] or 0)>=60 and float(signal["buy_ratio"] or 0)>=1.3
        and float(signal["m1h"] or 0)>0 and float(signal["m15"] or 0)>=1.5)
    temporal_early=(temporal.get("ready") is True and temporal.get("lane") in ("confirmed_continuation","swing_single_quote")
        and (early_ok or temporal.get("sparse_activity_verified") is True))
    activity_ok=trades_ok or temporal_early
    volume=float(signal["volume_1h"] or 0); flow=float(signal["flow_1h"] or 0)
    price_flow_ok=not(float(signal["m1h"] or 0)>=12 and flow/max(volume,1)<0.18)
    late=float(signal["m1h"] or 0)>12
    macd=paper_macd_veto(analysis,as_of=at)
    quote=quote_gate(token,signal["quote_at"],
        confirmed_continuation=bool(temporal.get("ready") and temporal.get("lane")=="confirmed_continuation"),
        require_continuity=False)
    blocked=(not temporal.get("ready") or not age_ok or not activity_ok
        or not price_flow_ok or late or macd.get("blocked") or not quote.get("ready"))
    reasons=[]
    if not temporal.get("ready"): reasons.append("temporal_recovery:"+str(temporal.get("reason")))
    if not age_ok: reasons.append("v31_age")
    if not activity_ok: reasons.append("v31_activity")
    if not price_flow_ok: reasons.append("price_flow")
    if late: reasons.append("late_spike")
    if macd.get("blocked"): reasons.append("macd_bearish")
    if not quote.get("ready"): reasons.append("quote_quality:"+str(quote.get("reason")))
    return {"blocked":blocked,"reasons":reasons,"macd":macd,"quote":quote,
            "age_ok":age_ok,"activity_ok":activity_ok}

def _current_signal(analysis,scored,at=None):
    c,score,quality,delta_impulse,phase=scored
    age=c.get("data_age_sec")
    fresh=isinstance(age,(int,float)) and not isinstance(age,bool) and math.isfinite(age) and age<=strategy.MAX_ENTRY_DATA_AGE_SEC
    snapshot=min(20,max(0,
        max(0,float(c.get("m1") or 0))*0.8
        + max(0,float(c.get("m15") or 0))*0.8
        + max(0,float(c.get("flow15") or 0))/100
        + min(4,float(c.get("trades") or 0)/5)
        + min(4,float(c.get("vol") or 0)/250)
    )) if fresh else 0.0
    # Exact PAPER entry_gate impulse rule: snapshot impulse supports
    # CONFIRMATION only; IGNITION still requires the observed delta impulse.
    entry_impulse=max(float(delta_impulse),snapshot) if fresh and phase=="CONFIRMATION" else float(delta_impulse)
    return {"phase":phase,"score":float(score),"quality":float(quality),"impulse":entry_impulse,
        "delta_impulse":float(delta_impulse),"snapshot_impulse":snapshot,
        "quote_at":c.get("last_transaction"),"data_age_sec":age,
        "price_sda":c.get("price"),"m1h":c.get("m1"),"m15":c.get("m15"),
        "flow_15m":c.get("flow15"),"flow_1h":c.get("flow"),"volume_1h":c.get("vol"),
        "trades_1h":c.get("trades"),"buy_ratio":c.get("buy_ratio"),
        "decision_at":(at or now()).isoformat()}

def evaluate_candidate(token,analysis,state,real_state,at=None):
    at=at or now(); token=str(token).lower()
    scored=strategy.score(token,analysis,persist=True,state=state)
    signal=_current_signal(analysis,scored,at)
    prior=state["temporal_history"].get(token) or {}
    temporal=evaluate_tp4(token,signal,at=at,previous=prior)
    # Advance REAL's own temporal evidence; never consult Shadow/PAPER journals.
    state["temporal_history"][token]=signal
    state["temporal_history"]=dict(list(state["temporal_history"].items())[-5000:])
    market=_market_gates(token,analysis,signal,temporal,at)
    market_block=market["blocked"]
    until=stamp(state["cooldowns"].get(token)); cooldown=bool(until and until>at)
    try: daily=float(real_state.get("daily_realized_pnl_sda") or 0)
    except (ValueError,TypeError): daily=float("-inf")
    risk_block=not math.isfinite(daily) or daily<=-float(cfg.MAX_DAILY_LOSS_SDA)
    signal_id=hashlib.sha256(f"{token}|{signal['quote_at']}|{strategy.VERSION}|REAL".encode()).hexdigest() if signal["quote_at"] else None
    duplicate=bool(signal_id and signal_id in state["consumed_signal_ids"])
    exists=token in real_state.get("positions",{}) or token in real_state.get("pending_buys",{})
    cap_ok,cap_reason,capacity=_capacity(real_state)
    reasons=[]
    if market_block: reasons.append("MARKET:"+str(temporal.get("reason")) if not temporal.get("ready") else "MARKET_FILTER")
    if cooldown: reasons.append("REAL_COOLDOWN")
    if risk_block: reasons.append("REAL_LOSS_CIRCUIT")
    if duplicate: reasons.append("REAL_DUPLICATE_SIGNAL")
    if exists: reasons.append("REAL_POSITION_OR_PENDING_BUY")
    if not cap_ok: reasons.append(cap_reason)
    ready=not(market_block or cooldown or risk_block or duplicate or exists or not cap_ok)
    result={"ready":ready,"reason":"READY" if ready else ";".join(reasons),"token":token,
        "signal_id":signal_id,"signal":signal,"temporal":temporal,"cooldown_active":cooldown,
        "risk_circuit":{"blocked":risk_block,"daily_realized_pnl_sda":daily,
                        "daily_loss_limit_sda":cfg.MAX_DAILY_LOSS_SDA},
        "capacity":capacity,"capacity_reason":cap_reason,"at":at.isoformat()}
    state["buy_ready"][token]={"ready":ready,"reason":result["reason"],"signal_id":signal_id,
        "at":result["at"],"capacity":capacity,"temporal":temporal}
    state["risk_circuit"]={"blocked":risk_block,"daily_realized_pnl_sda":daily,
        "limit_sda":cfg.MAX_DAILY_LOSS_SDA,"updated_at":at.isoformat()}
    return result

def run_scan(market_path="market_analysis.json",execute=True,state_path=STATE):
    if os.getenv("REAL_TRADING_ENABLED","false").lower()!="true":
        return {"status":"disabled","buy_ready":0,"actions":[]}
    if os.getenv("SDA_SCANNER_BUY_DATA_AVAILABLE","true").strip().lower()!="true":
        return {"status":"market_data_degraded","buy_ready":0,"actions":[]}
    state,error=load_state(state_path)
    if error: return {"status":"state_error","error":error,"buy_ready":0,"actions":[]}
    try:
        market=json.loads(Path(market_path).read_text(encoding="utf-8"))
        tokens=market.get("tokens")
        if not isinstance(tokens,dict): raise TypeError("tokens missing")
        real=real_trader.load_state()
    except Exception as exc:  # noqa: BLE001 - malformed input or REAL ledger failure must fail closed
        return {"status":"input_error","error":type(exc).__name__,"buy_ready":0,"actions":[]}
    actions=[]; ready=0; executed=0
    for token,row in tokens.items():
        if not isinstance(row,dict): continue
        result=evaluate_candidate(token,row,state,real)
        if not result["ready"]: continue
        ready+=1
        if not execute or executed>=MAX_BUYS_PER_SCAN:
            actions.append({"token":token,"status":"BUY_READY_ONLY","signal_id":result["signal_id"]}); continue
        try:
            real_trader.safety_check(cfg.position_sda(),real)
            response=real_trader.execute_buy(token,str(row.get("symbol") or row.get("ticker") or token[:8]),cfg.position_sda())
            status=response.get("status","UNKNOWN")
            if status=="DRY_RUN": status="WOULD EXECUTE"
            actions.append({"token":token,"status":status,"signal_id":result["signal_id"]})
            if status in ("CONFIRMED","PENDING_CONFIRMATION"):
                state["consumed_signal_ids"].append(result["signal_id"])
                state["consumed_signal_ids"]=state["consumed_signal_ids"][-20000:]
                state["cooldowns"][token]=(now()+timedelta(hours=COOLDOWN_HOURS)).isoformat()
                state["buy_ready"][token]={"ready":False,"reason":"REAL_BUY_SUBMITTED" if status=="CONFIRMED" else "REAL_BUY_PENDING",
                    "signal_id":result["signal_id"],"at":now().isoformat()}
                ready-=1
                executed+=1
            elif status=="WOULD EXECUTE":
                executed+=1
            else:
                state["buy_ready"][token]={"ready":False,"reason":"REAL_EXECUTOR_NOT_READY:"+str(status),
                    "signal_id":result["signal_id"],"at":now().isoformat()}
                ready-=1
        except Exception as exc:  # noqa: BLE001 - never let one candidate bypass REAL safety
            state["buy_ready"][token]={"ready":False,"reason":"REAL_SAFETY_OR_EXECUTION_BLOCKED:"+type(exc).__name__,
                "signal_id":result["signal_id"],"at":now().isoformat()}
            ready-=1
            actions.append({"token":token,"status":"SAFETY_OR_EXECUTION_BLOCKED","error":type(exc).__name__+":"+str(exc)[:180]})
    state["updated_at"]=now().isoformat(); save_state(state,state_path)
    return {"status":"ok","buy_ready":ready,"actions":actions,"paper_state_used":False}

def main():
    print("REAL BUY PIPELINE:",json.dumps(run_scan(),ensure_ascii=False,sort_keys=True))

if __name__=="__main__": main()
