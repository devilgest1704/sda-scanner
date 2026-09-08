# SDA paper trading engine
# 50 SDA / 1% fee / 0.1% slippage / rolling whale data
import os,json,traceback
from datetime import datetime,timezone
import requests

MARKET_FILE="market_data.json"; WHALE_FILE="whale_data.json"; META_FILE="token_metadata.json"
LIQUIDITY_FILE="liquidity_data.json"; POSITIONS_FILE="positions.json"; WALLET_FILE="wallet_data.json"

INVESTMENT_SDA=50.0; BUY_THRESHOLD=78; MIN_1H_VOLUME_SDA=0.0; MIN_TRADES_1H=2
TP1_PCT=0.05; TP2_PCT=0.10; SL_PCT=0.04; FEE_RATE=0.01; SLIPPAGE_RATE=0.001; MAX_OPEN_POSITIONS=5
TELEGRAM_TOKEN=os.environ.get("TELEGRAM_TOKEN"); CHAT_ID=os.environ.get("CHAT_ID")
EXPLORER_API="https://ledger.sidrachain.com/api/v2"

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
def send(s):
    print(s)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":s},timeout=30)
        except Exception as e:print("Telegram error:",e)
def lbl(a,meta):
    x=meta.get(str(a).lower(),{}) if isinstance(meta,dict) else {}
    return f"{x.get('symbol')}/SDA" if x.get("symbol") else f"{str(a)[:10]}.../SDA"
def price(v):
    v=num(v)
    return f"{v:.6f}" if abs(v)>=.01 else (f"{v:.8f}" if abs(v)>=.0001 else f"{v:.10f}")
def flow(a,w):
    x=a.get("flow",{}).get(w,{}) if isinstance(a,dict) else {}
    return x if isinstance(x,dict) else {}
def whale(addr,ws,w):
    x=ws.get(str(addr).lower(),{}) if isinstance(ws,dict) else {}
    if not isinstance(x,dict):return {}
    x=x.get("windows",{}).get(w,{})
    return x if isinstance(x,dict) else {}
def score(addr,a,ws):
    m=a.get("momentum",{}) or {}; f1=flow(a,"1h"); f15=flow(a,"15m")
    m15=num(m.get("15m_pct")); m1=num(m.get("1h_pct")); m4=num(m.get("4h_pct"))
    bv=num(f1.get("buy_volume")); sv=num(f1.get("sell_volume")); bc=num(f1.get("buy_count")); sc=num(f1.get("sell_count"))
    tv=bv+sv; tt=bc+sc; strength=bv/max(sv,1); tratio=bc/max(sc,1)
    w15=whale(addr,ws,"15m"); w1=whale(addr,ws,"1h"); w4=whale(addr,ws,"4h")
    wn=num(w1.get("net_flow")); wb=num(w1.get("buy_volume")); wsell=num(w1.get("sell_volume"))
    wbc=num(w1.get("buy_count")); wsc=num(w1.get("sell_count")); wn15=num(w15.get("net_flow")); wn4=num(w4.get("net_flow"))
    s=0.0
    s+=max(0,min(1,(m1+1)/9))*18; s+=max(0,min(1,(m4+3)/10))*8
    s+=max(0,min(1,(strength-.8)/1.7))*18; s+=max(0,min(1,(tratio-.8)/1.7))*7
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
    if va is not None:
        va=num(va); s+=4 if va>15 else (-2 if va< -20 else 0)
    s+=max(0,min(1,tv/15000))*7
    s+=5 if tt>=8 else (3 if tt>=4 else (1 if tt>=2 else -8))
    c=int(round(max(0,min(100,s))))
    if not avail:c=min(c,72)
    return {"confidence":c,"m1h":m1,"m15":m15,"m4h":m4,"net_1h":num(f1.get("net_flow")),
            "trades_1h":tt,"whale_net":wn,"whale_15m_net":wn15,"whale_4h_net":wn4,
            "whale_data_available":avail}
def lq(addr,ld):
    x=ld.get(str(addr).lower(),{}) if isinstance(ld,dict) else {}
    return x if isinstance(x,dict) else {}
def lqtext(x):
    if x.get("quote_status") in {"VALIDATED_READ_ONLY","AMOUNT_OUT_VALIDATED","READ_ONLY_QUOTE_AVAILABLE"} and x.get("amount_out_token_18dec") is not None:
        return f"Liquidity quote: {num(x['amount_out_token_18dec']):.8f} token"
    return "Liquidity: UNKNOWN"
def create(a,an,s,meta):
    ref=num(an.get("price_in_sda")); e=ref*(1+SLIPPAGE_RATE)
    return {"address":a,"label":lbl(a,meta),"opened_at":datetime.now(timezone.utc).isoformat(),"updated_at":datetime.now(timezone.utc).isoformat(),
            "status":"OPEN","entry_price":e,"reference_price":ref,"tp1":e*(1+TP1_PCT),"tp2":e*(1+TP2_PCT),"sl":e*(1-SL_PCT),
            "initial_sl":e*(1-SL_PCT),"investment_sda":INVESTMENT_SDA,"remaining_fraction":1.0,"tp1_hit":False,
            "entry_confidence":s["confidence"],"entry_metrics":s}
def close(p,a,current,reason):
    x=p["positions"].get(a)
    if not x:return None
    frac=num(x.get("remaining_fraction",1)); e=num(x.get("entry_price")); inv=num(x.get("investment_sda"))*frac
    qty=inv/e if e>0 else 0; ep=num(current)*(1-SLIPPAGE_RATE); value=qty*ep*(1-FEE_RATE); cost=inv*(1+FEE_RATE)
    profit=value-cost; roi=profit/cost*100 if cost else 0
    z={**x,"status":"CLOSED","closed_at":datetime.now(timezone.utc).isoformat(),"close_price":current,"close_reason":reason,
       "closed_fraction":frac,"closed_value_sda":value,"closed_profit_sda":profit,"closed_roi_pct":roi}
    p["closed_trades"].append(z);del p["positions"][a];p["closed_trades"]=p["closed_trades"][-500:];return z
def closemsg(x):
    return f"💰 {x['close_reason']} {x['label']}\n\nEntry: {price(x['entry_price'])} SDA\nCurrent: {price(x['close_price'])} SDA\nValue: {x['closed_value_sda']:.2f} SDA\nProfit: {x['closed_profit_sda']:+.2f} SDA\nROI: {x['closed_roi_pct']:+.2f}%\n\nMode: PAPER TRADING"

def main():
    md=load(MARKET_FILE,{"tokens":{}}); tokens=md.get("tokens",{}) or {}; ws=load(WHALE_FILE,{}); meta=load(META_FILE,{})
    ld=load(LIQUIDITY_FILE,{}); lt=ld.get("tokens",{}) or {}
    for a,q in (ld.get("quotes_50_sda",{}) or {}).items():
        a=str(a).lower();lt.setdefault(a,{})
        if isinstance(q,dict):lt[a].update(q)
    p=load(POSITIONS_FILE,{"positions":{},"closed_trades":[]})
    if not isinstance(p,dict):p={"positions":{},"closed_trades":[]}
    p.setdefault("positions",{});p.setdefault("closed_trades",[]);events=[]
    for a in list(p["positions"]):
        a=str(a).lower(); td=tokens.get(a)
        if not td:continue
        an=td.get("analysis",td);cur=num(an.get("price_in_sda"))
        if cur<=0:continue
        pos=p["positions"][a];pos["updated_at"]=datetime.now(timezone.utc).isoformat()
        if not pos.get("tp1_hit") and cur>=num(pos.get("tp1")):
            frac=min(.5,num(pos.get("remaining_fraction",1)));e=num(pos.get("entry_price"));inv=num(pos.get("investment_sda"))*frac
            qty=inv/e if e>0 else 0;v=qty*cur*(1-SLIPPAGE_RATE)*(1-FEE_RATE);cost=inv*(1+FEE_RATE);profit=v-cost;roi=profit/cost*100 if cost else 0
            pos["tp1_hit"]=True;pos["remaining_fraction"]=num(pos.get("remaining_fraction",1))-frac;pos["sl"]=e
            events.append(f"💰 TAKE PROFIT 1/2 {pos['label']}\n\nEntry: {price(e)} SDA\nCurrent: {price(cur)} SDA\nClosed: 50%\nProfit: {profit:+.2f} SDA\nROI: {roi:+.2f}%\n\n🔒 Remaining 50% protected at breakeven")
        if a not in p["positions"]:continue
        pos=p["positions"][a]
        if cur>=num(pos.get("tp2")):
            z=close(p,a,cur,"TP2")
            if z:events.append(closemsg(z));continue
        if cur<=num(pos.get("sl")):
            z=close(p,a,cur,"BREAKEVEN" if pos.get("tp1_hit") else "SL")
            if z:events.append(closemsg(z))
    ranks=[];cands=[]
    for raw,td in tokens.items():
        a=str(raw).lower()
        if a in p["positions"] or not isinstance(td,dict):continue
        an=td.get("analysis",td)
        if not isinstance(an,dict) or num(an.get("price_in_sda"))<=0:continue
        f=flow(an,"1h");vol=num(f.get("buy_volume"))+num(f.get("sell_volume"));tr=num(f.get("buy_count"))+num(f.get("sell_count"))
        s=score(a,an,ws);eligible=vol>=MIN_1H_VOLUME_SDA and tr>=MIN_TRADES_1H;s["eligible_for_buy"]=eligible
        ranks.append((s["confidence"],a,an,s,tr))
        if s["confidence"]>=BUY_THRESHOLD and eligible:cands.append((s["confidence"],a,an,s))
    ranks.sort(key=lambda x:(x[0],x[4]),reverse=True);cands.sort(reverse=True)
    lines=["🔎 TOP BUY CANDIDATES",""]
    for i,(c,a,an,s,tr) in enumerate(ranks[:5],1):
        st="🟢 BUY" if c>=BUY_THRESHOLD and tr>=MIN_TRADES_1H else ("🟠 NEAR BUY" if c>=75 else ("🟡 WATCH" if c>=55 else "⚪ WEAK"))
        lines += [f"{i}. {st} {lbl(a,meta)} — {c}/100",
                  f"   1h {s['m1h']:+.2f}% | flow {s['net_1h']:+.0f} SDA | trades {int(tr)} | whale 1h {s['whale_net']:+.0f}",
                  f"   {lqtext(lq(a,lt))}"]
    slots=max(0,MAX_OPEN_POSITIONS-len(p["positions"]))
    for _,a,an,s in cands[:slots]:
        pos=create(a,an,s,meta);pos["liquidity_snapshot"]=lq(a,lt);p["positions"][a]=pos
        events.append(f"🟢 BUY {pos['label']}\n\nEntry: {price(pos['entry_price'])} SDA\nTP1: {price(pos['tp1'])} SDA (+5.0%)\nTP2: {price(pos['tp2'])} SDA (+10.0%)\nSL: {price(pos['sl'])} SDA (-4.0%)\n\nInvested: {INVESTMENT_SDA:.0f} SDA\nConfidence: {pos['entry_confidence']}/100\n{lqtext(pos['liquidity_snapshot'])}\n\nMode: PAPER TRADING")
    opens=["📊 OPEN PAPER POSITIONS",""]
    if not p["positions"]:opens.append("No open paper positions.")
    else:
        for a,pos in p["positions"].items():
            an=(tokens.get(a,{}) or {}).get("analysis",(tokens.get(a,{}) or {}));cur=num(an.get("price_in_sda"));e=num(pos.get("entry_price"));roi=(cur-e)/e*100 if e>0 and cur>0 else 0
            opens += [f"• {pos.get('label',a)}",f"  Entry: {price(e)} | Now: {price(cur)}",f"  ROI: {roi:+.2f}% | Remaining: {num(pos.get('remaining_fraction',1))*100:.0f}%",f"  TP1: {price(pos.get('tp1'))} | TP2: {price(pos.get('tp2'))}",f"  SL: {price(pos.get('sl'))}",""]
    save(POSITIONS_FILE,p);send("\n".join(lines));send("\n".join(opens))
    send(f"🤖 SDA PAPER TRADING\n\nOpen positions: {len(p['positions'])}\nClosed trades: {len(p['closed_trades'])}\nBUY candidates: {len(cands)}\nEvents: {len(events)}\n\nInvestment: {INVESTMENT_SDA:.0f} SDA\nFee: {FEE_RATE*100:.1f}%\nSlippage: {SLIPPAGE_RATE*100:.1f}%")
    for e in events:send(e)

if __name__=="__main__":
    try:main()
    except Exception:
        e=traceback.format_exc();print(e);send("PAPER TRADING ENGINE ERROR\n\n"+e[:3500]);raise