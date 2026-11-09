"""Read-only Swing SELL V2 decision preview for REAL open positions."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
import swing_sell_v2 as policy

MAX_PRICE_AGE_SECONDS = 300
UNRESOLVED_INTENT_STAGES = {"DECIDED","APPROVAL_SUBMITTED","SELL_READY","SWAP_SUBMITTED","SWAP_CONFIRMED","UNWRAP_SUBMITTED","RECONCILED_READY","UNKNOWN"}

def _stamp(value):
    try:
        result=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if result.tzinfo is None: return None
        return result.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError): return None

def _decimal(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,TypeError,ValueError): return None

def scanner_market_price(token,analysis,now=None,max_age=MAX_PRICE_AGE_SECONDS):
    now=now or datetime.now(timezone.utc)
    tokens=analysis.get("tokens") if isinstance(analysis,dict) else {}
    item=tokens.get(str(token).lower()) if isinstance(tokens,dict) else None
    if not isinstance(item,dict): return None
    price=_decimal(item.get("price_in_sda"))
    stamp=_stamp(item.get("last_transaction") or item.get("last_transaction_at") or item.get("price_timestamp"))
    if price is None or price<=0 or stamp is None: return None
    age=(now.astimezone(timezone.utc)-stamp).total_seconds()
    if age<0 or age>max_age: return None
    return {"price_sda":float(price),"timestamp":stamp.isoformat(),"age_seconds":age,"source":"scanner_market_analysis"}

def _supabase_market_price(token,lookup,now,max_age):
    if not callable(lookup): return None
    try: item=lookup(token)
    except Exception: return None
    if not isinstance(item,dict): return None
    price=_decimal(item.get("price_sda")); stamp=_stamp(item.get("timestamp"))
    if price is None or price<=0 or stamp is None: return None
    age=(now.astimezone(timezone.utc)-stamp).total_seconds()
    if age<0 or age>max_age: return None
    return {"price_sda":float(price),"timestamp":stamp.isoformat(),"age_seconds":age,"source":"supabase_transaction"}

def resolve_decision_price(token,analysis,now=None,supabase_lookup=None,max_age=MAX_PRICE_AGE_SECONDS):
    now=now or datetime.now(timezone.utc)
    return scanner_market_price(token,analysis,now,max_age) or _supabase_market_price(token,supabase_lookup,now,max_age)

def _active_intent(item):
    if not isinstance(item,dict): return False
    stage=str(item.get("stage") or "").upper()
    status=str(item.get("status") or "").upper()
    return stage in UNRESOLVED_INTENT_STAGES or status in {"SUBMITTED","UNKNOWN","DECIDED","PENDING"}

def _action(decision,trailing_armed):
    if decision.get("action")=="PARTIAL_SELL": return "PARTIAL_SELL_20"
    if decision.get("action")=="FULL_SELL":
        return {"LOSS_LIMIT":"FULL_SELL_EMERGENCY","TRAILING_STOP":"FULL_SELL_TRAILING","HARD_TP":"FULL_SELL_TP"}.get(decision.get("reason"),"SELL_QUARANTINED")
    return "TRAILING_HOLD" if trailing_armed else "HOLD"

def build_decision_rows(state,analysis,metadata,now=None,supabase_lookup=None):
    now=now or datetime.now(timezone.utc)
    positions=state.get("positions") if isinstance(state,dict) else {}
    positions=positions if isinstance(positions,dict) else {}
    pending=state.get("pending_sells") if isinstance(state,dict) else {}
    pending=pending if isinstance(pending,dict) else {}
    metadata=metadata if isinstance(metadata,dict) else {}
    migration=_stamp((state or {}).get("sell_migration_observation_started_at") or (state or {}).get("sell_strategy_migrated_at"))
    rows=[]
    tokens=[str(token).lower() for token,position in positions.items() if isinstance(position,dict)]
    scanner_prices={token:scanner_market_price(token,analysis,now) for token in tokens}
    fallback_tokens=[token for token in tokens if scanner_prices.get(token) is None]
    fallback_prices={}
    if callable(supabase_lookup) and fallback_tokens:
        with ThreadPoolExecutor(max_workers=min(8,len(fallback_tokens))) as pool:
            values=list(pool.map(lambda token:_supabase_market_price(token,supabase_lookup,now,MAX_PRICE_AGE_SECONDS),fallback_tokens))
        fallback_prices=dict(zip(fallback_tokens,values))
    for token,position in positions.items():
        if not isinstance(position,dict): continue
        token=str(token).lower()
        meta=metadata.get(token) if isinstance(metadata.get(token),dict) else {}
        symbol=str(meta.get("symbol") or position.get("symbol") or token[:10])
        try:
            decimals=int(meta.get("decimals"))
            quantity=int(position.get("remaining_quantity",position.get("token_amount_raw")))
            cost=_decimal(position.get("remaining_cost_basis",position.get("amount_sda",position.get("cost_sda"))))
        except (TypeError,ValueError,OverflowError):
            decimals,quantity,cost=-1,0,None
        valid=0<=decimals<=36 and quantity>0 and cost is not None and cost>0
        entry=cost/(Decimal(quantity)/(Decimal(10)**decimals)) if valid else None
        partial=bool(position.get("profit_partial_done")); trailing=bool(position.get("trailing_armed"))
        peak=_decimal(position.get("highest_price_since_trailing_activation"))
        trail=peak*Decimal("0.85") if trailing and peak and peak>0 else None
        market=scanner_prices.get(token) or fallback_prices.get(token)
        if market and migration:
            observed=_stamp(market.get("timestamp"))
            if observed is None or observed<=migration: market=None
        price=_decimal(market.get("price_sda")) if market else None
        pnl=None; swing_state="WAITING_PARTIAL"; action="PRICE_UNAVAILABLE"
        intent=pending.get(token); wallet_qty=position.get("wallet_available_quantity")
        mismatch=False
        if wallet_qty is not None:
            try: mismatch=int(wallet_qty)!=quantity
            except (TypeError,ValueError,OverflowError): mismatch=True
        if _active_intent(intent):
            swing_state,action="SELL_INTENT_PENDING","SELL_QUARANTINED"
        elif mismatch or not valid:
            swing_state,action="WALLET_RECONCILIATION_REQUIRED","SELL_QUARANTINED"
        elif price is not None and entry is not None:
            fraction=price/entry-Decimal(1); pnl=float(fraction*100)
            preview=dict(position)
            if trailing:
                preview=policy.update_trailing_peak(preview,price)
                peak=_decimal(preview.get("highest_price_since_trailing_activation"))
                trail=peak*Decimal("0.85") if peak else None
            result=policy.decide(preview,fraction,price)
            action=_action(result,trailing)
            swing_state="TRAILING_ACTIVE" if trailing else ("PARTIAL_DONE" if partial else "WAITING_PARTIAL")
        else: swing_state="PRICE_UNAVAILABLE"
        rows.append({"token":symbol,"token_address":token,"entry_price":float(entry) if entry is not None else None,
            "decision_price":float(price) if price is not None else None,"price_timestamp":market.get("timestamp") if market else None,
            "price_age_seconds":market.get("age_seconds") if market else None,"price_source":market.get("source") if market else None,
            "pnl_pct":pnl,"profit_partial_done":partial,"trailing_armed":trailing,
            "current_peak":float(peak) if peak is not None else None,"trailing_price":float(trail) if trail is not None else None,
            "swing_state":swing_state,"action":action})
    return rows
