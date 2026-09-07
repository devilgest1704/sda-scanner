import os,json,traceback
from datetime import datetime,timezone
import requests

MARKET='market_data.json'; WHALE='whale_data.json'; META='token_metadata.json'; LIQ='liquidity_data.json'; POS='positions.json'
INVESTMENT_SDA=50.0; BUY_THRESHOLD=78; MIN_1H_VOLUME_SDA=0.0; MIN_TRADES_1H=2
TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.04; FEE_RATE=.01; SLIPPAGE_RATE=.001; MAX_OPEN_POSITIONS=5
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

def whale_flow(address,whales,window='1h'):
    w=whales.get(address) or whales.get(address.lower()) or {}; f=(w.get('windows') or {}).get(window) or {}
    if not f and window=='1h':
        wb=n(w.get('whale_buy_volume')); ws=n(w.get('whale_sell_volume'))
        return {'buy_volume':wb,'sell_volume':ws,'buy_count':n(w.get('whale_buy_count')),'sell_count':n(w.get('whale_sell_count')),'net_flow':wb-ws}
    return f

def score_for(address,a,whales):
    m=a.get('momentum',{}) or {}; f=flow(a,'1h'); f15=flow(a,'15m')
    m15=n(m.get('15m_pct')); m1=n(m.get('1h_pct')); m4=n(m.get('4h_pct'))
    buy=n(f.get('buy_volume')); sell=n(f.get('sell_volume')); bc=n(f.get('buy_count')); sc=n(f.get('sell_count')); total=buy+sell; strength=buy/max(sell,1); ratio=bc/max(sc,1); trades=bc+sc
    w15=whale_flow(address,whales,'15m'); w1=whale_flow(address,whales,'1h'); w4=whale_flow(address,whales,'4h')
    wn=n(w1.get('net_flow')); wb=n(w1.get('buy_volume')); ws=n(w1.get('sell_volume')); wbc=n(w1.get('buy_count')); wsc=n(w1.get('sell_count')); w15n=n(w15.get('net_flow')); w4n=n(w4.get('net_flow'))
    s=0.0; s+=max(0,min(1,(m1+1)/9))*18; s+=max(0,min(1,(m4+3)/10))*8
    s+=max(0,min(1,(strength-.8)/1.7))*18; s+=max(0,min(1,(ratio-.8)/1.7))*7
    if wn>0:s+=max(0,min(1,wn/10000))*20
    elif wn<0:s+=max(-10,min(0,wn/10000))
    if wbc>=2:s+=3
    if wsc>=3 and wn<0:s-=3
    if w15n>0:s+=4
    elif w15n<0:s-=3
    if m1>=2 and m15>.2:s+=10
    elif m1>=1 and m15>=0:s+=6
    elif m1>=4 and m15<-.5:s-=5
    if n(f15.get('net_flow'))>0:s+=3
    elif n(f15.get('net_flow'))<0:s-=2
    va=a.get('volume_acceleration_15m_pct')
    if va is not None:
        if n(va)>15:s+=4
        elif n(va)<-20:s-=2
    s+=max(0,min(1,total/15000))*7
    if trades>=8:s+=5
    elif trades>=4:s+=3
    elif trades>=2:s+=1
    if trades<MIN_TRADES_1H:s-=8
    confidence=int(round(max(0,min(100,s))))
    if wb==0 and ws==0:confidence=min(confidence,72)
    return {'confidence':confidence,'strength':strength,'trade_ratio':ratio,'total_1h':total,'net_1h':n(f.get('net_flow')),'net_15m':n(f15.get('net_flow')),'whale_buy':wb,'whale_sell':ws,'whale_net':wn,'whale_buy_count':wbc,'whale_sell_count':wsc,'whale_15m_net':w15n,'whale_4h_net':w4n,'m15':m15,'m1h':m1,'m4h':m4,'volume_accel':None if va is None else n(va),'trades_1h':trades}

def liquidity_for(address,liquidity):return liquidity.get(address) or liquidity.get(address.lower()) or {}

def fmt(x):
    x=n(x)
    if abs(x)>=.01:return f'{x:.6f}'
    if abs(x)>=.0001:return f'{x:.8f}'
    return f'{x:.10f}'

def liq_text(liq):
    if not liq or liq.get('status')=='UNKNOWN' or liq.get('buy_50_sda') is None:
        return '💧 Liquidity: UNKNOWN\n📉 Est. 50 SDA impact: UNKNOWN'
    impact=liq.get('estimated_price_impact_pct')
    impact_txt=f'{n(impact):.2f}%' if impact is not None else 'UNKNOWN'
    return f"💧 50 SDA quote: {n(liq.get('buy_50_sda')):.8f} token\n📉 Est. 50 SDA impact: {impact_txt}"

def buy_exec(p):return p*(1+SLIPPAGE_RATE)
def sell_exec(p):return p*(1-SLIPPAGE_RATE)

def newpos(address,a,sc,meta):
    ref=n(a.get('price_in_sda')); entry=buy_exec(ref)
    return {'address':address,'label':label(address,meta),'opened_at':datetime.now(timezone.utc).isoformat(),'updated_at':datetime.now(timezone.utc).isoformat(),'status':'OPEN','entry_price':entry,'reference_price':ref,'tp1':entry*(1+TP1_PCT),'tp2':entry*(1+TP2_PCT),'sl':entry*(1-SL_PCT),'initial_sl':entry*(1-SL_PCT),'investment_sda':INVESTMENT_SDA,'remaining_fraction':1.0,'tp1_hit':False,'entry_confidence':sc['confidence'],'entry_metrics':sc}

def close(pd,address,current,reason):
    p=pd['positions'].get(address)
    if not p:return None
    frac=n(p.get('remaining_fraction'),1); entry=n(p['entry_price']); inv=n(p['investment_sda'])*frac; qty=inv/entry if entry>0 else 0; value=qty*sell_exec(current)*(1-FEE_RATE); cost=inv*(1+FEE_RATE); profit=value-cost; roi=profit/cost*100 if cost else 0
    c={**p,'status':'CLOSED','closed_at':datetime.now(timezone.utc).isoformat(),'close_price':current,'close_reason':reason,'closed_fraction':frac,'closed_value_sda':value,'closed_profit_sda':profit,'closed_roi_pct':roi}; pd['closed_trades'].append(c); del pd['positions'][address]; pd['closed_trades']=pd['closed_trades'][-500:]; return c

def close_msg(c):
    icon='💰' if c['close_reason']=='TP2' else ('🔒' if c['close_reason']=='BREAKEVEN' else '🛑')
    return f"{icon} {c['close_reason']} {c['label']}\n\nEntry: {fmt(c['entry_price'])} SDA\nCurrent: {fmt(c['close_price'])} SDA\nValue: {c['closed_value_sda']:.0f} SDA\nProfit: {c['closed_profit_sda']:+.0f} SDA\nROI: {c['closed_roi_pct']:+.2f}%\n\nMode: PAPER TRADING"

try:
    md=load(MARKET,{'tokens':{}}); tokens=md.get('tokens',{}) or {}; whales=load(WHALE,{}); meta=load(META,{}); ld=load(LIQ,{}); liquidity=ld.get('tokens',{}) or {}
    pd=load(POS,{'positions':{},'closed_trades':[]}); pd.setdefault('positions',{}); pd.setdefault('closed_trades',[]); events=[]
    for address in list(pd['positions']):
        td=tokens.get(address) or tokens.get(address.lower())
        if not td:continue
        a=td.get('analysis') or td; current=n(a.get('price_in_sda'))
        if current<=0:continue
        p=pd['positions'][address]; p['updated_at']=datetime.now(timezone.utc).isoformat()
        if not p.get('tp1_hit') and current>=n(p['tp1']):
            frac=min(.5,n(p.get('remaining_fraction'),1)); entry=n(p['entry_price']); inv=n(p['investment_sda'])*frac; qty=inv/entry if entry else 0; value=qty*sell_exec(current)*(1-FEE_RATE); cost=inv*(1+FEE_RATE); profit=value-cost; roi=profit/cost*100 if cost else 0; p['tp1_hit']=True; p['remaining_fraction']=n(p.get('remaining_fraction'),1)-frac; p['sl']=entry
            events.append(f"💰 TAKE PROFIT 1/2 {p['label']}\n\nEntry: {fmt(entry)} SDA\nCurrent: {fmt(current)} SDA\nClosed: 50%\nProfit: {profit:+.0f} SDA\nROI: {roi:+.2f}%\n\n🔒 Remaining 50% protected at breakeven")
        if address not in pd['positions']:continue
        p=pd['positions'][address]
        if current>=n(p['tp2']):
            c=close(pd,address,current,'TP2')
            if c:events.append(close_msg(c))
        elif current<=n(p['sl']):
            c=close(pd,address,current,'SL' if not p.get('tp1_hit') else 'BREAKEVEN')
            if c:events.append(close_msg(c))
    candidates=[]; ranking=[]
    for address,td in tokens.items():
        address=str(address).lower()
        if address in pd['positions']:continue
        a=td.get('analysis') or td
        if not a or n(a.get('price_in_sda'))<=0:continue
        f=flow(a,'1h'); sc=score_for(address,a,whales); trades=n(f.get('buy_count'))+n(f.get('sell_count')); sc['eligible_for_buy']=(trades>=MIN_TRADES_1H)
        if sc['confidence']>=BUY_THRESHOLD and sc['eligible_for_buy']:candidates.append((sc['confidence'],address,a,sc))
        ranking.append((sc['confidence'],address,a,sc,trades))
    candidates.sort(reverse=True); ranking.sort(key=lambda x:(x[0],x[4]),reverse=True)
    lines=['🔎 TOP BUY CANDIDATES','']
    for i,(conf,address,a,sc,trades) in enumerate(ranking[:5],1):
        status='🟢 BUY' if conf>=BUY_THRESHOLD and trades>=MIN_TRADES_1H else ('🟠 NEAR BUY' if conf>=75 else ('🟡 WATCH' if conf>=55 else '⚪ WEAK'))
        lines.append(f"{i}. {status} {label(address,meta)} — {conf}/100")
        lines.append(f"   1h {sc['m1h']:+.2f}% | flow {sc['net_1h']:+.0f} SDA | trades {int(trades)} | whale 1h {sc['whale_net']:+.0f}")
        lines.append('   '+liq_text(liquidity_for(address,liquidity)).replace('\n',' | '))
    send('\n'.join(lines))
    slots=max(0,MAX_OPEN_POSITIONS-len(pd['positions']))
    for _,address,a,sc in candidates[:slots]:
        p=newpos(address,a,sc,meta); p['liquidity_snapshot']=liquidity_for(address,liquidity); pd['positions'][address]=p
        events.append(f"🟢 BUY {p['label']}\n\nEntry: {fmt(p['entry_price'])} SDA\nTP1: {fmt(p['tp1'])} SDA (+5.0%)\nTP2: {fmt(p['tp2'])} SDA (+10.0%)\nSL: {fmt(p['sl'])} SDA (-4.0%)\n\nInvested: {INVESTMENT_SDA:.0f} SDA\nConfidence: {p['entry_confidence']}/100\n{liq_text(p['liquidity_snapshot'])}\n\nMode: PAPER TRADING")
    save(POS,pd); send(f"🤖 SDA PAPER TRADING\n\nOpen positions: {len(pd['positions'])}\nClosed trades: {len(pd['closed_trades'])}\nBUY candidates: {len(candidates)}\nEvents: {len(events)}")
    for e in events:send(e)
except Exception:
    err=traceback.format_exc(); print(err); send('❌ PAPER TRADING ENGINE ERROR\n\n'+err[:3500])
