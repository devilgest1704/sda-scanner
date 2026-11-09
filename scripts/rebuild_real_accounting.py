#!/usr/bin/env python3
"""Read-only Sidra blockchain accounting reconstruction. No signing or trading."""
import os, json, time, hashlib, requests
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE=os.getenv("SIDRA_EXPLORER_API","https://ledger.sidrachain.com/api/v2").rstrip("/")
RPC=os.getenv("SIDRA_RPC_URL","https://node.sidrachain.com")
WALLET=os.getenv("REAL_ACCOUNTING_WALLET","0x9e3643f2ac15c91ee83aa2814015f15b18d573f2").lower()
WSDA=os.getenv("SIDRA_WSDA_ADDRESS","0xE4095a910209D7BE03B55D02F40d4554B1666182").lower()
ROUTER=os.getenv("SIDRA_ROUTER_ADDRESS","0x35cAC72Db00e8dAC0e4f7F8A0F53D339E0cC23fb").lower()
CHAIN=97453
ZERO="0x"+"0"*40
OUT=Path("accounting_evidence"); OUT.mkdir(exist_ok=True)
S=requests.Session(); S.headers.update({"User-Agent":"sda-real-accounting-readonly/2.0","Accept":"application/json"})
REQ=0

def get(path,params=None):
    global REQ
    for n in range(6):
        try:
            r=S.get(BASE+path,params=params,timeout=(15,90)); REQ+=1
            if r.status_code==429:
                time.sleep(2**n); continue
            r.raise_for_status(); return r.json()
        except Exception:
            if n==5: raise
            time.sleep(2**n)

def paged(path,params=None):
    p=dict(params or {}); rows=[]; pages=0; seen=set()
    while True:
        d=get(path,p)
        if not isinstance(d,dict) or not isinstance(d.get("items"),list):
            raise RuntimeError("Unexpected paginated response: "+path)
        rows.extend(d["items"]); pages+=1
        nxt=d.get("next_page_params")
        if not nxt: return rows,pages
        k=json.dumps(nxt,sort_keys=True)
        if k in seen: raise RuntimeError("Pagination cursor loop: "+path)
        seen.add(k); p=dict(nxt)
        if pages>100000: raise RuntimeError("Pagination cap: "+path)

def rpc(method,params):
    r=requests.post(RPC,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
        timeout=(10,30),headers={"Content-Type":"application/json","User-Agent":"sda-accounting-readonly/2.0"})
    r.raise_for_status(); d=r.json()
    if d.get("error"): raise RuntimeError(str(d["error"]))
    return d.get("result")

def addr(x):
    if isinstance(x,dict): return str(x.get("hash") or x.get("address_hash") or "").lower()
    return str(x or "").lower()

def dec(raw,places):
    return Decimal(str(raw or "0"))/(Decimal(10)**int(places or 0))

def rawval(v):
    if isinstance(v,dict): return v.get("value",v.get("raw",0))
    return v or 0

def dump(name,obj):
    p=OUT/name; p.write_text(json.dumps(obj,indent=2,sort_keys=True,default=str)+"\n",encoding="utf-8")
    return hashlib.sha256(p.read_bytes()).hexdigest()

def word(v): return f"{int(v):064x}"
def aword(a): return str(a).lower().replace("0x","").rjust(64,"0")
def calldata(token_in,token_out,fee,amount,deadline):
    return "0x414bf389"+aword(token_in)+aword(token_out)+word(fee)+aword(WALLET)+word(deadline)+word(amount)+word(0)+word(0)

def load_fee_preferences():
    try:
        d=json.loads(Path("liquidity_data.json").read_text())
        q=d.get("quotes_50_sda",{})
        out={}
        for token,item in q.items():
            if isinstance(item,dict) and str(item.get("source","")).lower() in {"historical_token","historical_buy","live_pool_probe"}:
                fee=int(item.get("fee_tier",item.get("fee",0)) or 0)
                if fee in {100,500,3000,10000}: out[token.lower()]=fee
        return out
    except Exception: return {}

FEE_PREF=load_fee_preferences()

def quote(token_in,token_out,amount,value,preferred=None):
    fees=([preferred] if preferred else [])+[x for x in (100,500,3000,10000) if x!=preferred]
    errors=[]; good=[]
    for fee in fees:
        try:
            data=calldata(token_in,token_out,fee,amount,int(time.time())+120)
            result=rpc("eth_call",[{"from":WALLET,"to":ROUTER,"data":data,"value":hex(value)},"latest"])
            n=int(result,16)
            if n>0:
                if fee==preferred: return {"amount_out_raw":str(n),"fee":fee,"error":None}
                good.append((n,fee))
        except Exception as e: errors.append(f"{fee}:{str(e)[:100]}")
    if good:
        n,fee=max(good,key=lambda x:x[0]); return {"amount_out_raw":str(n),"fee":fee,"error":None}
    return {"amount_out_raw":None,"fee":None,"error":"; ".join(errors[:4]) or "no quote"}

def main():
    chain=int(rpc("eth_chainId",[]),16)
    if chain!=CHAIN: raise RuntimeError(f"Wrong chain id {chain}")
    address=get(f"/addresses/{WALLET}")
    txs,tp=paged(f"/addresses/{WALLET}/transactions")
    transfers,xp=paged(f"/addresses/{WALLET}/token-transfers")
    internal,ip=paged(f"/addresses/{WALLET}/internal-transactions")
    holdings,hp=paged(f"/addresses/{WALLET}/tokens",{"type":"ERC-20"})
    hashes=sorted({str(x.get("hash") or "").lower() for x in txs if x.get("hash")} | {str(x.get("transaction_hash") or "").lower() for x in transfers if x.get("transaction_hash")} | {str(x.get("transaction_hash") or "").lower() for x in internal if x.get("transaction_hash")})
    details={}; errors={}
    for i,h in enumerate(hashes,1):
        try: details[h]=get(f"/transactions/{h}")
        except Exception as e: errors[h]=str(e)
        if i%25==0: print(f"Details {i}/{len(hashes)}",flush=True)
    logs={}; txtrans={}; txinternal={}
    for i,h in enumerate(hashes,1):
        for name,path,dest in [
            ("logs",f"/transactions/{h}/logs",logs),
            ("token_transfers",f"/transactions/{h}/token-transfers",txtrans),
            ("internal_transactions",f"/transactions/{h}/internal-transactions",txinternal)]:
            try: dest[h]=get(path)
            except Exception as e: dest[h]={"fetch_error":str(e)}
    native=int(rpc("eth_getBalance",[WALLET,"latest"]),16)
    token_meta={}
    for item in holdings:
        t=item.get("token") or {}
        token_meta[addr(t)]={"symbol":t.get("symbol"),"name":t.get("name"),
            "decimals":int(t.get("decimals") or 0),"address":addr(t)}
    evidence={"meta":{"chain_id":chain,"wallet":WALLET,"wsda":WSDA,"router":ROUTER,
        "retrieved_at":datetime.now(timezone.utc).isoformat(),"request_count":REQ,
        "pagination":{"transactions_pages":tp,"token_transfer_pages":xp,
            "internal_transaction_pages":ip,"token_holdings_pages":hp},
        "counts":{"transactions":len(txs),"token_transfers":len(transfers),
            "internal_transactions":len(internal),"transaction_details":len(details),
            "transaction_detail_errors":len(errors),"token_holdings":len(holdings)},
        "native_balance_wei":str(native),"native_balance_sda":str(Decimal(native)/Decimal(10**18))},
        "address":address,"transactions":txs,"token_transfers":transfers,
        "internal_transactions":internal,"token_holdings":{"items":holdings,"next_page_params":None},
        "transaction_details":details,"transaction_logs":logs,
        "transaction_token_transfers":txtrans,"transaction_internal_transactions":txinternal,
        "detail_errors":errors}
    raw_sha=dump("blockchain_raw.json",evidence)

    byhash=defaultdict(list)
    for tr in transfers: byhash[str(tr.get("transaction_hash") or "").lower()].append(tr)
    def tokenof(tr):
        t=tr.get("token") or {}; return addr(t),str(t.get("symbol") or "UNKNOWN"),int(t.get("decimals") or 0)
    def human(tr): 
        a,sym,places=tokenof(tr); return dec(rawval(tr.get("total",tr.get("value"))),places)
    def logs_for(h):
        obj=logs.get(h,{})
        return obj.get("items",[]) if isinstance(obj,dict) else (obj if isinstance(obj,list) else [])
    def wsda_flows(h):
        out=[]
        for lg in logs_for(h):
            if addr(lg.get("address"))!=WSDA: continue
            ps={p.get("name"):p.get("value") for p in (lg.get("decoded") or {}).get("parameters",[])}
            if ps.get("value") is None: continue
            out.append((str(ps.get("from") or "").lower(),str(ps.get("to") or "").lower(),dec(ps["value"],18)))
        return out
    txmap={str(t.get("hash") or "").lower():t for t in txs}
    records=[]; classifications=[]
    for h in hashes:
        tx=txmap.get(h,{}); d=details.get(h,{})
        status=str(d.get("status") or tx.get("status") or "").lower()
        result=str(d.get("result") or tx.get("result") or "").lower()
        success=status=="ok" and result=="success"
        method=str((d.get("decoded_input") or {}).get("method_call") or d.get("method") or tx.get("method") or "").lower()
        fr,to=addr(d.get("from") or tx.get("from")),addr(d.get("to") or tx.get("to"))
        value=dec(d.get("value",tx.get("value","0")),18)
        wallet_deltas=[]; incoming=[]; outgoing=[]
        for tr in byhash.get(h,[]):
            tf,tt=addr(tr.get("from")),addr(tr.get("to")); contract,sym,places=tokenof(tr); q=human(tr)
            if tt==WALLET:
                item={"contract":contract,"symbol":sym,"quantity":str(q),"direction":"IN","type":tr.get("type")}
                wallet_deltas.append(item); incoming.append(item)
            elif tf==WALLET:
                item={"contract":contract,"symbol":sym,"quantity":str(q),"direction":"OUT","type":tr.get("type")}
                wallet_deltas.append(item); outgoing.append(item)
        nonws_in=[x for x in incoming if x["contract"]!=WSDA]
        nonws_out=[x for x in outgoing if x["contract"]!=WSDA]
        ws=wsda_flows(h)
        proceeds=sum((q for f,t,q in ws if t in {ROUTER,WALLET} and f!=ZERO),Decimal(0))
        burn=sum((q for f,t,q in ws if f in {ROUTER,WALLET} and t==ZERO),Decimal(0))
        kind="OTHER"
        if not success: kind="FAILED"
        elif "approve(" in method or method.startswith("approve"): kind="APPROVAL"
        elif "withdraw(" in method or method.startswith("withdraw"): kind="UNWRAP"
        elif to==ROUTER and value>0 and nonws_in and any(t==ROUTER and f==ZERO for f,t,q in ws):
            kind="BUY"
            x=nonws_in[0]
            records.append({"hash":h,"timestamp":d.get("timestamp") or tx.get("timestamp"),
                "action":"BUY","contract":x["contract"],"symbol":x["symbol"],
                "quantity":Decimal(x["quantity"]),"cost":value,"proceeds":Decimal(0),
                "gas":dec((d.get("fee") or tx.get("fee") or {}).get("value","0"),18)})
        elif to==ROUTER and nonws_out and proceeds>0:
            kind="SELL"
            x=nonws_out[0]
            records.append({"hash":h,"timestamp":d.get("timestamp") or tx.get("timestamp"),
                "action":"SELL","contract":x["contract"],"symbol":x["symbol"],
                "quantity":Decimal(x["quantity"]),"cost":Decimal(0),"proceeds":proceeds,
                "gas":dec((d.get("fee") or tx.get("fee") or {}).get("value","0"),18)})
        elif fr==WALLET and to==WALLET: kind="SELF TRANSFER"
        elif fr==WALLET and to!=ROUTER and value>0: kind="NATIVE TRANSFER OUT"
        elif to==WALLET and fr!=WALLET and value>0: kind="NATIVE TRANSFER IN"
        elif nonws_in: kind="TOKEN TRANSFER IN"
        elif nonws_out: kind="TOKEN TRANSFER OUT"
        elif value>0: kind="NATIVE TRANSFER"
        classifications.append({"hash":h,"timestamp":d.get("timestamp") or tx.get("timestamp"),
            "block":d.get("block_number") or tx.get("block_number"),"from":fr,"to":to,
            "status":status,"result":result,"success":success,"method":method,
            "classification":kind,"native_value_sda":str(value),
            "fee_sda":str(dec((d.get("fee") or tx.get("fee") or {}).get("value","0"),18)),
            "gas_used":d.get("gas_used") or tx.get("gas_used"),
            "token_deltas":wallet_deltas,"wsda_flows":[{"from":f,"to":t,"amount_sda":str(q)} for f,t,q in ws],
            "wsda_proceeds_sda":str(proceeds),"wsda_burn_sda":str(burn)})

    # Gas costs are allocated to lots, sell, approvals and linked unwrapping.
    approvals=defaultdict(list); unwraps=[]
    for h,d in details.items():
        m=str((d.get("decoded_input") or {}).get("method_call") or "")
        stamp=d.get("timestamp") or ""
        fee=dec((d.get("fee") or {}).get("value","0"),18)
        if m.startswith("approve("): approvals[addr(d.get("to"))].append({"timestamp":stamp,"fee":fee,"hash":h,"used":False})
        if m.startswith("withdraw("): unwraps.append({"timestamp":stamp,"fee":fee,"hash":h,"used":False})
    for v in approvals.values(): v.sort(key=lambda x:x["timestamp"])
    unwraps.sort(key=lambda x:x["timestamp"])
    records.sort(key=lambda x:x["timestamp"] or "")
    lots=defaultdict(deque); closed=[]; unmatched=[]
    for r in records:
        c=r["contract"]
        if r["action"]=="BUY":
            lots[c].append({"symbol":r["symbol"],"qty":r["quantity"],"remaining":r["quantity"],
                "cost":r["cost"],"buy_gas":r["gas"],"buy_hash":r["hash"],"timestamp":r["timestamp"]})
            continue
        remaining=r["quantity"]; matches=[]
        while remaining>Decimal("1e-18") and lots[c]:
            lot=lots[c][0]; take=min(remaining,lot["remaining"])
            ratio=take/lot["qty"]
            matches.append({"lot":lot,"qty":take,"cost":lot["cost"]*ratio,"gas":lot["buy_gas"]*ratio})
            lot["remaining"]-=take; remaining-=take
            if lot["remaining"]<=Decimal("1e-18"): lots[c].popleft()
        if remaining>Decimal("1e-12"):
            unmatched.append({"sell_tx":r["hash"],"token":r["symbol"],"contract":c,
                "unmatched_quantity":str(remaining),"proceeds_sda":str(r["proceeds"]),
                "reason":"No confirmed on-chain BUY lot in the retrieved wallet history"})
            continue
        basis=sum((m["cost"] for m in matches),Decimal(0))
        buygas=sum((m["gas"] for m in matches),Decimal(0))
        prev_sell=max([x["timestamp"] for x in closed if x["contract"]==c],default="")
        ap=[a for a in approvals.get(c,[]) if not a["used"] and a["timestamp"]>prev_sell and a["timestamp"]<=r["timestamp"]]
        ag=Decimal(0)
        if ap:
            a=ap[-1]; a["used"]=True; ag=a["fee"]
        ug=Decimal(0); unwrap_hash=None
        sell_dt=datetime.fromisoformat(r["timestamp"].replace("Z","+00:00"))
        for u in unwraps:
            if u["used"]: continue
            udt=datetime.fromisoformat(u["timestamp"].replace("Z","+00:00"))
            if 0 <= (udt-sell_dt).total_seconds() <= 120:
                ug=u["fee"]; unwrap_hash=u["hash"]; u["used"]=True; break
        gas=buygas+r["gas"]+ag+ug
        closed.append({"contract":c,"token":r["symbol"],"timestamp":r["timestamp"],"quantity_sold":str(r["quantity"]),
            "buy_cost_sda":str(basis),"sell_proceeds_sda":str(r["proceeds"]),
            "gross_pnl_sda":str(r["proceeds"]-basis),"gas_sda":str(gas),
            "net_pnl_sda":str(r["proceeds"]-basis-gas),
            "buy_tx":[m["lot"]["buy_hash"] for m in matches],"sell_tx":r["hash"],
            "approval_gas_sda":str(ag),"unwrap_tx":unwrap_hash,
            "gas_components":{"entry_gas":str(buygas),"sell_gas":str(r["gas"]),
                "approval_gas":str(ag),"unwrap_gas":str(ug)}})

    open_lots=[]
    for c,queue in lots.items():
        for lot in queue:
            if lot["remaining"]>Decimal("1e-18"):
                ratio=lot["remaining"]/lot["qty"]
                open_lots.append({"contract":c,"token":lot["symbol"],"quantity":str(lot["remaining"]),
                    "cost_basis_sda":str(lot["cost"]*ratio),"entry_gas_sda":str(lot["buy_gas"]*ratio),
                    "total_cost_basis_sda":str((lot["cost"]+lot["buy_gas"])*ratio),
                    "buy_tx":lot["buy_hash"],"timestamp":lot["timestamp"]})

    # Fetch the current on-chain holdings and quote every token with eth_call only.
    fee_by_token=FEE_PREF
    holding_rows=[]
    for item in holdings:
        t=item.get("token") or {}; c=addr(t); places=int(t.get("decimals") or 0)
        q=dec(item.get("value","0"),places)
        holding_rows.append({"contract":c,"symbol":t.get("symbol") or c[:10],
            "name":t.get("name"),"decimals":places,"quantity":str(q),"amount_raw":str(item.get("value","0"))})
    def price_one(h):
        if h["contract"]==WSDA:
            h.update({"spot_price_sda":1.0,"spot_value_sda":float(Decimal(h["quantity"])),
                "buy_quote_error":None,"exit_quote_sda":None,"exit_quote_error":None}); return h
        pref=fee_by_token.get(h["contract"])
        bq=quote(WSDA,h["contract"],10**18,10**18,pref)
        if bq["amount_out_raw"]:
            out=dec(bq["amount_out_raw"],h["decimals"])
            price=Decimal(1)/out if out>0 else None
            h.update({"spot_price_sda":float(price) if price else None,
                "spot_value_sda":float(Decimal(h["quantity"])*price) if price else None,
                "buy_quote_fee":bq["fee"],"buy_quote_error":None})
        else:
            h.update({"spot_price_sda":None,"spot_value_sda":None,"buy_quote_fee":None,
                "buy_quote_error":bq["error"]})
        # Full-balance sell quote is used for open-position liquidation value.
        amount=int(h["amount_raw"])
        if amount>0:
            sq=quote(h["contract"],WSDA,amount,0,pref)
            h.update({"exit_quote_sda":float(dec(sq["amount_out_raw"],18)) if sq["amount_out_raw"] else None,
                "exit_quote_fee":sq["fee"],"exit_quote_error":sq["error"]})
        else:
            h.update({"exit_quote_sda":None,"exit_quote_fee":None,"exit_quote_error":"zero balance"})
        return h
    with ThreadPoolExecutor(max_workers=4) as pool:
        priced=list(pool.map(price_one,holding_rows))
    holdmap={h["contract"]:h for h in priced}
    open_positions=[]; residual_dust=[]; open_value=Decimal(0); open_cost=Decimal(0); open_complete=True
    for lot in open_lots:
        c=lot["contract"]; h=holdmap.get(c)
        qty=Decimal(lot["quantity"]); cost=Decimal(lot["total_cost_basis_sda"])
        if qty<=Decimal("0.001"):
            residual_dust.append(lot)
            continue
        if not h or Decimal(h["quantity"]) < qty-Decimal("0.00000001"):
            open_complete=False; current=None; err="wallet balance does not cover FIFO open lot"
        else:
            current=h.get("exit_quote_sda")
            if current is None: current=h.get("spot_value_sda")
            err=h.get("exit_quote_error") if h.get("exit_quote_sda") is None else None
            if current is None: open_complete=False
        pnl=Decimal(str(current))-cost if current is not None else None
        if current is not None: open_value+=Decimal(str(current))
        open_cost+=cost
        open_positions.append({**lot,"current_value_sda":current,"open_pnl_sda":str(pnl) if pnl is not None else None,
            "value_method":"full-balance PinetSwap sell eth_call" if h and h.get("exit_quote_sda") is not None else "1 SDA buy mark",
            "valuation_error":err})
    realized_gross=sum((Decimal(x["gross_pnl_sda"]) for x in closed),Decimal(0))
    realized_net=sum((Decimal(x["net_pnl_sda"]) for x in closed),Decimal(0))
    open_pnl=open_value-open_cost if open_complete else None
    total_pnl=realized_net+open_pnl if open_pnl is not None and not unmatched else None
    native_sda=Decimal(native)/Decimal(10**18)
    wsda_qty=Decimal(holdmap.get(WSDA,{}).get("quantity","0"))
    token_spot=sum((Decimal(str(h["spot_value_sda"])) for h in priced if h.get("spot_value_sda") is not None and h["contract"]!=WSDA),Decimal(0))
    unpriced=[{"token":h["symbol"],"contract":h["contract"],"quantity":h["quantity"],
        "buy_quote_error":h.get("buy_quote_error")} for h in priced if h["contract"]!=WSDA and h.get("spot_value_sda") is None]
    wallet_value_partial=native_sda+wsda_qty+token_spot
    wallet_value_complete=not unpriced

    # Native cash-flow reconciliation: external SDA in/out, router buy spends,
    # swap proceeds, residual WSDA, and gas paid by this wallet.
    wallet_fees=Decimal(0); native_in=Decimal(0); native_out=Decimal(0); router_buy_spend=Decimal(0)
    for h,d in details.items():
        fee=dec((d.get("fee") or {}).get("value","0"),18)
        if addr(d.get("from"))==WALLET: wallet_fees+=fee
        val=dec(d.get("value","0"),18); fr,to=addr(d.get("from")),addr(d.get("to"))
        if val>0 and to==WALLET and fr!=WALLET: native_in+=val
        elif val>0 and fr==WALLET and to==ROUTER: router_buy_spend+=val
        elif val>0 and fr==WALLET and to!=ROUTER: native_out+=val
    all_proceeds=sum((r["proceeds"] for r in records if r["action"]=="SELL"),Decimal(0))
    residual_wsda=wsda_qty
    expected=native_in-native_out-router_buy_spend+all_proceeds-residual_wsda-wallet_fees
    cash_delta=native_sda-expected

    # Compare, never substitute, the bot ledger with blockchain facts.
    ledger=None; ledger_error=None
    try:
        u="https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/real_trade_state.json"
        rr=requests.get(u,timeout=(10,30)); rr.raise_for_status(); ledger=rr.json()
    except Exception as e: ledger_error=str(e)
    ledger_trades=(ledger or {}).get("trades",[]) if isinstance(ledger,dict) else []
    ledger_positions=(ledger or {}).get("positions",{}) if isinstance(ledger,dict) else {}
    ledger_times=[str(x.get("at") or "") for x in ledger_trades if x.get("at")]
    ledger_start=min(ledger_times) if ledger_times else ""
    ledger_start_day=ledger_start[:10] if ledger_start else ""
    def after_ledger_start(r):
        return not ledger_start_day or str(r.get("timestamp") or "")[:10]>=ledger_start_day
    chain_buys={r["hash"] for r in records if r["action"]=="BUY"}
    chain_sells={r["hash"] for r in records if r["action"]=="SELL"}
    chain_buys_current={r["hash"] for r in records if r["action"]=="BUY" and after_ledger_start(r)}
    chain_sells_current={r["hash"] for r in records if r["action"]=="SELL" and after_ledger_start(r)}
    ledger_buys={str(x.get("tx") or "").lower() for x in ledger_trades if str(x.get("action","")).upper()=="BUY" and x.get("tx")}
    ledger_sells={str(x.get("tx") or "").lower() for x in ledger_trades if str(x.get("action","")).upper()=="SELL" and x.get("tx")}
    ledger_buys_current={str(x.get("tx") or "").lower() for x in ledger_trades if str(x.get("action","")).upper()=="BUY" and x.get("tx") and (not ledger_start or str(x.get("at") or "")>=ledger_start)}
    ledger_sells_current={str(x.get("tx") or "").lower() for x in ledger_trades if str(x.get("action","")).upper()=="SELL" and x.get("tx") and (not ledger_start or str(x.get("at") or "")>=ledger_start)}
    manual_rows=[x for x in ledger_trades if str(x.get("action","")).upper()=="SELL" and not x.get("tx")]
    manual_matches=[]; manual_unmatched=[]
    for m in manual_rows:
        candidates=[]
        for r in records:
            if r["action"]!="SELL" or r["contract"]!=str(m.get("token") or "").lower(): continue
            if not after_ledger_start(r): continue
            try:
                delta=abs((datetime.fromisoformat(str(r["timestamp"]).replace("Z","+00:00"))-
                    datetime.fromisoformat(str(m.get("at")).replace("Z","+00:00"))).total_seconds())
            except Exception: delta=999999
            amount_delta=abs(Decimal(str(m.get("amount_sda") or "0"))-r["proceeds"])
            if delta<=600 and amount_delta<=Decimal("0.001"):
                candidates.append((delta,amount_delta,r))
        if len(candidates)==1:
            delta,amount_delta,r=candidates[0]
            manual_matches.append({"ledger_at":m.get("at"),"token":m.get("symbol"),
                "matched_sell_tx":r["hash"],"time_delta_seconds":delta,
                "proceeds_delta_sda":str(amount_delta),"ledger_pnl_sda":m.get("pnl_sda"),
                "blockchain_gross_pnl_sda":next((x["gross_pnl_sda"] for x in closed if x["sell_tx"]==r["hash"]),None)})
        else: manual_unmatched.append(m)
    matched_manual_hashes={x["matched_sell_tx"] for x in manual_matches}
    active_open_contracts={x["contract"] for x in open_lots if Decimal(x["quantity"])>Decimal("0.001")}
    ledger_contracts={str(k).lower() for k in ledger_positions}
    historical_buys=sorted(r["hash"] for r in records if r["action"]=="BUY" and ledger_start and str(r["timestamp"])<ledger_start)
    historical_sells=sorted(r["hash"] for r in records if r["action"]=="SELL" and ledger_start and str(r["timestamp"])<ledger_start)
    discrepancies={
        "ledger_missing_buy_tx":sorted(chain_buys_current-ledger_buys_current),
        "ledger_buy_not_onchain":sorted(ledger_buys_current-chain_buys_current),
        "ledger_missing_sell_tx":sorted(chain_sells_current-ledger_sells_current-matched_manual_hashes),
        "ledger_sell_not_onchain":sorted(ledger_sells_current-chain_sells_current),
        "manual_sell_matches":manual_matches,
        "manual_sells_without_tx_unmatched":manual_unmatched,
        "historical_onchain_buys_before_ledger_start":historical_buys,
        "historical_onchain_sells_before_ledger_start":historical_sells,
        "ledger_positions_without_meaningful_open_lot":sorted(ledger_contracts-active_open_contracts),
        "blockchain_open_lots_missing_from_ledger":sorted(active_open_contracts-ledger_contracts),
        "ledger_start_timestamp":ledger_start or None,
        "ledger_fetch_error":ledger_error
    }
    # Current token balances include external/airdrop holdings. Their basis is
    # intentionally not invented; they are disclosed separately.
    known_contracts={x["contract"] for x in open_lots}
    external_assets=[h for h in priced if h["contract"] not in known_contracts and h["contract"]!=WSDA]
    for asset in external_assets:
        asset["transfer_in_txs"]=[{"hash":tr.get("transaction_hash"),"timestamp":tr.get("timestamp"),
            "from":addr(tr.get("from")),"quantity":str(dec(rawval(tr.get("total",tr.get("value"))),
                (tr.get("total") or {}).get("decimals") or (tr.get("token") or {}).get("decimals") or 0))}
            for tr in transfers if addr(tr.get("to"))==WALLET and addr(tr.get("token"))==asset["contract"]]
    # Flag plausible but non-atomic native-payment/token-transfer pairs; do not
    # silently treat them as confirmed swaps or assign their basis to FIFO.
    candidate_external_pairs=[]
    for h,d in details.items():
        if addr(d.get("from"))!=WALLET or addr(d.get("to")) in {ROUTER,WALLET}: continue
        paid=dec(d.get("value","0"),18)
        if paid<=0: continue
        try: pay_dt=datetime.fromisoformat(str(d.get("timestamp")).replace("Z","+00:00"))
        except Exception: continue
        recipient=addr(d.get("to"))
        for tr in transfers:
            if addr(tr.get("from"))!=recipient or addr(tr.get("to"))!=WALLET: continue
            token=tr.get("token") or {}
            if addr(token)==WSDA: continue
            try: recv_dt=datetime.fromisoformat(str(tr.get("timestamp")).replace("Z","+00:00"))
            except Exception: continue
            delta=(recv_dt-pay_dt).total_seconds()
            if 0<=delta<=120:
                candidate_external_pairs.append({"payment_tx":h,"payment_sda":str(paid),
                    "counterparty":recipient,"token_transfer_tx":tr.get("transaction_hash"),
                    "token":token.get("symbol"),"token_contract":addr(token),
                    "token_quantity":str(dec(rawval(tr.get("total",tr.get("value"))),
                        (tr.get("total") or {}).get("decimals") or token.get("decimals") or 0)),
                    "seconds_between":delta,"classification":"POSSIBLE OTC/EXTERNAL ACQUISITION — NOT A CONFIRMED SWAP"})
    external_unknown_basis_hashes=sorted({str(tr.get("transaction_hash") or "").lower()
        for tr in transfers if addr(tr.get("to"))==WALLET and addr(tr.get("token")) in {x["contract"] for x in external_assets}})
    tx_by_hash={str(x.get("hash") or "").lower():x for x in txs}
    def block_of(h): return tx_by_hash.get(h,{}).get("block_number")
    dates=[x.get("timestamp") for x in details.values() if x.get("timestamp")] or [x.get("timestamp") for x in txs if x.get("timestamp")]
    blocks=[x.get("block_number") for x in details.values() if x.get("block_number") is not None] or [x.get("block_number") for x in txs if x.get("block_number") is not None]
    failed=[x for x in classifications if not x["success"]]
    blockers=[]
    if unmatched: blockers.extend([x["sell_tx"] for x in unmatched])
    blockers.extend(external_unknown_basis_hashes)
    if errors: blockers.extend(errors.keys())
    if not open_complete: blockers.extend(x["buy_tx"] for x in open_positions if x["current_value_sda"] is None)
    if unpriced: blockers.extend(x["contract"] for x in unpriced)
    if abs(cash_delta)>Decimal("0.00000001"): blockers.append("NATIVE_CASH_RECONCILIATION_DELTA")
    if discrepancies["ledger_missing_sell_tx"]: blockers.extend(discrepancies["ledger_missing_sell_tx"])
    if discrepancies["ledger_positions_without_meaningful_open_lot"]: blockers.extend(discrepancies["ledger_positions_without_meaningful_open_lot"])
    blockers=sorted(set(blockers))
    net_values=[Decimal(x["net_pnl_sda"]) for x in closed]
    wins=[x for x in net_values if x>0]; losses=[x for x in net_values if x<0]
    stats={"wins":len(wins),"losses":len(losses),"win_rate_pct":float(Decimal(len(wins))*100/Decimal(len(net_values))) if net_values else None,
        "avg_win_sda":str(sum(wins,Decimal(0))/len(wins)) if wins else None,
        "avg_loss_sda":str(sum(losses,Decimal(0))/len(losses)) if losses else None,
        "best_trade":max(closed,key=lambda x:Decimal(x["net_pnl_sda"])) if closed else None,
        "worst_trade":min(closed,key=lambda x:Decimal(x["net_pnl_sda"])) if closed else None}
    report={"schema_version":2,"wallet":WALLET,"chain_id":CHAIN,"source":"Sidra Blockscout REST API v2 + read-only JSON-RPC eth_call",
        "raw_evidence_sha256":raw_sha,"retrieval":evidence["meta"],
        "blockchain_range":{"first_timestamp":min(dates) if dates else None,"latest_timestamp":max(dates) if dates else None,
            "first_block":min(blocks) if blocks else None,"latest_block":max(blocks) if blocks else None},
        "counts":{"normal_transactions":len(txs),"token_transfers":len(transfers),"internal_transactions":len(internal),
            "failed_transactions":len(failed),"confirmed_buys":len([r for r in records if r["action"]=="BUY"]),
            "confirmed_sells":len([r for r in records if r["action"]=="SELL"]),
            "approvals":sum(x["classification"]=="APPROVAL" for x in classifications),
            "unwraps":sum(x["classification"]=="UNWRAP" for x in classifications)},
        "classifications":classifications,"closed_trades":closed,"unmatched_sells":unmatched,
        "realized_pnl_gross_sda":str(realized_gross),"realized_pnl_net_sda":str(realized_net),
        "open_positions":open_positions,"residual_dust":residual_dust,"open_cost_basis_sda":str(open_cost),
        "open_market_value_sda":str(open_value) if open_complete else None,
        "open_pnl_sda":str(open_pnl) if open_pnl is not None else None,
        "total_pnl_sda":str(total_pnl) if total_pnl is not None else None,"statistics":stats,
        "external_flows":{"native_deposits_sda":str(native_in),"native_withdrawals_and_other_out_sda":str(native_out),
            "router_buy_spend_sda":str(router_buy_spend),"all_swap_proceeds_sda":str(all_proceeds),
            "wallet_paid_gas_sda":str(wallet_fees),"cash_reconciliation_expected_sda":str(expected),
            "native_balance_sda":str(native_sda),"cash_reconciliation_delta_sda":str(cash_delta)},
        "residual_wsda_sda":str(residual_wsda),"current_wallet_value_sda":str(wallet_value_partial),
        "current_wallet_value_complete":wallet_value_complete,"unpriced_assets":unpriced,
        "external_or_unknown_basis_assets":external_assets,"candidate_external_purchase_pairs":candidate_external_pairs,
        "external_unknown_basis_transfer_hashes":external_unknown_basis_hashes,
        "partial_confirmed_trading_pnl_sda":str(realized_net+open_pnl) if open_pnl is not None else None,
        "ledger_discrepancies":discrepancies,
        "reconciliation_status":"COMPLETE" if not blockers else "INCOMPLETE",
        "unresolved_hashes_or_items":blockers,
        "method":{"lot_matching":"FIFO","buy_cost":"confirmed native SDA value in successful router swap",
            "sell_proceeds":"WSDA Transfer logs from pool to router/wallet, WSDA valued 1:1 SDA",
            "gas":"wallet-paid transaction fees; entry gas prorated across FIFO quantities; sell gas, associated approval and linked unwrap gas allocated",
            "market_value":"read-only PinetSwap eth_call; 1 SDA buy mark for wallet value, full-balance sell quote for open positions",
            "external_assets":"not assigned invented acquisition basis; disclosed separately",
            "trading_side_effects":"none; eth_call and explorer GET only"},
        "validation":{"all_pagination_exhausted":True,"detail_fetch_errors":errors,
            "cash_reconciliation_zero":abs(cash_delta)<=Decimal("0.00000001"),
            "open_positions_valued":open_complete,"wallet_value_complete":wallet_value_complete,
            "unmatched_sell_count":len(unmatched),"ledger_loaded":ledger is not None}}
    # BOT accounting is narrower than wallet accounting: only successful,
    # wallet-funded router BUYs and FIFO-matched SELLs have verified bot basis.
    bot_buy_records=[]
    for tx in classifications:
        if tx.get("classification")!="BUY" or not tx.get("success"): continue
        received=next((d for d in tx.get("token_deltas",[]) if d.get("direction")=="IN"
                       and str(d.get("symbol") or "").upper()!="PINE"
                       and str(d.get("contract") or "").lower()!=WSDA),None)
        if not received: continue
        bot_buy_records.append({"tx":tx["hash"],"at":tx.get("timestamp"),
            "symbol":received.get("symbol") or "UNKNOWN","contract":str(received.get("contract") or "").lower(),
            "quantity":str(received.get("quantity") or "0"),"invested_sda":str(tx.get("native_value_sda") or "0")})
    bot_buy_contracts={x["contract"] for x in bot_buy_records}
    bot_closed_trades=[x for x in closed if str(x.get("contract") or "").lower() in bot_buy_contracts
                       and str(x.get("token") or "").upper()!="PINE"]
    bot_open_positions=[]
    for p in open_positions:
        contract=str(p.get("contract") or "").lower()
        quantity=Decimal(str(p.get("quantity") or "0"))
        if contract not in bot_buy_contracts or str(p.get("token") or "").upper()=="PINE" or quantity<=Decimal("0.001"):
            continue
        pnl=Decimal(str(p.get("open_pnl_sda") or "0"))
        value=Decimal(str(p.get("current_value_sda"))) if p.get("current_value_sda") is not None else None
        cost=Decimal(str(p.get("total_cost_basis_sda") or "0"))
        bot_open_positions.append({"token":contract,"symbol":p.get("token") or "UNKNOWN","quantity":str(quantity),
            "value_sda":str(value) if value is not None else None,"cost_basis_sda":str(cost),
            "open_pnl_sda":str(pnl),"pnl_pct":str(pnl/cost*100) if cost>0 else None,
            "buy_tx":p.get("buy_tx"),"at":p.get("timestamp")})
    bot_open_positions.sort(key=lambda x:x["symbol"])
    bot_closed_trades.sort(key=lambda x:(str(x.get("timestamp") or ""),str(x.get("sell_tx") or "")),reverse=True)
    bot_net_values=[Decimal(x["net_pnl_sda"]) for x in bot_closed_trades]
    bot_wins=[x for x in bot_net_values if x>0]
    bot_losses=[x for x in bot_net_values if x<0]
    bot_realized=sum(bot_net_values,Decimal(0))
    bot_open_pnl=sum((Decimal(x["open_pnl_sda"]) for x in bot_open_positions),Decimal(0))
    bot_total=bot_realized+bot_open_pnl
    bot_open_complete=all(x["value_sda"] is not None for x in bot_open_positions)
    bot_unmatched=[x for x in unmatched if str(x.get("contract") or "").lower() in bot_buy_contracts
                   and str(x.get("token") or "").upper()!="PINE"]
    bot_blockers=[]
    if errors: bot_blockers.extend(sorted(errors))
    if abs(cash_delta)>Decimal("0.00000001"): bot_blockers.append("NATIVE_CASH_RECONCILIATION_DELTA")
    bot_blockers.extend(x["sell_tx"] for x in bot_unmatched)
    if not bot_open_complete:
        bot_blockers.extend(x["buy_tx"] for x in bot_open_positions if x["value_sda"] is None)
    bot_blockers=sorted(set(bot_blockers))
    best=max(bot_closed_trades,key=lambda x:Decimal(x["net_pnl_sda"])) if bot_closed_trades else None
    worst=min(bot_closed_trades,key=lambda x:Decimal(x["net_pnl_sda"])) if bot_closed_trades else None
    bot_stats={"open_positions_count":len(bot_open_positions),"confirmed_buys_count":len(bot_buy_records),
        "matched_sells_count":len(bot_closed_trades),"wins":len(bot_wins),"losses":len(bot_losses),
        "win_rate_pct":str(Decimal(len(bot_wins))*100/Decimal(len(bot_net_values))) if bot_net_values else None,
        "average_trade_sda":str(sum(bot_net_values,Decimal(0))/len(bot_net_values)) if bot_net_values else None,
        "average_win_sda":str(sum(bot_wins,Decimal(0))/len(bot_wins)) if bot_wins else None,
        "average_loss_sda":str(sum(bot_losses,Decimal(0))/len(bot_losses)) if bot_losses else None,
        "best_trade":({"symbol":best.get("token"),"net_pnl_sda":best.get("net_pnl_sda"),
                       "sell_tx":best.get("sell_tx"),"timestamp":best.get("timestamp")} if best else None),
        "worst_trade":({"symbol":worst.get("token"),"net_pnl_sda":worst.get("net_pnl_sda"),
                        "sell_tx":worst.get("sell_tx"),"timestamp":worst.get("timestamp")} if worst else None)}
    excluded_external_sells=[x for x in unmatched if x not in bot_unmatched]
    bot_state={"schema_version":1,"generated_at":evidence["meta"]["retrieved_at"],
        "wallet":WALLET,"chain_id":CHAIN,"source":"blockchain_raw.json / confirmed router transactions / FIFO",
        "raw_evidence_sha256":raw_sha,"blockchain_range":report["blockchain_range"],
        "pagination":evidence["meta"]["pagination"],"pagination_exhausted":True,
        "bot_reconciliation_status":"COMPLETE" if not bot_blockers else "INCOMPLETE",
        "bot_unresolved_transactions":bot_blockers,
        "realized_pnl_sda":str(bot_realized),"open_pnl_sda":str(bot_open_pnl),"total_pnl_sda":str(bot_total),
        "open_positions":bot_open_positions,"buy_transactions":bot_buy_records,
        "sell_transactions":[{"tx":x.get("sell_tx"),"at":x.get("timestamp"),"symbol":x.get("token"),
            "contract":x.get("contract"),"quantity":x.get("quantity_sold"),"proceeds_sda":x.get("sell_proceeds_sda"),
            "cost_basis_sda":x.get("buy_cost_sda"),"gross_pnl_sda":x.get("gross_pnl_sda"),
            "gas_sda":x.get("gas_sda"),"net_pnl_sda":x.get("net_pnl_sda"),"buy_tx":x.get("buy_tx")}
            for x in bot_closed_trades],"statistics":bot_stats,
        "wallet_value_sda":str(wallet_value_partial),"wallet_value_complete":wallet_value_complete,
        "native_balance_sda":str(native_sda),
        "wallet_valuation":{"valued_assets_count":max(0,len(priced)-len(unpriced)),
            "total_assets_count":len(priced),"unpriced_assets":unpriced},
        "excluded_external_activity":{"unmatched_sells":excluded_external_sells,
            "unknown_basis_assets":[{"symbol":x.get("symbol"),"contract":x.get("contract")} for x in external_assets]},
        "method":{"lot_matching":"FIFO","bot_buy_rule":"successful wallet-funded router BUY with received token delta",
            "bot_sell_rule":"successful SELL fully matched to confirmed bot BUY FIFO basis",
            "dust_threshold_token_quantity":"0.001",
            "external_receipts":"excluded from BOT performance without verified router BUY cost basis",
            "wallet_value":"all priced wallet assets, including external receipts/airdrops"}}
    report["bot_reconciliation_status"]=bot_state["bot_reconciliation_status"]
    report["bot_unresolved_transactions"]=bot_blockers
    report["bot_realized_pnl_net_sda"]=bot_state["realized_pnl_sda"]
    report["bot_open_pnl_sda"]=bot_state["open_pnl_sda"]
    report["bot_total_pnl_sda"]=bot_state["total_pnl_sda"]
    report["bot_open_positions_count"]=bot_stats["open_positions_count"]
    dump("real_accounting_report.json",report)
    dump("closed_trades.json",closed)
    dump("ledger_comparison.json",{"discrepancies":discrepancies,"ledger_updated_at":(ledger or {}).get("updated_at") if ledger else None})
    Path("real_accounting_state.json").write_text(json.dumps(bot_state,indent=2,sort_keys=True,default=str)+"\\n",encoding="utf-8")
    print(json.dumps({"run_wallet":WALLET,"pages":evidence["meta"]["pagination"],"counts":report["counts"],
        "range":report["blockchain_range"],"realized_gross_sda":str(realized_gross),
        "realized_net_sda":str(realized_net),"open_cost_basis_sda":str(open_cost),
        "open_market_value_sda":report["open_market_value_sda"],"open_pnl_sda":report["open_pnl_sda"],
        "total_pnl_sda":report["total_pnl_sda"],"native_balance_sda":str(native_sda),
        "cash_reconciliation_delta_sda":str(cash_delta),"wallet_value_sda":str(wallet_value_partial),
        "wallet_value_complete":wallet_value_complete,"unmatched_sells":unmatched,
        "unpriced_assets":unpriced,"ledger_discrepancies":discrepancies,
        "reconciliation_status":report["reconciliation_status"],"unresolved":blockers},indent=2,default=str),flush=True)

if __name__=="__main__":
    main()
