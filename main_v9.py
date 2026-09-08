# SDA Scanner v9 wrapper
# Runs the existing paper-trading engine but replaces only the real-wallet
# portfolio/P&L layer with PinetSwap's exact wallet trade ledger query.
import os
from datetime import datetime, timezone
import requests
import main as engine

PINET_URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
PINET_HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public",
}
MAX_ROWS = int(os.environ.get("PINET_WALLET_TRADE_LIMIT", "5000"))
PAGE = 1000

def _num(v, d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d

def _now(): return datetime.now(timezone.utc).isoformat()

def fetch_trades(wallet):
    wallet = str(wallet).lower()
    rows=[]; offset=0; last_status=None; error=None
    while len(rows) < MAX_ROWS:
        take=min(PAGE, MAX_ROWS-len(rows))
        params={
            "select":"tx_hash,token_address,amount,volume_in_sda,price_in_sda,block_number,tx_timestamp,tx_type",
            "or":f"(and(tx_type.eq.buy,to_address.eq.{wallet}),and(tx_type.eq.sell,from_address.eq.{wallet}))",
            "order":"block_number.asc",
            "offset":str(offset),
            "limit":str(take),
        }
        try:
            r=requests.get(PINET_URL,params=params,headers=PINET_HEADERS,timeout=30)
            last_status=r.status_code
            r.raise_for_status()
            page=r.json()
            if not isinstance(page,list):
                error=f"Unexpected JSON type: {type(page).__name__}"
                break
            rows.extend(page)
            if len(page)<take: break
            offset += len(page)
        except Exception as e:
            error=f"HTTP {last_status}: {e}"
            break
    return rows[:MAX_ROWS], last_status, error

def decimals_for(token, raw_amount, current_amount, meta):
    try:
        x=meta.get(token,{}) if isinstance(meta,dict) else {}
        for k in ("decimals","token_decimals","decimal"):
            if isinstance(x,dict) and x.get(k) is not None:
                return int(x[k])
    except Exception: pass
    if raw_amount and current_amount and current_amount>0:
        best=18; err=float("inf")
        for d in range(0,19):
            q=raw_amount/(10**d)
            e=abs(q-current_amount)/max(current_amount,1e-12)
            if e<err: err=e; best=d
        if err<0.01: return best
    return 18

def portfolio_history_v9(wallet, md, meta, ld, previous=None, wallet_obj=None):
    wallet_obj=wallet_obj if isinstance(wallet_obj,dict) else engine.wallet_snapshot(md,meta)
    rows,status,error=fetch_trades(wallet)
    current_wallet={str(h.get("address","")).lower():h for h in wallet_obj.get("holdings",[]) if h.get("address")}
    buys=[]; sells=[]
    for x in rows:
        if not isinstance(x,dict): continue
        side=str(x.get("tx_type") or "").lower()
        token=str(x.get("token_address") or "").lower()
        if side not in ("buy","sell") or not token: continue
        raw=_num(x.get("amount")); vol=_num(x.get("volume_in_sda")); px=_num(x.get("price_in_sda"))
        if raw<=0 or vol<=0: continue
        cur_amt=_num((current_wallet.get(token) or {}).get("amount"))
        dec=decimals_for(token,raw,cur_amt,meta)
        amount=raw/(10**dec)
        if amount<=0 and px>0: amount=vol/px
        tr={"tx":str(x.get("tx_hash") or ""),"block":int(_num(x.get("block_number"))),"timestamp":str(x.get("tx_timestamp") or ""),"token":token,"symbol":(current_wallet.get(token) or {}).get("symbol") or (meta.get(token,{}) or {}).get("symbol") or token[:10]+"...","amount":amount,"price_sda":px,"side":"BUY" if side=="buy" else "SELL","decimals":dec}
        if not tr["tx"]: continue
        if side=="buy": tr["cost_sda"]=vol; buys.append(tr)
        else: tr["proceeds_sda"]=vol; sells.append(tr)
    trades=sorted(buys+sells,key=lambda z:(z["block"],z["timestamp"],z["tx"]))
    lots={}; realized=0.0; history=[]; unmatched=0.0
    for tr in trades:
        token=tr["token"]
        if tr["side"]=="BUY":
            lots.setdefault(token,[]).append({"amount":tr["amount"],"cost_sda":tr["cost_sda"]}); history.append(tr); continue
        q=lots.setdefault(token,[]); qty=tr["amount"]; cost_removed=0.0
        while qty>1e-12 and q:
            lot=q[0]; take=min(qty,lot["amount"]); unit=lot["cost_sda"]/lot["amount"] if lot["amount"] else 0.0
            cost_removed += take*unit; lot["amount"]-=take; lot["cost_sda"]-=take*unit; qty-=take
            if lot["amount"]<=1e-12: q.pop(0)
        tr["cost_basis_sda"]=cost_removed; tr["unmatched_amount"]=qty
        if qty>1e-12: unmatched += qty
        realized += tr["proceeds_sda"]-cost_removed
        history.append(tr)
    current={}
    for token,q in lots.items():
        amount=sum(z["amount"] for z in q); cost=sum(z["cost_sda"] for z in q)
        if amount<=1e-12: continue
        h=current_wallet.get(token,{})
        age=None
        first=next((z["timestamp"] for z in buys if z["token"]==token and z["timestamp"]),"")
        if first:
            try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(first.replace("Z","+00:00"))).total_seconds()/86400
            except Exception: pass
        current[token]={"amount":_num(h.get("amount"),amount),"reconstructed_amount":amount,"balance_delta":_num(h.get("amount"),amount)-amount,"cost_sda":cost,"avg_cost_sda":cost/amount if amount else 0.0,"lots":q,"first_buy_at":first,"last_buy_at":next((z["timestamp"] for z in reversed(buys) if z["token"]==token and z["timestamp"]),""),"age_days":age,"symbol":h.get("symbol") or token[:10]+"...","price_sda":_num(h.get("price_sda")),"value_sda":h.get("value_sda"),"cost_basis_status":"DETECTED"}
        if current[token]["value_sda"] is not None:
            current[token]["unrealized_pnl_sda"]=_num(current[token]["value_sda"])-cost
            current[token]["unrealized_pnl_pct"]=current[token]["unrealized_pnl_sda"]/cost*100 if cost else None
    for token,h in current_wallet.items():
        if token not in current:
            current[token]={"amount":_num(h.get("amount")),"reconstructed_amount":0.0,"balance_delta":_num(h.get("amount")),"cost_sda":None,"avg_cost_sda":None,"lots":[],"first_buy_at":"","last_buy_at":"","age_days":None,"symbol":h.get("symbol"),"price_sda":_num(h.get("price_sda")),"value_sda":h.get("value_sda"),"unrealized_pnl_sda":None,"unrealized_pnl_pct":None,"cost_basis_status":"UNKNOWN"}
    return {"wallet":wallet,"router":"pinet-supabase-wallet-query-v9","updated_at":_now(),"buy_count":len(buys),"sell_count":len(sells),"trades":history[-500:],"current":current,"realized_pnl_sda":realized,"source":"PinetSwap token_transactions exact wallet query","source_rows":len(rows),"http_status":status,"query_error":error,"unmatched_sell_amount":unmatched,"ledger_ok":bool(rows) and error is None}

def portfolio_message_v9(portfolio, recommendations):
    text=engine.portfolio_message(portfolio,recommendations)
    diag=["","🔧 PinetSwap P/L DEBUG",f"Ledger HTTP: {portfolio.get('http_status')}",f"Ledger rows: {portfolio.get('source_rows',0)}",f"Detected: {portfolio.get('buy_count',0)} BUY / {portfolio.get('sell_count',0)} SELL",f"Source: {portfolio.get('source','UNKNOWN')}"]
    if portfolio.get("query_error"): diag.append(f"⚠️ Query error: {portfolio['query_error'][:500]}")
    return text+"\n"+"\n".join(diag)

engine.portfolio_history=portfolio_history_v9
engine.portfolio_message=portfolio_message_v9

if __name__=="__main__":
    engine.main()
