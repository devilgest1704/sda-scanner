import os,json,traceback
from datetime import datetime,timezone
from pathlib import Path
import requests

MARKET='market_data.json'; WHALE='whale_data.json'; META='token_metadata.json'; POS='positions.json'
INVESTMENT_SDA=10000.0; BUY_THRESHOLD=78; MIN_1H_VOLUME_SDA=1000.0
TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.04; FEE_RATE=.0025; SLIPPAGE_RATE=.001; MAX_OPEN_POSITIONS=5
TG=os.environ.get('TELEGRAM_TOKEN'); CHAT=os.environ.get('CHAT_ID')

def load(path,default):
    try:
        with open(path,encoding='utf-8') as f:return json.load(f)
    except Exception:return default

def save(path,obj):
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:json.dump(obj,f,indent=2,ensure_ascii=False)
    os.replace(tmp,path)

def n(v,d=0.):
    try:return float(v) if v is not None else d
    except:return d

def send(text):
    print(text)
    if not TG or not CHAT:return
    try:requests.post(f'https://api.telegram.org/bot{TG}/sendMessage',json={'chat_id':CHAT,'text':text},timeout=30)
    except Exception as e:print('Telegram:',e)

def label(a,meta):
    x=meta.get(a.lower(),{}) or meta.get(a,{})
    return f"{x.get('symbol')}/SDA" if x.get('symbol') else f'{a[:10]}.../SDA'

def flow(a,w):return (a.get('flow',{}).get(w,{}) or {})

def score_for(address,a,whales):
    m=a.get('momentum',{}) or {}; f=flow(a,'1h'); f15=flow(a,'15m')
    m15=n(m.get('15m_pct')); m1=n(m.get('1h_pct')); m4=n(m.get('4h_pct'))
    buy=n(f.get('buy_volume')); sell=n(f.get('sell_volume')); bc=n(f.get('buy_count')); sc=n(f.get('sell_count'))
    total=buy+sell; strength=buy/max(sell,1); ratio=bc/max(sc,1)
    whale=whales.get(address,{}) or whales.get(address.lower(),{})
    wb=n(whale.get('whale_buy_volume')); ws=n(whale.get('whale_sell_volume')); wn=wb-ws; wbc=n(whale.get('whale_buy_count'))
    s=0
    s+=max(0,min(1,(m1+2)/8))*20; s+=max(0,min(1,(m4+2)/14))*10
    s+=max(0,min(1,(strength-.8)/1.7))*18; s+=max(0,min(1,(ratio-.8)/1.7))*7
    s+=max(0,min(1,wn/50000))*25 if wn>0 else max(0,min(1,1+wn/50000))*5
    if wbc>=2:s+=5
    if m1>1 and -1<=m15<=1.5:s+=8
    elif m1>1.5 and m15>0:s+=5
    if n(f15.get('net_flow'))>0:s+=4
    va=a.get('volume_acceleration_15m_pct')
    if va is not None and n(va)>15:s+=3
    s+=max(0,min(1,total/15000))*10
    return {'confidence':int(round(max(0,min(100,s)))), 'strength':strength,'trade_ratio':ratio,'total_1h':total,'net_1h':n(f.get('net_flow')),'net_15m':n(f15.get('net_flow')),'whale_buy':wb,'whale_sell':ws,'whale_net':wn,'whale_buy_count':wbc,'m15':m15,'m1h':m1,'m4h':m4,'volume_accel':None if va is None else n(va)}

def fmt(x):
    x=n(x)
    if abs(x)>=1:return f'{x:.6f}'
    if abs(x)>=.01:return f'{x:.6f}'
    if abs(x)>=.0001:return f'{x:.8f}'
    return f'{x:.10f}'

def buy_exec(p):return p*(1+SLIPPAGE_RATE)
def sell_exec(p):return p*(1-SLIPPAGE_RATE)

def newpos(address,a,sc,meta):
    ref=n(a.get('price_in_sda')); entry=buy_exec(ref); lab=label(address,meta)
    return {'address':address,'label':lab,'opened_at':datetime.now(timezone.utc).isoformat(),'updated_at':datetime.now(timezone.utc).isoformat(),'status':'OPEN','entry_price':entry,'reference_price':ref,'tp1':entry*(1+TP1_PCT),'tp2':entry*(1+TP2_PCT),'sl':entry*(1-SL_PCT),'initial_sl':entry*(1-SL_PCT),'investment_sda':INVESTMENT_SDA,'remaining_fraction':1.0,'tp1_hit':False,'entry_confidence':sc['confidence'],'entry_metrics':sc}

def close(pd,address,current,reason):
    p=pd['positions'].get(address)
    if not p:return None
    frac=n(p.get('remaining_fraction'),1); entry=n(p['entry_price']); inv=n(p['investment_sda'])*frac
    qty=inv/entry if entry>0 else 0; value=qty*sell_exec(current)*(1-FEE_RATE); cost=inv*(1+FEE_RATE); profit=value-cost; roi=profit/cost*100 if cost else 0
    c={**p,'status':'CLOSED','closed_at':datetime.now(timezone.utc).isoformat(),'close_price':current,'close_reason':reason,'closed_fraction':frac,'closed_value_sda':value,'closed_profit_sda':profit,'closed_roi_pct':roi}
    pd['closed_trades'].append(c); del pd['positions'][address]; pd['closed_trades']=pd['closed_trades'][-500:]; return c

def close_msg(c):
    icon='💰' if c['close_reason']=='TP2' else ('🔒' if c['close_reason']=='BREAKEVEN' else '🛑')
    return f"{icon} {c['close_reason']} {c['label']}\n\nEntry: {fmt(c['entry_price'])} SDA\nCurrent: {fmt(c['close_price'])} SDA\nValue: {c['closed_value_sda']:.0f} SDA\nProfit: {c['closed_profit_sda']:+.0f} SDA\nROI: {c['closed_roi_pct']:+.2f}%\n\nMode: PAPER TRADING"

try:
    md=load(MARKET,{'tokens':{}}); tokens=md.get('tokens',{}) or {}; whales=load(WHALE,{}); meta=load(META,{})
    pd=load(POS,{'positions':{},'closed_trades':[]}); pd.setdefault('positions',{}); pd.setdefault('closed_trades',[])
    events=[]
    # Manage existing positions first.
    for address in list(pd['positions']):
        td=tokens.get(address) or tokens.get(address.lower())
        if not td:continue
        a=td.get('analysis') or td
        current=n(a.get('price_in_sda'))
        if current<=0:continue
        p=pd['positions'][address]; p['updated_at']=datetime.now(timezone.utc).isoformat()
        if not p.get('tp1_hit') and current>=n(p['tp1']):
            frac=min(.5,n(p.get('remaining_fraction'),1)); entry=n(p['entry_price']); inv=n(p['investment_sda'])*frac; qty=inv/entry if entry else 0; value=qty*sell_exec(current)*(1-FEE_RATE); cost=inv*(1+FEE_RATE); profit=value-cost; roi=profit/cost*100 if cost else 0
            p['tp1_hit']=True; p['remaining_fraction']=n(p.get('remaining_fraction'),1)-frac; p['sl']=entry
            events.append(f"💰 TAKE PROFIT 1/2 {p['label']}\n\nEntry: {fmt(entry)} SDA\nCurrent: {fmt(current)} SDA\nClosed: 50%\nProfit: {profit:+.0f} SDA\nROI: {roi:+.2f}%\n\n🔒 Remaining 50% protected at breakeven")
        if address not in pd['positions']:continue
        p=pd['positions'][address]
        if current>=n(p['tp2']):
            c=close(pd,address,current,'TP2')
            if c:events.append(close_msg(c))
        elif current<=n(p['sl']):
            reason='SL' if not p.get('tp1_hit') else 'BREAKEVEN'; c=close(pd,address,current,reason)
            if c:events.append(close_msg(c))
    # New candidates.
    candidates=[]
    for address,td in tokens.items():
        if address in pd['positions']:continue
        a=td.get('analysis') or td
        if not a or a.get('active') is False:continue
        price=n(a.get('price_in_sda'))
        f=flow(a,'1h'); vol=n(f.get('buy_volume'))+n(f.get('sell_volume'))
        if price<=0 or vol<MIN_1H_VOLUME_SDA:continue
        sc=score_for(address,a,whales)
        if sc['confidence']>=BUY_THRESHOLD:candidates.append((sc['confidence'],address,a,sc))
    candidates.sort(reverse=True)
    slots=max(0,MAX_OPEN_POSITIONS-len(pd['positions']))
    for _,address,a,sc in candidates[:slots]:
        p=newpos(address,a,sc,meta); pd['positions'][address]=p
        events.append(f"🟢 BUY {p['label']}\n\nEntry: {fmt(p['entry_price'])} SDA\nTP1: {fmt(p['tp1'])} SDA (+5.0%)\nTP2: {fmt(p['tp2'])} SDA (+10.0%)\nSL: {fmt(p['sl'])} SDA (-4.0%)\n\nInvested: {INVESTMENT_SDA:.0f} SDA\nConfidence: {p['entry_confidence']}/100\nMode: PAPER TRADING")
    save(POS,pd)
    send(f"🤖 SDA PAPER TRADING\n\nOpen positions: {len(pd['positions'])}\nClosed trades: {len(pd['closed_trades'])}\nBUY candidates: {len(candidates)}\nEvents: {len(events)}")
    for e in events:send(e)
except Exception:
    err=traceback.format_exc(); print(err); send('❌ PAPER TRADING ENGINE ERROR\n\n'+err[:3500])
