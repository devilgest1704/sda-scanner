# SDA Scanner v11 decision engine
import main as engine

def n(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d

engine.BUY_THRESHOLD=70
engine.MIN_TRADES_1H=2
engine.MAX_NEW_BUYS_PER_RUN=1

def score_v11(addr,a,ws):
    m=a.get('momentum',{}) or {}; f1=engine.flow(a,'1h'); f15=engine.flow(a,'15m')
    m15=n(m.get('15m_pct')); m1=n(m.get('1h_pct')); m4=n(m.get('4h_pct'))
    bv=n(f1.get('buy_volume')); sv=n(f1.get('sell_volume')); bc=n(f1.get('buy_count')); sc=n(f1.get('sell_count'))
    tv=bv+sv; tt=bc+sc; net=bv-sv; ratio=bv/max(tv,1.0)
    w15=engine.whale(addr,ws,'15m'); w1=engine.whale(addr,ws,'1h')
    wn=n(w1.get('net_flow')); wn15=n(w15.get('net_flow')); wbc=n(w1.get('buy_count')); wsc=n(w1.get('sell_count'))
    wav=any(n(w1.get(k))!=0 for k in ('net_flow','buy_volume','sell_volume','buy_count','sell_count'))
    s=max(0,min(20,(m1+2)*2)); s+=max(0,min(10,(m4+3)*1.5))
    if tv>0:s+=max(0,min(22,22*((ratio-.35)/.65)))
    s+=10 if tt>=10 else (7 if tt>=6 else (4 if tt>=3 else (1 if tt>=2 else 0)))
    if m15>0 and m1>0:s+=min(12,6+m15)
    elif m15<-.5 and m1>3:s-=6
    n15=n(f15.get('net_flow'))
    if n15>0:s+=min(6,max(1,n15/100))
    elif n15<0:s-=min(5,max(1,abs(n15)/100))
    if wav:
        if wn>0:s+=min(14,wn/250)
        elif wn<0:s-=min(12,abs(wn)/250)
        if wbc>=2:s+=3
        if wsc>=3 and wn<0:s-=3
        if wn15>0:s+=3
        elif wn15<0:s-=3
    if m1>=12 and m15<=0:s-=8
    if m1>=18 and n15<=0:s-=5
    if tt<=2 and tv<50:s-=8
    return {'confidence':int(round(max(0,min(100,s)))),'m1h':m1,'m15':m15,'m4h':m4,'net_1h':net,'trades_1h':tt,'buy_ratio_1h':ratio,'whale_net':wn,'whale_15m_net':wn15,'whale_data_available':wav}

engine.score=score_v11

def recommendations_v11(portfolio,md,ws,meta,ld):
    out=[]; cur=portfolio.get('current',{}) if isinstance(portfolio,dict) else {}; tokens=md.get('tokens',{}) if isinstance(md,dict) else {}
    for token,pf in cur.items():
        an=(tokens.get(token,{}) or {}).get('analysis',tokens.get(token,{}) or {})
        s=score_v11(token,an,ws); pnl_raw=pf.get('unrealized_pnl_pct'); cost=n(pf.get('cost_sda'),0); pnl=n(pnl_raw,0); c=s['confidence']; m1=s['m1h']; f=s['net_1h']
        if pnl_raw is None or cost<=0: action='HOLD / NO COST BASIS'; reason='cost basis unavailable'
        elif c>=70 and m1>0 and f>0: action='HOLD / TRAIL'; reason='trend + positive SDA flow confirmed'
        elif pnl>=10 and m1>=0 and f>=0: action='HOLD / TRAIL'; reason='profit protected; momentum/flow not broken'
        elif pnl>=8 and (m1<0 or f<0): action='PARTIAL SELL'; reason='profit + weakening momentum/flow'
        elif c<35 and m1<0 and f<0: action='SELL / EXIT'; reason='trend and SDA flow both negative'
        elif pnl<=-15 and m1<0 and f<0: action='SELL / EXIT'; reason='deep loss with confirmed deterioration'
        elif pnl<=-8 and m1>=0 and f>=0: action='HOLD / WATCH'; reason='loss alone is not an exit; momentum/flow still positive'
        else: action='HOLD / WATCH'; reason='no confirmed exit condition'
        out.append({'token':token,'symbol':pf.get('symbol') or engine.lbl(token,meta),'action':action,'reason':reason,'pnl_pct':pnl,'pnl_sda':n(pf.get('unrealized_pnl_sda')),'score':c,'m1h':m1,'flow_1h':f})
    order={'SELL / EXIT':0,'PARTIAL SELL':1,'HOLD / TRAIL':2,'HOLD / WATCH':3,'HOLD / NO COST BASIS':4}
    return sorted(out,key=lambda x:(order.get(x['action'],9),x['score']))

engine.portfolio_recommendations=recommendations_v11
_original=engine.portfolio_message

def portfolio_message_v11(portfolio,recommendations):
    text=_original(portfolio,recommendations).replace('Realized P/L:','Historical matched P/L:')
    lines=text.splitlines(); out=[]
    for line in lines:
        out.append(line)
        if line.startswith('Historical matched P/L:'):out.append('  (informational only; not used for current SELL decisions)')
    out += ['', '🧠 V11 DECISION ENGINE','SDA accumulation mode: ON','Loss alone ≠ SELL; momentum + flow deterioration required.']
    return '\n'.join(out)
engine.portfolio_message=portfolio_message_v11

if __name__=='__main__':engine.main()
