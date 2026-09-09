# SDA Scanner V18 — autonomous paper trading engine
# Real wallet is READ ONLY. Paper positions are fully simulated.
import os, json, traceback
from datetime import datetime, timezone
import requests

MARKET_FILE="market_data.json"; WHALE_FILE="whale_data.json"; META_FILE="token_metadata.json"
LIQUIDITY_FILE="liquidity_data.json"; POSITIONS_FILE="positions.json"; WALLET_FILE="wallet_data.json"; PORTFOLIO_FILE="portfolio_data.json"
AUTO_STATE_FILE="paper_auto_state.json"
INVESTMENT_MIN_SDA=50.0; INVESTMENT_MAX_SDA=100.0; BUY_THRESHOLD=75
MIN_1H_VOLUME_SDA=0.0; MIN_TRADES_1H=3
TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.04; FEE_RATE=.01; SLIPPAGE_RATE=.001
MAX_OPEN_POSITIONS=5; MAX_NEW_BUYS_PER_RUN=1; MAX_PORTFOLIO_TXS=5000
WALLET_ADDRESS=os.environ.get("WATCH_WALLET","0x0a7415b28d0f3641fd202ced0c4d3a70619e6230").lower()
EXPLORER_API="https://ledger.sidrachain.com/api/v2"
PINET_SUPABASE_URL="https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
PINET_SUPABASE_HEADERS={"apikey":"sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z","accept-profile":"public"}
TELEGRAM_TOKEN=os.environ.get("TELEGRAM_TOKEN"); CHAT_ID=os.environ.get("CHAT_ID")

def load(p,d):
    try:
        with open(p,encoding="utf-8") as f:return json.load(f)
    except:return d

def save(p,d):
    t=p+".tmp"
    with open(t,"w",encoding="utf-8") as f:json.dump(d,f,indent=2,ensure_ascii=False)
    os.replace(t,p)

def num(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d

def now():return datetime.now(timezone.utc).isoformat()

def send(s):
    print(s)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":s},timeout=30)
        except Exception as e:print("Telegram error:",e)

def api_get(path,params=None):
    r=requests.get(EXPLORER_API+path,params=params,timeout=30);r.raise_for_status();return r.json()

def lbl(a,meta):
    x=meta.get(str(a).lower(),{}) if isinstance(meta,dict) else {}
    return f"{x.get('symbol')}/SDA" if x.get('symbol') else f"{str(a)[:10]}.../SDA"

def price(v):
    v=num(v);return f"{v:.6f}" if abs(v)>=.01 else (f"{v:.8f}" if abs(v)>=.0001 else f"{v:.10f}")

def flow(a,w):
    x=a.get("flow",{}).get(w,{}) if isinstance(a,dict) else {};return x if isinstance(x,dict) else {}

def whale(addr,ws,w):
    x=ws.get(str(addr).lower(),{}) if isinstance(ws,dict) else {}
    if not isinstance(x,dict):return {}
    x=x.get("windows",{}).get(w,{})
    return x if isinstance(x,dict) else {}

def score(addr,a,ws):
    m=a.get("momentum",{}) or {};f1=flow(a,"1h");f15=flow(a,"15m")
    m15=num(m.get("15m_pct"));m1=num(m.get("1h_pct"));m4=num(m.get("4h_pct"))
    bv=num(f1.get("buy_volume"));sv=num(f1.get("sell_volume"));bc=num(f1.get("buy_count"));sc=num(f1.get("sell_count"))
    tv=bv+sv;tt=bc+sc;strength=bv/max(sv,1);tratio=bc/max(sc,1)
    w15=whale(addr,ws,"15m");w1=whale(addr,ws,"1h");w4=whale(addr,ws,"4h")
    wn=num(w1.get("net_flow"));wb=num(w1.get("buy_volume"));wsell=num(w1.get("sell_volume"));wbc=num(w1.get("buy_count"));wsc=num(w1.get("sell_count"));wn15=num(w15.get("net_flow"));wn4=num(w4.get("net_flow"))
    s=max(0,min(1,(m1+1)/9))*18+max(0,min(1,(m4+3)/10))*8+max(0,min(1,(strength-.8)/1.7))*18+max(0,min(1,(tratio-.8)/1.7))*7
    avail=wb>0 or wsell>0 or wbc>0 or wsc>0
    if wn>0:s+=max(0,min(1,wn/10000))*20
    elif wn<0:s+=max(-10,min(0,wn/10000))
    if wbc>=2:s+=3
    if wsc>=3 and wn<0:s-=3
    if wn15>0:s+=4
    elif wn15<0:s-=3
    if m1>=2 and m15>.2:s+=10
    elif m1>=1 and m15>=0:s+=6
    elif m1>=4 and m15<-.5:s-=5
    n15=num(f15.get("net_flow"))
    if n15>0:s+=3
    elif n15<0:s-=2
    va=a.get("volume_acceleration_15m_pct")
    if va is not None:s+=4 if num(va)>15 else (-2 if num(va)<-20 else 0)
    s+=max(0,min(1,tv/15000))*7;s+=5 if tt>=8 else (3 if tt>=4 else (1 if tt>=2 else -8))
    c=int(round(max(0,min(100,s))))
    if not avail:c=min(c,72)
    return {"confidence":c,"m1h":m1,"m15":m15,"m4h":m4,"net_1h":num(f1.get("net_flow")),"trades_1h":tt,"whale_net":wn,"whale_15m_net":wn15,"whale_4h_net":wn4,"whale_data_available":avail}

def liquidity_for(a,ld):
    a=str(a).lower();q=(ld.get("quotes_50_sda") or {}).get(a)
    if isinstance(q,dict):return q
    q=(ld.get("tokens") or {}).get(a);return q if isinstance(q,dict) else {}

def liquidity_text(x):
    if not isinstance(x,dict):return "Liquidity: UNKNOWN"
    st=str(x.get("quote_status") or x.get("status") or "").upper()
    if x.get("amount_out_token_18dec") is not None and st in {"VALIDATED_READ_ONLY","AMOUNT_OUT_VALIDATED","READ_ONLY_QUOTE_AVAILABLE"}:return f"Liquidity quote: {num(x['amount_out_token_18dec']):.8f} token"
    if x.get("observed_sda_in") is not None and x.get("observed_token_out") is not None:return f"Liquidity observed: {num(x['observed_sda_in']):.2f} SDA → {num(x['observed_token_out']):.8f} token"
    return "Liquidity: UNKNOWN"

def _investment_for_score(c):
    c=num(c)
    if c>=90:return INVESTMENT_MAX_SDA
    if c>=82:return 75.0
    return INVESTMENT_MIN_SDA

def create(a,an,s,meta,liq,investment=None):
    ref=num(an.get("price_in_sda"));e=ref*(1+SLIPPAGE_RATE);inv=investment if investment is not None else _investment_for_score(s.get("confidence"))
    return {"address":a,"label":lbl(a,meta),"opened_at":now(),"updated_at":now(),"status":"OPEN","entry_price":e,"reference_price":ref,"tp1":e*(1+TP1_PCT),"tp2":e*(1+TP2_PCT),"sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),"investment_sda":float(max(INVESTMENT_MIN_SDA,min(INVESTMENT_MAX_SDA,inv))),"remaining_fraction":1.0,"tp1_hit":False,"entry_confidence":s["confidence"],"entry_metrics":s,"liquidity_snapshot":liq}

def close(p,a,current,reason,fraction=None):
    x=p["positions"].get(a)
    if not x:return None
    remaining=num(x.get("remaining_fraction",1));frac=remaining if fraction is None else min(remaining,max(0,num(fraction)))
    if frac<=0:return None
    e=num(x.get("entry_price"));inv=num(x.get("investment_sda"))*frac;qty=inv/e if e>0 else 0;ep=num(current)*(1-SLIPPAGE_RATE);value=qty*ep*(1-FEE_RATE);cost=inv*(1+FEE_RATE);profit=value-cost;roi=profit/cost*100 if cost else 0
    z={**x,"status":"CLOSED" if frac>=remaining-1e-9 else "PARTIAL CLOSED","closed_at":now(),"close_price":current,"close_reason":reason,"closed_fraction":frac,"closed_value_sda":value,"closed_profit_sda":profit,"closed_roi_pct":roi}
    if frac>=remaining-1e-9:
        p["closed_trades"].append(z);del p["positions"][a]
    else:
        x["remaining_fraction"]=remaining-frac;x["updated_at"]=now();p["closed_trades"].append(z)
    p["closed_trades"]=p["closed_trades"][-500:];return z

def _unwrap_items(obj):
    if isinstance(obj,list):return obj
    if not isinstance(obj,dict):return []
    for k in ("items","balances","result","data","tokens","token_balances"):
        v=obj.get(k)
        if isinstance(v,list):return v
        if isinstance(v,dict):
            x=_unwrap_items(v)
            if x:return x
    return []

def _token_address(obj):
    if isinstance(obj,str):return obj.lower() if obj.startswith("0x") else ""
    if not isinstance(obj,dict):return ""
    for k in ("address","address_hash","hash","token_address","tokenAddress","contract_address","contract"):
        v=obj.get(k)
        if isinstance(v,str) and v.startswith("0x"):return v.lower()
    return ""

def _amount_from_transfer(t):
    if not isinstance(t,dict):return 0.0
    tok=t.get("token") or {}
    try:d=int(tok.get("decimals",t.get("decimals",18)))
    except:d=18
    raw=t.get("total",t.get("value",t.get("amount",t.get("token_value",t.get("tokenValue",0)))))
    if isinstance(raw,dict):raw=raw.get("value",raw.get("raw",raw.get("amount")))
    try:return float(raw)/(10**d)
    except:return 0.0

def transfer_addr(x):
    if isinstance(x,str):return x.lower()
    if isinstance(x,dict):
        for k in ("hash","address","address_hash","value"):
            v=x.get(k)
            if isinstance(v,str) and v.startswith("0x"):return v.lower()
    return ""

def wallet_snapshot(md,meta):
    w={"wallet":WALLET_ADDRESS,"native_sda":None,"holdings":[],"total_token_value_sda":0.0,"total_value_sda":None,"updated_at":now(),"error":None,"holding_source":None}
    try:
        d=api_get(f"/addresses/{WALLET_ADDRESS}");raw=d.get("coin_balance",d.get("balance")) if isinstance(d,dict) else None
        if isinstance(raw,dict):raw=raw.get("value",raw.get("raw",raw.get("balance")))
        if raw is not None:w["native_sda"]=num(raw)/1e18
    except Exception as e:w["error"]=f"native balance: {e}"
    try:
        d=api_get(f"/addresses/{WALLET_ADDRESS}/token-balances");items=_unwrap_items(d)
        for b in items:
            if not isinstance(b,dict):continue
            t=b.get("token") if isinstance(b.get("token"),dict) else b;a=_token_address(t) or _token_address(b)
            if not a:continue
            sym=t.get("symbol") or b.get("symbol");raw=b.get("value",b.get("balance",b.get("token_balance")))
            try:dec=int(t.get("decimals",b.get("decimals",18)));amt=float(raw)/(10**dec)
            except:continue
            if amt<=0:continue
            an=(md.get("tokens",{}).get(a,{}) or {}).get("analysis",{}) if isinstance(md,dict) else {};px=num(an.get("price_in_sda"));val=amt*px if px>0 else None
            w["holdings"].append({"address":a,"symbol":sym or (meta.get(a,{}) or {}).get("symbol") or a[:10]+"...","amount":amt,"decimals":dec,"price_sda":px,"value_sda":val})
        if items:w["holding_source"]=f"/addresses/{WALLET_ADDRESS}/token-balances"
    except Exception as e:
        if not w["error"]:w["error"]=f"token balances: {e}"
    w["total_token_value_sda"]=sum(num(h.get("value_sda")) for h in w["holdings"] if h.get("value_sda") is not None)
    if w["native_sda"] is not None:w["total_value_sda"]=w["native_sda"]+w["total_token_value_sda"]
    save(WALLET_FILE,w);return w

def fetch_pinet_wallet_trades(wallet,max_rows=PINET_MAX_PORTFOLIO_ROWS if "PINET_MAX_PORTFOLIO_ROWS" in globals() else 5000):
    wallet=str(wallet).lower();out=[];offset=0
    while len(out)<max_rows:
        take=min(1000,max_rows-len(out));params={"select":"tx_hash,token_address,amount,volume_in_sda,price_in_sda,block_number,tx_timestamp,tx_type","or":f"(and(tx_type.eq.buy,to_address.eq.{wallet}),and(tx_type.eq.sell,from_address.eq.{wallet}))","order":"block_number.asc","offset":str(offset),"limit":str(take)}
        try:r=requests.get(PINET_SUPABASE_URL,params=params,headers=PINET_SUPABASE_HEADERS,timeout=30);r.raise_for_status();page=r.json()
        except Exception:break
        if not isinstance(page,list) or not page:break
        out.extend(page)
        if len(page)<take:break
        offset+=len(page)
    return out[:max_rows]

def portfolio_from_pinet(wallet,meta,max_rows=5000):
    rows=fetch_pinet_wallet_trades(wallet,max_rows)
    if not rows:return None
    buys=[];sells=[];seen=set()
    for x in rows:
        if not isinstance(x,dict):continue
        h=str(x.get("tx_hash") or "");token=str(x.get("token_address") or "").lower();side=str(x.get("tx_type") or "").lower();vol=num(x.get("volume_in_sda"));px=num(x.get("price_in_sda"));amt=num(x.get("amount"))
        if amt<=0 and vol>0 and px>0:amt=vol/px
        key=(h,token,side)
        if not h or not token or side not in {"buy","sell"} or amt<=0 or vol<=0 or key in seen:continue
        seen.add(key);base={"tx":h,"block_number":int(num(x.get("block_number"))),"timestamp":str(x.get("tx_timestamp") or ""),"token":token,"symbol":(meta.get(token,{}) or {}).get("symbol") or token[:10]+"...","amount":amt,"side":side.upper(),"price_sda":px}
        base["cost_sda" if side=="buy" else "proceeds_sda"]=vol
        (buys if side=="buy" else sells).append(base)
    trades=sorted(buys+sells,key=lambda z:(z.get("block_number",0),z.get("timestamp",""),z.get("tx","")));lots={};realized=0.0;history=[]
    for tr in trades:
        t=tr["token"]
        if tr["side"]=="BUY":lots.setdefault(t,[]).append({"amount":tr["amount"],"cost_sda":tr["cost_sda"]});history.append(tr);continue
        qty=tr["amount"];removed=0.0;q=lots.setdefault(t,[])
        while qty>1e-12 and q:
            lot=q[0];take=min(qty,lot["amount"]);unit=lot["cost_sda"]/lot["amount"] if lot["amount"] else 0;removed+=take*unit;lot["amount"]-=take;lot["cost_sda"]-=take*unit;qty-=take
            if lot["amount"]<=1e-12:q.pop(0)
        tr["cost_basis_sda"]=removed;tr["unmatched_amount"]=max(0,qty);realized+=num(tr.get("proceeds_sda"))-removed;history.append(tr)
    current={}
    for t,q in lots.items():
        amt=sum(x["amount"] for x in q);cost=sum(x["cost_sda"] for x in q)
        if amt>1e-12:current[t]={"amount":amt,"cost_sda":cost,"avg_cost_sda":cost/amt,"lots":q,"symbol":(meta.get(t,{}) or {}).get("symbol") or t[:10]+"...","price_sda":0.0,"value_sda":None,"unrealized_pnl_sda":None,"unrealized_pnl_pct":None,"cost_basis_status":"DETECTED"}
    return {"wallet":wallet,"router":"pinet-supabase-wallet-query","updated_at":now(),"buy_count":len(buys),"sell_count":len(sells),"trades":history[-500:],"current":current,"realized_pnl_sda":realized,"source":"PinetSwap token_transactions wallet query"}

def portfolio_history(wallet,md,meta,ld,previous=None,wallet_obj=None):
    p=portfolio_from_pinet(wallet,meta)
    if not p:p=previous if isinstance(previous,dict) else {"current":{},"realized_pnl_sda":0.0,"buy_count":0,"sell_count":0,"trades":[]}
    w=wallet_obj if isinstance(wallet_obj,dict) else wallet_snapshot(md,meta);hold={str(h.get("address","")).lower():h for h in w.get("holdings",[]) if h.get("address")}
    for t,h in hold.items():
        if t not in p.setdefault("current",{}):p["current"][t]={"amount":num(h.get("amount")),"cost_sda":None,"avg_cost_sda":None,"lots":[],"symbol":h.get("symbol"),"price_sda":num(h.get("price_sda")),"value_sda":h.get("value_sda"),"unrealized_pnl_sda":None,"unrealized_pnl_pct":None,"cost_basis_status":"UNKNOWN"}
        else:
            x=p["current"][t];x["amount"]=num(h.get("amount"),x.get("amount",0));x["symbol"]=h.get("symbol") or x.get("symbol");x["price_sda"]=num(h.get("price_sda"));x["value_sda"]=h.get("value_sda")
            if x.get("cost_sda") is not None and x.get("value_sda") is not None:x["unrealized_pnl_sda"]=num(x["value_sda"])-num(x["cost_sda"]);x["unrealized_pnl_pct"]=x["unrealized_pnl_sda"]/num(x["cost_sda"])*100 if num(x["cost_sda"]) else None
    known=[x for x in p.get("current",{}).values() if x.get("unrealized_pnl_sda") is not None];p["open_cost_sda"]=sum(num(x.get("cost_sda")) for x in known);p["open_value_sda"]=sum(num(x.get("value_sda")) for x in known);p["open_unrealized_pnl_sda"]=sum(num(x.get("unrealized_pnl_sda")) for x in known);p["known_total_pnl_sda"]=num(p.get("realized_pnl_sda"))+p["open_unrealized_pnl_sda"];p["matched_sell_count"]=sum(1 for x in p.get("trades",[]) if x.get("side")=="SELL" and num(x.get("unmatched_amount"))<=num(x.get("amount"))*0.01);p["excluded_unmatched_sell_count"]=sum(1 for x in p.get("trades",[]) if x.get("side")=="SELL" and num(x.get("unmatched_amount"))>num(x.get("amount"))*0.01)
    return p

def portfolio_recommendations(portfolio,md,ws,meta,ld):
    out=[];tokens=md.get("tokens",{}) if isinstance(md,dict) else {};state=load("decision_state_v15.json",{});new={}
    for token,pf in (portfolio.get("current",{}) or {}).items():
        an=(tokens.get(token,{}) or {}).get("analysis",tokens.get(token,{}) or {});s=score(token,an,ws) if an else {"confidence":0,"m1h":0,"net_1h":0};scorev=num(s.get("confidence"));m1=num(s.get("m1h"));flow1=num(s.get("net_1h"));pnl_raw=pf.get("unrealized_pnl_pct");pnl=num(pnl_raw);old=state.get(token,{}) if isinstance(state.get(token),dict) else {};neg=int(num(old.get("negative_count")));weak=int(num(old.get("profit_weakening_count")));loss=int(num(old.get("loss_weakening_count")))
        negative=scorev<35 and m1<0 and flow1<0;deep=pnl<=-15 and m1<0 and flow1<0;weakening=pnl>0 and scorev<40 and (m1<0 or flow1<0);loss_weak=pnl<=-8 and (scorev<20 or (pnl<=-12 and scorev<35)) and (m1<0 or flow1<0)
        neg=min(5,neg+1) if negative or deep else 0;weak=min(5,weak+1) if weakening else 0;loss=min(5,loss+1) if loss_weak else 0;new[token]={"negative_count":neg,"profit_weakening_count":weak,"loss_weakening_count":loss}
        if pf.get("cost_sda") is None or pnl_raw is None:action="HOLD / NO COST BASIS"
        elif weak>=2:action="PARTIAL SELL"
        elif neg>=3 or loss>=3:action="SELL / EXIT"
        elif scorev>=70 and m1>0 and flow1>0:action="HOLD / TRAIL"
        else:action="HOLD / WATCH"
        out.append({"token":token,"symbol":pf.get("symbol") or lbl(token,meta),"action":action,"pnl_pct":pnl_raw,"pnl_sda":pf.get("unrealized_pnl_sda"),"score":scorev,"m1h":m1,"flow_1h":flow1})
    save("decision_state_v15.json",new);order={"SELL / EXIT":0,"PARTIAL SELL":1,"HOLD / TRAIL":2,"HOLD / WATCH":3,"HOLD / NO COST BASIS":4};return sorted(out,key=lambda x:(order.get(x["action"],9),-x["score"]))

def portfolio_message(portfolio,recommendations):
    lines=["📈 REAL PORTFOLIO / P&L (READ-ONLY)",""];cur=portfolio.get("current",{}) if isinstance(portfolio,dict) else {}
    for t,pf in sorted(cur.items(),key=lambda x:x[1].get("symbol",x[0])):
        pnl=pf.get("unrealized_pnl_sda");pct=pf.get("unrealized_pnl_pct");lines += [f"• {pf.get('symbol') or t[:10]}",f"  Amount: {num(pf.get('amount')):.8f} | Cost: {num(pf.get('cost_sda')):.2f} SDA" if pf.get('cost_sda') is not None else "  Amount: %.8f | Cost: UNKNOWN"%num(pf.get('amount')),f"  Now: {num(pf.get('value_sda')):.2f} SDA | P/L: {num(pnl):+.2f} SDA ({num(pct):+.2f}%)" if pnl is not None else "  Now: UNKNOWN | P/L: UNKNOWN",""]
    lines += [f"Realized P/L: {num(portfolio.get('realized_pnl_sda')):+.2f} SDA",f"Detected trades: {int(num(portfolio.get('buy_count')))} BUY / {int(num(portfolio.get('sell_count')))} SELL","","🧭 POSITION RECOMMENDATIONS"]
    for r in recommendations or []:lines.append(f"• {r['action']} {r['symbol']} | P/L {num(r.get('pnl_pct')):+.2f}% | score {num(r.get('score')):.0f} | 1h {num(r.get('m1h')):+.2f}%")
    return "\n".join(lines)

def wallet_message(w):return "👛 REAL WALLET (READ-ONLY)\n\nSDA: %.4f\nToken positions: %d"%(num(w.get("native_sda")),len(w.get("holdings",[])))

def _load_auto():return load(AUTO_STATE_FILE,{})
def _save_auto(x):save(AUTO_STATE_FILE,x)

def _auto_exit(p,tokens,ws):
    state=_load_auto();events=[]
    for a in list(p.get("positions",{})):
        td=tokens.get(a,{}) or {};an=td.get("analysis",td) if isinstance(td,dict) else {};cur=num(an.get("price_in_sda"));pos=p["positions"].get(a)
        if not pos or cur<=0:continue
        s=score(a,an,ws);c=num(s.get("confidence"));m1=num(s.get("m1h"));f1=num(s.get("net_1h"));entry=num(pos.get("entry_price"));roi=(cur-entry)/entry*100 if entry else 0
        old=state.get(a,{}) if isinstance(state.get(a),dict) else {};neg=int(num(old.get("neg")));weak=int(num(old.get("weak")))
        negative=c<35 and m1<0 and f1<0;weakening=roi>0 and c<40 and (m1<0 or f1<0)
        neg=min(5,neg+1) if negative else 0;weak=min(5,weak+1) if weakening else 0;state[a]={"neg":neg,"weak":weak}
        if weak>=2 and not pos.get("tp1_hit"):
            z=close(p,a,cur,"AUTO PARTIAL SELL",.5)
            if z:events.append(f"🟠 AUTO PARTIAL SELL {z['label']}\n\nPrice: {price(cur)} SDA\nClosed: 50%\nProfit: {z['closed_profit_sda']:+.2f} SDA\nScore: {c:.0f}/100\nMode: PAPER AUTO")
        elif neg>=3:
            z=close(p,a,cur,"AUTO SELL / EXIT")
            if z:events.append(f"🔴 AUTO SELL / EXIT {z['label']}\n\nPrice: {price(cur)} SDA\nProfit: {z['closed_profit_sda']:+.2f} SDA\nScore: {c:.0f}/100\nMode: PAPER AUTO")
    _save_auto(state);return events

def main():
    md=load(MARKET_FILE,{"tokens":{}});tokens=md.get("tokens",{}) or {};ws=load(WHALE_FILE,{});meta=load(META_FILE,{});ld=load(LIQUIDITY_FILE,{})
    p=load(POSITIONS_FILE,{"positions":{},"closed_trades":[]});p=p if isinstance(p,dict) else {"positions":{},"closed_trades":[]};p.setdefault("positions",{});p.setdefault("closed_trades",[]);events=[]
    wallet=wallet_snapshot(md,meta);prev=load(PORTFOLIO_FILE,{})
    try:portfolio=portfolio_history(WALLET_ADDRESS,md,meta,ld,prev,wallet);recs=portfolio_recommendations(portfolio,md,ws,meta,ld);save(PORTFOLIO_FILE,portfolio)
    except Exception as e:portfolio=prev if isinstance(prev,dict) else {"current":{},"realized_pnl_sda":0.0};recs=[]
    events.extend(_auto_exit(p,tokens,ws))
    for a in list(p["positions"]):
        td=tokens.get(a,{}) or {};an=td.get("analysis",td);cur=num(an.get("price_in_sda"));pos=p["positions"].get(a)
        if not pos or cur<=0:continue
        e=num(pos.get("entry_price"));pos["updated_at"]=now()
        if not pos.get("tp1_hit") and cur>=num(pos.get("tp1")):
            inv=num(pos.get("investment_sda"))*.5;qty=inv/e if e else 0;value=qty*cur*(1-SLIPPAGE_RATE)*(1-FEE_RATE);cost=inv*(1+FEE_RATE);profit=value-cost;pos["tp1_hit"]=True;pos["remaining_fraction"]=.5;pos["sl"]=max(e*(1+FEE_RATE)/((1-SLIPPAGE_RATE)*(1-FEE_RATE)),e);events.append(f"💰 TAKE PROFIT 1/2 {pos['label']} | Profit: {profit:+.2f} SDA")
        if a not in p["positions"]:continue
        pos=p["positions"][a]
        if pos.get("tp1_hit"):
            breakeven=e*(1+FEE_RATE)/((1-SLIPPAGE_RATE)*(1-FEE_RATE));pos["sl"]=max(num(pos.get("sl")),breakeven,cur*(1-SL_PCT))
        if cur>=num(pos.get("tp2")):
            z=close(p,a,cur,"TP2");
            if z:events.append(f"💰 TP2 {z['label']} | Profit: {z['closed_profit_sda']:+.2f} SDA")
        elif cur<=num(pos.get("sl")):
            z=close(p,a,cur,"PROTECTED SL" if pos.get("tp1_hit") else "SL")
            if z:events.append(f"🛡️ {z['close_reason']} {z['label']} | Profit: {z['closed_profit_sda']:+.2f} SDA")
    ranks=[];cands=[]
    for raw,td in tokens.items():
        a=str(raw).lower()
        if a in p["positions"] or not isinstance(td,dict):continue
        an=td.get("analysis",td)
        if not isinstance(an,dict) or num(an.get("price_in_sda"))<=0:continue
        f=flow(an,"1h");tr=num(f.get("buy_count"))+num(f.get("sell_count"));s=score(a,an,ws);eligible=tr>=MIN_TRADES_1H;s["eligible_for_buy"]=eligible;ranks.append((s["confidence"],a,an,s,tr))
        if s["confidence"]>=BUY_THRESHOLD and eligible:cands.append((s["confidence"],a,an,s))
    ranks.sort(key=lambda x:(x[0],x[4]),reverse=True);cands.sort(reverse=True);lines=["🔎 TOP BUY CANDIDATES",""]
    for i,(c,a,an,s,tr) in enumerate(ranks[:5],1):lines += [f"{i}. {'🟢 BUY' if c>=BUY_THRESHOLD and tr>=MIN_TRADES_1H else ('🟡 WATCH' if c>=55 else '⚪ WEAK')} {lbl(a,meta)} — {c}/100",f"   1h {s['m1h']:+.2f}% | flow {s['net_1h']:+.0f} SDA | trades {int(tr)} | whale 1h {s['whale_net']:+.0f}",f"   {liquidity_text(liquidity_for(a,ld))}"]
    slots=max(0,MAX_OPEN_POSITIONS-len(p["positions"]))
    for _,a,an,s in cands[:min(slots,MAX_NEW_BUYS_PER_RUN)]:
        inv=_investment_for_score(s["confidence"]);pos=create(a,an,s,meta,liquidity_for(a,ld),inv);p["positions"][a]=pos;events.append(f"🟢 AUTO BUY {pos['label']}\n\nInvested: {inv:.0f} SDA\nEntry: {price(pos['entry_price'])} SDA\nTP1: {price(pos['tp1'])} SDA\nTP2: {price(pos['tp2'])} SDA\nSL: {price(pos['sl'])} SDA\nScore: {s['confidence']}/100\nMode: PAPER AUTO")
    opens=["📊 OPEN PAPER POSITIONS",""]
    if not p["positions"]:opens.append("No open paper positions.")
    else:
        for a,pos in p["positions"].items():
            cur=num((tokens.get(a,{}) or {}).get("analysis",{}).get("price_in_sda"));e=num(pos.get("entry_price"));roi=(cur-e)/e*100 if e and cur else 0;opens += [f"• {pos.get('label',a)}",f"  Entry: {price(e)} | Now: {price(cur)}",f"  ROI: {roi:+.2f}% | Invested: {num(pos.get('investment_sda')):.0f} SDA | Remaining: {num(pos.get('remaining_fraction',1))*100:.0f}%",f"  TP1: {price(pos.get('tp1'))} | TP2: {price(pos.get('tp2'))}",f"  SL: {price(pos.get('sl'))}",""]
    save(POSITIONS_FILE,p);send("\n".join(lines));send("\n".join(opens));send(wallet_message(wallet));send(portfolio_message(portfolio,recs));send(f"🤖 SDA PAPER TRADING V18\n\nOpen positions: {len(p['positions'])}\nClosed trades: {len(p['closed_trades'])}\nBUY candidates: {len(cands)}\nEvents: {len(events)}\n\nInvestment per BUY: 50-100 SDA\nFee: {FEE_RATE*100:.1f}%\nSlippage: {SLIPPAGE_RATE*100:.1f}%\nAuto scan: every 5 minutes")
    for e in events:send(e)

if __name__=="__main__":
    try:main()
    except Exception:
        e=traceback.format_exc();print(e)
        if TELEGRAM_TOKEN and CHAT_ID:
            try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":"PAPER TRADING ENGINE ERROR\n\n"+e[:3500]},timeout=30)
            except Exception:pass
        raise
