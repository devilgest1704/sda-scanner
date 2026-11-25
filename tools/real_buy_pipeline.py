"""Independent REAL BUY pipeline; shares V30 market scoring and TP4 temporal rules."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import socket
import uuid
from contextlib import contextmanager
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
MAX_BUYS_PER_SCAN=1
CLAIM_TTL_SECONDS=300
CLAIM_STATUSES={"CLAIMED","FAILED_RETRYABLE","BROADCAST_PENDING","UNKNOWN","EXECUTED","WOULD_EXECUTE"}

def now():
    return datetime.now(UTC)

def initial_state():
    return {"schema_version":VERSION,"updated_at":now().isoformat(),"history":{},
            "temporal_history":{},"signal_history":{},"cooldowns":{},"risk_circuit":{},
            "buy_ready":{},"consumed_signal_ids":[],"claims":{},"market_data_status":"UNKNOWN"}

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
    tmp=path.with_suffix(path.suffix+"."+uuid.uuid4().hex+".tmp")
    try:
        with tmp.open("w",encoding="utf-8") as handle:
            json.dump(_json_clean(state),handle,indent=2,ensure_ascii=False,allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)

@contextmanager
def state_lock(path):
    lock_path=Path(path).with_suffix(Path(path).suffix+".lock")
    with lock_path.open("a+",encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def _valid_claim(signal_id,claim):
    return (isinstance(claim,dict) and claim.get("signal_id")==signal_id
        and claim.get("status") in CLAIM_STATUSES
        and all(isinstance(claim.get(key),str) and bool(claim[key])
                for key in ("token","worker_id","claim_id"))
        and stamp(claim.get("claimed_at")) is not None
        and stamp(claim.get("expires_at")) is not None
        and isinstance(claim.get("position_sda"),(int,float))
        and not isinstance(claim.get("position_sda"),bool)
        and math.isfinite(claim["position_sda"]) and claim["position_sda"]>0)

def load_state(path=STATE):
    path=Path(path)
    if not path.exists():
        return None,"REAL_SIGNAL_STATE_MISSING"
    try:
        state=json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state,dict) or state.get("schema_version")!=VERSION
            or stamp(state.get("updated_at")) is None
            or any(not isinstance(state.get(k),dict) for k in ("history","temporal_history","signal_history","cooldowns","risk_circuit","buy_ready"))
            or not isinstance(state.get("consumed_signal_ids"),list)
            or any(not isinstance(v,dict) for v in state["history"].values())
            or any(not isinstance(v,dict) for v in state["temporal_history"].values())
            or any(not isinstance(v,list) or any(not isinstance(item,dict) for item in v) for v in state["signal_history"].values())
            or any(not isinstance(v,dict) for v in state["buy_ready"].values())
            or any(not isinstance(k,str) or stamp(v) is None for k,v in state["cooldowns"].items())
            or any(not isinstance(v,str) or not v for v in state["consumed_signal_ids"])
            or not isinstance(state.get("claims",{}),dict)
            or any(not _valid_claim(key,value) for key,value in state.get("claims",{}).items())
            or state.get("market_data_status","UNKNOWN") not in ("UNKNOWN","OK","MARKET_DATA_DEGRADED","INPUT_ERROR","REAL_TRADING_DISABLED")):
            return None,"REAL_SIGNAL_STATE_INVALID_SCHEMA"
        state.setdefault("claims",{})
        return state,None
    except (OSError,ValueError,TypeError):
        return None,"REAL_SIGNAL_STATE_UNREADABLE"

def stamp(value):
    if not isinstance(value,str): return None
    try:
        d=datetime.fromisoformat(value)
        return d.astimezone(UTC) if d.tzinfo else None
    except ValueError: return None

def signal_identity(token,quote_at):
    observed=stamp(quote_at)
    if observed is None:
        return None
    identity=f"{cfg.CHAIN_ID}|{str(token).lower()}|{observed.isoformat()}|{strategy.VERSION}|REAL"
    return hashlib.sha256(identity.encode()).hexdigest()

def _required_state(path):
    state,error=load_state(path)
    if error or state is None:
        raise RuntimeError(error or "REAL_SIGNAL_STATE_UNAVAILABLE")
    return state

def _save_updated(state,path):
    state["updated_at"]=now().isoformat()
    save_state(state,path)

def _reconcile_claims(state,real):
    for signal_id,claim in state["claims"].items():
        if claim["status"] in ("EXECUTED","WOULD_EXECUTE"):
            continue
        token=claim["token"]
        position=real.get("positions",{}).get(token)
        pending=real.get("pending_buys",{}).get(token)
        if isinstance(position,dict):
            claim.update(status="EXECUTED",tx_hash=position.get("buy_tx") or claim.get("tx_hash"))
            if signal_id not in state["consumed_signal_ids"]:
                state["consumed_signal_ids"].append(signal_id)
        elif isinstance(pending,dict):
            claim.update(status="BROADCAST_PENDING",tx_hash=pending.get("tx_hash") or claim.get("tx_hash"))
        if claim["status"] in ("BROADCAST_PENDING","UNKNOWN") and claim.get("tx_hash"):
            completed=any(isinstance(trade,dict) and trade.get("action")=="BUY"
                and trade.get("tx")==claim["tx_hash"] for trade in real.get("trades",[]))
            if completed:
                claim["status"]="EXECUTED"
                if signal_id not in state["consumed_signal_ids"]:
                    state["consumed_signal_ids"].append(signal_id)
            else:
                try:
                    receipt=real_trader.rpc("eth_getTransactionReceipt",[claim["tx_hash"]])
                    if not receipt:
                        claim["reconciliation"]="PENDING_RECEIPT"
                    elif int(receipt.get("status","0x0"),16)==1:
                        claim["reconciliation"]="CONFIRMED_AWAITING_ACCOUNTING"
                    else:
                        # A mined revert is terminal for this intent. Release the
                        # token claim for a later, distinct market observation while
                        # retaining the failed hash for audit and dedup history.
                        claim.update(status="FAILED_RETRYABLE",
                                     reconciliation="REVERTED_REQUIRES_RECONCILIATION",
                                     resolved_at=now().isoformat())
                except (OSError,ValueError,TypeError,RuntimeError,AttributeError) as exc:
                    claim["reconciliation"]="RECEIPT_UNAVAILABLE:"+type(exc).__name__

def _claim_block(state,signal_id,token,at):
    claim=state["claims"].get(signal_id)
    if signal_id in state["consumed_signal_ids"] or (claim and claim["status"] in ("EXECUTED","WOULD_EXECUTE")):
        return "DUPLICATE_BLOCKED"
    for previous in state["claims"].values():
        if previous["token"]!=token:
            continue
        if previous["status"] in ("BROADCAST_PENDING","UNKNOWN"):
            return "ALREADY_CLAIMED"
        if previous["status"]=="CLAIMED" and stamp(previous["expires_at"])>at:
            return "ALREADY_CLAIMED"
    return None

def _reserved_real(state,real,at):
    reserved=copy.deepcopy(real)
    for claim in state["claims"].values():
        active=(claim["status"] in ("BROADCAST_PENDING","UNKNOWN")
            or (claim["status"]=="CLAIMED" and stamp(claim["expires_at"])>at))
        token=claim["token"]
        if active and token not in reserved["positions"] and token not in reserved["pending_buys"]:
            reserved["pending_buys"][token]={"position_sda":claim["position_sda"]}
    return reserved

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
    signal_rows=state["signal_history"].setdefault(token,[])
    signal_rows.append({"signal":signal,"temporal_ready":bool(temporal.get("ready")),
        "temporal_reason":temporal.get("reason"),"lane":temporal.get("lane"),
        "observed_at":at.isoformat()})
    state["signal_history"][token]=signal_rows[-50:]
    state["temporal_history"]=dict(list(state["temporal_history"].items())[-5000:])
    market=_market_gates(token,analysis,signal,temporal,at)
    market_block=market["blocked"]
    until=stamp(state["cooldowns"].get(token)); cooldown=bool(until and until>at)
    try: daily=float(real_state.get("daily_realized_pnl_sda") or 0)
    except (ValueError,TypeError): daily=float("-inf")
    risk_block=not math.isfinite(daily) or daily<=-float(cfg.MAX_DAILY_LOSS_SDA)
    signal_id=signal_identity(token,signal["quote_at"])
    legacy_id=hashlib.sha256(f"{token}|{signal['quote_at']}|{strategy.VERSION}|REAL".encode()).hexdigest() if signal["quote_at"] else None
    duplicate=bool(signal_id and (signal_id in state["consumed_signal_ids"] or legacy_id in state["consumed_signal_ids"]))
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

def _mark_unavailable(state_path,status):
    with state_lock(state_path):
        state=_required_state(state_path)
        state["market_data_status"]=status
        for token in state["buy_ready"]:
            state["buy_ready"][token]={"ready":False,"reason":status,"at":now().isoformat()}
        _save_updated(state,state_path)

def _prepare_scan(tokens,execute,state_path):
    reservations=[]; actions=[]; ready=0
    with state_lock(state_path):
        state=_required_state(state_path)
        real=real_trader.load_state()
        _reconcile_claims(state,real)
        state["market_data_status"]="OK"
        state["buy_ready"]={}
        for token,row in tokens.items():
            if not isinstance(row,dict):
                continue
            token=str(token).lower(); at=now()
            signal_id=signal_identity(token,row.get("last_transaction"))
            blocked=_claim_block(state,signal_id,token,at)
            if blocked:
                actions.append({"token":token,"status":blocked,"signal_id":signal_id})
                state["buy_ready"][token]={"ready":False,"reason":blocked,"signal_id":signal_id,"at":at.isoformat()}
                continue
            result=evaluate_candidate(token,row,state,_reserved_real(state,real,at))
            if not result["ready"]:
                continue
            if signal_id is None or result["signal_id"]!=signal_id:
                state["buy_ready"][token]={"ready":False,"reason":"SIGNAL_IDENTITY_MISMATCH","at":at.isoformat()}
                continue
            ready+=1
            if not execute or len(reservations)>=MAX_BUYS_PER_SCAN:
                actions.append({"token":token,"status":"BUY_READY_ONLY","signal_id":signal_id})
                continue
            claim={"signal_id":signal_id,"token":token,"claimed_at":at.isoformat(),
                "status":"CLAIMED","worker_id":f"{socket.gethostname()}:{os.getpid()}:{os.getenv('GITHUB_RUN_ID','local')}:{uuid.uuid4().hex}",
                "claim_id":uuid.uuid4().hex,"expires_at":(at+timedelta(seconds=CLAIM_TTL_SECONDS)).isoformat(),
                "chain_id":cfg.CHAIN_ID,"strategy_version":strategy.VERSION,
                "quote_at":row.get("last_transaction"),"position_sda":cfg.position_sda()}
            state["claims"][signal_id]=claim
            reservations.append({**claim,"symbol":str(row.get("symbol") or row.get("ticker") or token[:8])})
        _save_updated(state,state_path)
    return ready,actions,reservations

def _owned_claim(state,reservation):
    claim=state["claims"].get(reservation["signal_id"])
    if not claim or claim["claim_id"]!=reservation["claim_id"] or claim["worker_id"]!=reservation["worker_id"]:
        raise RuntimeError("REAL_CLAIM_OWNERSHIP_CHANGED")
    return claim

def _begin_execution(reservation,state_path):
    with state_lock(state_path):
        state=_required_state(state_path)
        claim=_owned_claim(state,reservation)
        if claim["status"]!="CLAIMED" or stamp(claim["expires_at"])<=now():
            raise RuntimeError("REAL_CLAIM_NOT_EXECUTABLE")
        real=real_trader.load_state()
        if reservation["token"] in real["positions"] or reservation["token"] in real["pending_buys"]:
            raise RuntimeError("REAL_POSITION_OR_PENDING_BUY")
        claim.update(status="BROADCAST_PENDING",execution_started_at=now().isoformat())
        _save_updated(state,state_path)

def _finish_claim(reservation,state_path,status,response=None,error=None):
    with state_lock(state_path):
        state=_required_state(state_path)
        claim=_owned_claim(state,reservation)
        if claim["status"]=="EXECUTED":
            return
        claim.update(status=status,updated_at=now().isoformat())
        if response and response.get("tx"):
            claim["tx_hash"]=response["tx"]
        if error:
            claim["error"]=type(error).__name__
        if status in ("BROADCAST_PENDING","UNKNOWN"):
            try:
                pending=real_trader.load_state().get("pending_buys",{}).get(reservation["token"],{})
                if pending.get("tx_hash"):
                    claim["tx_hash"]=pending["tx_hash"]
            except (OSError,ValueError,TypeError,RuntimeError) as exc:
                claim["intent_read_error"]=type(exc).__name__
        if status=="EXECUTED":
            if reservation["signal_id"] not in state["consumed_signal_ids"]:
                state["consumed_signal_ids"].append(reservation["signal_id"])
            state["cooldowns"][reservation["token"]]=(now()+timedelta(hours=COOLDOWN_HOURS)).isoformat()
        previous=state["buy_ready"].get(reservation["token"],{})
        state["buy_ready"][reservation["token"]]={**previous,"ready":status=="WOULD_EXECUTE",
            "reason":"READY" if status=="WOULD_EXECUTE" else "REAL_CLAIM_"+status,
            "signal_id":reservation["signal_id"],"at":now().isoformat()}
        _save_updated(state,state_path)

def _execute_reserved(reservation,state_path):
    token=reservation["token"]
    action={"token":token,"signal_id":reservation["signal_id"]}
    try:
        real_trader.safety_check(reservation["position_sda"],real_trader.load_state())
    except (OSError,ValueError,TypeError,RuntimeError) as exc:
        _finish_claim(reservation,state_path,"FAILED_RETRYABLE",error=exc)
        return {**action,"status":"SAFETY_OR_EXECUTION_BLOCKED","error":type(exc).__name__}
    _begin_execution(reservation,state_path)
    try:
        response=real_trader.execute_buy(token,reservation["symbol"],reservation["position_sda"])
        status=response.get("status","UNKNOWN")
    except real_trader.BuyPreBroadcastError as exc:
        _finish_claim(reservation,state_path,"FAILED_RETRYABLE",error=exc)
        return {**action,"status":"SAFETY_OR_EXECUTION_BLOCKED","claim_status":"FAILED_RETRYABLE","error":type(exc).__name__}
    except (OSError,ValueError,TypeError,RuntimeError) as exc:
        _finish_claim(reservation,state_path,"UNKNOWN",error=exc)
        return {**action,"status":"SAFETY_OR_EXECUTION_BLOCKED","claim_status":"UNKNOWN","error":type(exc).__name__}
    outcome={"CONFIRMED":"EXECUTED","ALREADY_OPEN":"EXECUTED","DRY_RUN":"WOULD_EXECUTE",
        "PENDING_CONFIRMATION":"BROADCAST_PENDING"}.get(status,"UNKNOWN")
    _finish_claim(reservation,state_path,outcome,response=response)
    return {**action,"status":"WOULD EXECUTE" if outcome=="WOULD_EXECUTE" else status,"claim_status":outcome}

def run_scan(market_path="market_analysis.json",execute=True,state_path=STATE):
    if os.getenv("REAL_TRADING_ENABLED","false").lower()!="true":
        try:
            _mark_unavailable(state_path,"REAL_TRADING_DISABLED")
        except (OSError,ValueError,TypeError,RuntimeError) as exc:
            return {"status":"disabled","state_error":type(exc).__name__,"buy_ready":0,"actions":[]}
        return {"status":"disabled","buy_ready":0,"actions":[]}
    # Resolve all persisted BUY intents before market-data gates. A restart must
    # reconcile confirmed/pending transactions even when the current scan cannot
    # produce a fresh signal. recover_pending_buys is read-only on-chain and never
    # creates or broadcasts a new transaction.
    try:
        recovery_actions = real_trader.recover_pending_buys()
    except (OSError,ValueError,TypeError,RuntimeError) as exc:
        return {"status":"pending_buy_recovery_error","error":type(exc).__name__,
                "buy_ready":0,"actions":[]}
    if os.getenv("SDA_SCANNER_BUY_DATA_AVAILABLE","true").strip().lower()!="true":
        try:
            _mark_unavailable(state_path,"MARKET_DATA_DEGRADED")
        except (OSError,ValueError,TypeError,RuntimeError) as exc:
            return {"status":"market_data_degraded","state_error":type(exc).__name__,"buy_ready":0,"actions":recovery_actions}
        return {"status":"market_data_degraded","buy_ready":0,"actions":recovery_actions}
    try:
        market=json.loads(Path(market_path).read_text(encoding="utf-8"))
        tokens=market.get("tokens")
        if not isinstance(tokens,dict): raise TypeError("tokens missing")
    except Exception as exc:  # noqa: BLE001 - malformed input or REAL ledger failure must fail closed
        try:
            _mark_unavailable(state_path,"INPUT_ERROR")
        except (OSError,ValueError,TypeError,RuntimeError) as state_error:
            return {"status":"input_error","error":type(exc).__name__,"state_error":type(state_error).__name__,"buy_ready":0,"actions":recovery_actions}
        return {"status":"input_error","error":type(exc).__name__,"buy_ready":0,"actions":recovery_actions}
    try:
        ready,actions,reservations=_prepare_scan(tokens,execute,state_path)
    except (OSError,ValueError,TypeError,RuntimeError,KeyError) as exc:
        return {"status":"state_error","error":str(exc)[:180],"buy_ready":0,"actions":recovery_actions}
    actions.extend(recovery_actions)
    for reservation in reservations:
        try:
            action=_execute_reserved(reservation,state_path)
        except (OSError,ValueError,TypeError,RuntimeError,KeyError) as exc:
            action={"token":reservation["token"],"status":"CLAIM_STATE_ERROR","error":type(exc).__name__}
        actions.append(action)
        if action["status"]!="WOULD EXECUTE":
            ready-=1
    return {"status":"ok","buy_ready":ready,"actions":actions,"paper_state_used":False}

def main():
    print("REAL BUY PIPELINE:",json.dumps(run_scan(),ensure_ascii=False,sort_keys=True))

if __name__=="__main__": main()
