# SDA scanner / paper trading engine
# Real wallet is READ ONLY; paper positions remain separate.
import os,json,traceback
from datetime import datetime,timezone
import requests

MARKET_FILE="market_data.json"; WHALE_FILE="whale_data.json"; META_FILE="token_metadata.json"
LIQUIDITY_FILE="liquidity_data.json"; POSITIONS_FILE="positions.json"; WALLET_FILE="wallet_data.json"; PORTFOLIO_FILE="portfolio_data.json"
INVESTMENT_SDA=50.0; BUY_THRESHOLD=78; MIN_1H_VOLUME_SDA=0.0; MIN_TRADES_1H=2
TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.04; FEE_RATE=.01; SLIPPAGE_RATE=.001
MAX_OPEN_POSITIONS=5; MAX_NEW_BUYS_PER_RUN=1; MAX_PORTFOLIO_TXS=5000
WALLET_ADDRESS=os.environ.get("WATCH_WALLET","0x0a7415b28d0f3641fd202ced0c4d3a70619e6230").lower()
EXPLORER_API="https://ledger.sidrachain.com/api/v2"
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
def now(): return datetime.now(timezone.utc).isoformat()
def send(s):
    print(s)
    if TELEGRAM_TOKEN and CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":s},timeout=30)
        except Exception as e: print("Telegram error:",e)
def api_get(path, params=None):
    r=requests.get(EXPLORER_API+path, params=params, timeout=30)
    r.raise_for_status(); return r.json()
def lbl(a,meta):
    x=meta.get(str(a).lower(),{}) if isinstance(meta,dict) else {}
    return f"{x.get('symbol')}/SDA" if x.get("symbol") else f"{str(a)[:10]}.../SDA"
def price(v):
    v=num(v); return f"{v:.6f}" if abs(v)>=.01 else (f"{v:.8f}" if abs(v)>=.0001 else f"{v:.10f}")
def flow(a,w):
    x=a.get("flow",{}).get(w,{}) if isinstance(a,dict) else {}; return x if isinstance(x,dict) else {}
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
    s+=max(0,min(1,tv/15000))*7; s+=5 if tt>=8 else (3 if tt>=4 else (1 if tt>=2 else -8))
    c=int(round(max(0,min(100,s))))
    if not avail:c=min(c,72)
    return {"confidence":c,"m1h":m1,"m15":m15,"m4h":m4,"net_1h":num(f1.get("net_flow")),"trades_1h":tt,
            "whale_net":wn,"whale_15m_net":wn15,"whale_4h_net":wn4,"whale_data_available":avail}

def liquidity_for(a,ld):
    a=str(a).lower()
    q=(ld.get("quotes_50_sda") or {}).get(a)
    if isinstance(q,dict):return q
    q=(ld.get("tokens") or {}).get(a)
    return q if isinstance(q,dict) else {}
def liquidity_text(x):
    if not isinstance(x,dict):return "Liquidity: UNKNOWN"
    st=str(x.get("quote_status") or x.get("status") or "").upper()
    if x.get("amount_out_token_18dec") is not None and st in {"VALIDATED_READ_ONLY","AMOUNT_OUT_VALIDATED","READ_ONLY_QUOTE_AVAILABLE"}:
        return f"Liquidity quote: {num(x['amount_out_token_18dec']):.8f} token"
    if x.get("observed_sda_in") is not None and x.get("observed_token_out") is not None:
        return f"Liquidity observed: {num(x['observed_sda_in']):.2f} SDA → {num(x['observed_token_out']):.8f} token"
    return "Liquidity: UNKNOWN"

def create(a,an,s,meta,liq):
    ref=num(an.get("price_in_sda")); e=ref*(1+SLIPPAGE_RATE)
    return {"address":a,"label":lbl(a,meta),"opened_at":now(),"updated_at":now(),"status":"OPEN","entry_price":e,"reference_price":ref,
            "tp1":e*(1+TP1_PCT),"tp2":e*(1+TP2_PCT),"sl":e*(1-SL_PCT),"initial_sl":e*(1-SL_PCT),
            "investment_sda":INVESTMENT_SDA,"remaining_fraction":1.0,"tp1_hit":False,"entry_confidence":s["confidence"],
            "entry_metrics":s,"liquidity_snapshot":liq}

def close(p,a,current,reason):
    x=p["positions"].get(a)
    if not x:return None
    frac=num(x.get("remaining_fraction",1)); e=num(x.get("entry_price")); inv=num(x.get("investment_sda"))*frac
    qty=inv/e if e>0 else 0; ep=num(current)*(1-SLIPPAGE_RATE); value=qty*ep*(1-FEE_RATE); cost=inv*(1+FEE_RATE)
    profit=value-cost; roi=profit/cost*100 if cost else 0
    z={**x,"status":"CLOSED","closed_at":now(),"close_price":current,"close_reason":reason,"closed_fraction":frac,
       "closed_value_sda":value,"closed_profit_sda":profit,"closed_roi_pct":roi}
    p["closed_trades"].append(z); del p["positions"][a]; p["closed_trades"]=p["closed_trades"][-500:]; return z

def _append_holding(w, md, meta, address, amount, decimals=18, symbol=None):
    address=str(address or '').lower(); amount=num(amount)
    if not address or amount <= 0:return
    mdx=meta.get(address,{}) if isinstance(meta,dict) else {}; sym=mdx.get('symbol') or symbol or address[:10]+'...'
    td=(md.get('tokens',{}) or {}).get(address,{}) if isinstance(md,dict) else {}; an=td.get('analysis',td) if isinstance(td,dict) else {}
    px=num(an.get('price_in_sda')); val=amount*px if px>0 else None
    for h in w['holdings']:
        if h.get('address')==address:
            h['amount'] += amount; h['decimals']=int(decimals or h.get('decimals',18));
            if symbol and not h.get('symbol'):h['symbol']=symbol
            if val is not None:h['value_sda']=h['amount']*px
            return
    w['holdings'].append({'address':address,'symbol':sym,'amount':amount,'decimals':int(decimals or 18),'price_sda':px,'value_sda':val})

def _unwrap_items(obj):
    if isinstance(obj,list):return obj
    if not isinstance(obj,dict):return []
    for k in ('items','balances','result','data','tokens','token_balances'):
        v=obj.get(k)
        if isinstance(v,list):return v
        if isinstance(v,dict):
            x=_unwrap_items(v)
            if x:return x
    return []

def _token_address(obj):
    if isinstance(obj,str):return obj.lower() if obj.startswith('0x') else ''
    if not isinstance(obj,dict):return ''
    for k in ('address','address_hash','hash','token_address','tokenAddress','contract_address','contract'):
        v=obj.get(k)
        if isinstance(v,str) and v.startswith('0x'):return v.lower()
    return ''

def _token_info(b):
    t=b.get('token') if isinstance(b,dict) else {}; t=t if isinstance(t,dict) else {}
    a=_token_address(t) or _token_address(b); sym=t.get('symbol') or b.get('symbol'); dec=t.get('decimals',b.get('decimals',18))
    try:dec=int(dec)
    except:dec=18
    return a,sym,dec

def _raw_balance(b):
    candidates=[]
    if isinstance(b,dict):
        for k in ('value','balance','token_balance','tokenBalance','amount','raw_value','rawValue','total'):
            if b.get(k) not in (None,''):candidates.append(b.get(k))
        t=b.get('token')
        if isinstance(t,dict):
            for k in ('balance','value','token_balance'):
                if t.get(k) not in (None,''):candidates.append(t.get(k))
    for raw in candidates:
        if isinstance(raw,dict):raw=raw.get('value',raw.get('raw',raw.get('amount')))
        try:
            if isinstance(raw,str) and raw.lower().startswith('0x'):return int(raw,16)
            return float(raw)
        except:continue
    return None

def _amount_from_transfer(t):
    if not isinstance(t,dict):return 0.0
    tok=t.get('token') or {}
    try:dec=int(tok.get('decimals',t.get('decimals',18)))
    except:dec=18
    raw=t.get('total',t.get('value',t.get('amount',t.get('token_value',t.get('tokenValue',0)))))
    if isinstance(raw,dict):raw=raw.get('value',raw.get('raw',raw.get('amount')))
    try:return float(raw)/(10**dec)
    except:return 0.0

def token_transfers_for_address(address,limit=5000):
    out=[]; params={'items_count':min(100,limit)}
    try:
        while len(out)<limit:
            d=api_get(f'/addresses/{address}/token-transfers',params); items=d if isinstance(d,list) else d.get('items',[])
            if not isinstance(items,list) or not items:break
            out.extend(items); nxt=d.get('next_page_params') if isinstance(d,dict) else None
            if not isinstance(nxt,dict):break
            params=nxt
        return out[:limit]
    except:return out[:limit]

def wallet_snapshot(md,meta):
    w={'wallet':WALLET_ADDRESS,'native_sda':None,'holdings':[],'total_token_value_sda':0.0,'total_value_sda':None,'updated_at':now(),'error':None,'holding_source':None}
    try:
        d=api_get(f'/addresses/{WALLET_ADDRESS}'); raw=d.get('coin_balance',d.get('balance')) if isinstance(d,dict) else None
        if isinstance(raw,dict):raw=raw.get('value',raw.get('raw',raw.get('balance')))
        if raw is not None:
            if isinstance(raw,str) and raw.lower().startswith('0x'):raw=int(raw,16)
            w['native_sda']=num(raw)/1e18
    except Exception as e:w['error']=f'native balance: {e}'
    sources=[]
    for path,params in [(f'/addresses/{WALLET_ADDRESS}/token-balances',None),(f'/addresses/{WALLET_ADDRESS}/tokens',{'type':'ERC-20','items_count':100})]:
        try:
            d=api_get(path,params); items=_unwrap_items(d)
            if items:sources.append((path,items))
        except Exception as e:
            if not sources:w['error']=f'{w["error"]+" | " if w["error"] else ""}{path}: {e}'
    for path,items in sources:
        before=len(w['holdings'])
        for b in items:
            if not isinstance(b,dict):continue
            a,sym,dec=_token_info(b)
            if not a:continue
            raw=_raw_balance(b)
            if raw is None:continue
            try:amt=float(raw)/(10**dec)
            except:continue
            if amt>0:_append_holding(w,md,meta,a,amt,dec,sym)
        if len(w['holdings'])>before:w['holding_source']=path;break
    if not w['holdings']:
        try:
            transfers=token_transfers_for_address(WALLET_ADDRESS,5000); net={};info={};wallet=WALLET_ADDRESS.lower()
            for t in transfers:
                if not isinstance(t,dict):continue
                tok=t.get('token') or {}; a=_token_address(tok) or _token_address(t); amt=_amount_from_transfer(t)
                if not a or amt<=0:continue
                fr=transfer_addr(t.get('from'));to=transfer_addr(t.get('to'))
                if to==wallet:net[a]=net.get(a,0)+amt
                if fr==wallet:net[a]=net.get(a,0)-amt
                try:dec=int(tok.get('decimals',t.get('decimals',18)))
                except:dec=18
                info[a]=(dec,tok.get('symbol') or t.get('symbol'))
            for a,amt in net.items():
                if amt>1e-12:
                    dec,sym=info.get(a,(18,None));_append_holding(w,md,meta,a,amt,dec,sym)
            if w['holdings']:w['holding_source']='reconstructed from token-transfers'
        except Exception as e:w['error']=f"{w['error'] or ''} transfer fallback: {e}".strip()
    w['total_token_value_sda']=sum(num(h.get('value_sda')) for h in w['holdings'] if h.get('value_sda') is not None)
    if w['native_sda'] is not None:w['total_value_sda']=w['native_sda']+w['total_token_value_sda']
    save(WALLET_FILE,w);return w

def tx_hash(x):
    if not isinstance(x,dict):return ''
    h=x.get('hash') or x.get('transaction_hash') or x.get('tx_hash');return h if isinstance(h,str) else ''
def tx_timestamp(x):
    if not isinstance(x,dict):return ''
    for k in ('timestamp','block_timestamp','created_at'):
        v=x.get(k)
        if v:return str(v)
    return ''
def raw_input_tx(x):
    if not isinstance(x,dict):return ''
    for k in ('raw_input','input','calldata'):
        v=x.get(k)
        if isinstance(v,str):return v
        if isinstance(v,dict):
            for sk in ('value','data','raw_input','input'):
                sv=v.get(sk)
                if isinstance(sv,str):return sv
    return ''
def token_transfer_items(wallet,txh):
    try:
        d=api_get(f'/transactions/{txh}/token-transfers');items=d if isinstance(d,list) else d.get('items',[]);return items if isinstance(items,list) else []
    except:return []
def transfer_addr(x):
    if isinstance(x,str):return x.lower()
    if isinstance(x,dict):
        for k in ('hash','address','address_hash','value'):
            v=x.get(k)
            if isinstance(v,str) and v.startswith('0x'):return v.lower()
            if isinstance(v,dict):
                z=transfer_addr(v)
                if z:return z
    return ''
def transfer_amount(t):return _amount_from_transfer(t)
def transfer_token(t):
    if not isinstance(t,dict):return ''
    tok=t.get('token') or {};return _token_address(tok) or _token_address(t)
def transfer_from(t):return transfer_addr(t.get('from')) if isinstance(t,dict) else ''
def transfer_to(t):return transfer_addr(t.get('to')) if isinstance(t,dict) else ''
def internal_transactions_for_address(address,limit=5000):
    out=[];params={'items_count':min(100,limit)}
    try:
        while len(out)<limit:
            d=api_get(f'/addresses/{address}/internal-transactions',params);items=d if isinstance(d,list) else d.get('items',[])
            if not isinstance(items,list) or not items:break
            out.extend(items);nxt=d.get('next_page_params') if isinstance(d,dict) else None
            if not isinstance(nxt,dict):break
            params=nxt
        return out[:limit]
    except:return out[:limit]
def explorer_address_transactions(address,limit=MAX_PORTFOLIO_TXS):
    out=[];params={'items_count':min(100,limit)}
    try:
        while len(out)<limit:
            d=api_get(f'/addresses/{address}/transactions',params);items=d if isinstance(d,list) else d.get('items',[])
            if not isinstance(items,list) or not items:break
            out.extend(items);nxt=d.get('next_page_params') if isinstance(d,dict) else None
            if not isinstance(nxt,dict):break
            params=nxt
        return out[:limit]
    except:return out[:limit]
def gas_fee_sda(tx):
    if not isinstance(tx,dict):return 0.0
    raw=tx.get('fee');raw=raw.get('value',raw.get('raw')) if isinstance(raw,dict) else raw
    if raw is not None:
        try:return float(raw)/1e18
        except:pass
    gas=tx.get('gas_used',tx.get('gasUsed'));gp=tx.get('gas_price',tx.get('gasPrice'))
    try:return float(gas)*float(gp)/1e18
    except:return 0.0

PINET_SUPABASE_URL="https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
PINET_SUPABASE_HEADERS={"apikey":"sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z","accept-profile":"public"}
PINET_PAGE_SIZE=1000;PINET_MAX_PORTFOLIO_ROWS=5000

def fetch_pinet_wallet_trades(wallet,max_rows=PINET_MAX_PORTFOLIO_ROWS):
    wallet=str(wallet).lower()
    if not wallet:return []
    out=[];offset=0;page_size=min(PINET_PAGE_SIZE,max_rows)
    while len(out)<max_rows:
        take=min(page_size,max_rows-len(out));params={'select':'tx_hash,token_address,amount,volume_in_sda,price_in_sda,block_number,tx_timestamp,tx_type','or':f'(and(tx_type.eq.buy,to_address.eq.{wallet}),and(tx_type.eq.sell,from_address.eq.{wallet}))','order':'block_number.asc','offset':str(offset),'limit':str(take)}
        try:
            r=requests.get(PINET_SUPABASE_URL,params=params,headers=PINET_SUPABASE_HEADERS,timeout=30);r.raise_for_status();page=r.json()
        except:break
        if not isinstance(page,list) or not page:break
        out.extend(page)
        if len(page)<take:break
        offset+=len(page)
    return out[:max_rows]

def portfolio_from_pinet(wallet,meta,max_rows=PINET_MAX_PORTFOLIO_ROWS):
    rows=fetch_pinet_wallet_trades(wallet,max_rows)
    if not rows:return None
    buys=[];sells=[];seen=set();wallet=str(wallet).lower()
    for x in rows:
        if not isinstance(x,dict):continue
        h=str(x.get('tx_hash') or '');token=str(x.get('token_address') or '').lower();side=str(x.get('tx_type') or '').lower().strip();vol=num(x.get('volume_in_sda'));px=num(x.get('price_in_sda'));amount=num(x.get('amount'))
        if amount<=0 and vol>0 and px>0:amount=vol/px
        if not h or not token or side not in {'buy','sell'} or amount<=0 or vol<=0:continue
        key=(h.lower(),token,side)
        if key in seen:continue
        seen.add(key);sym=(meta.get(token,{}) or {}).get('symbol') if isinstance(meta,dict) else None
        base={'tx':h,'block_number':int(num(x.get('block_number'))) if num(x.get('block_number')) else 0,'timestamp':str(x.get('tx_timestamp') or ''),'token':token,'symbol':sym or token[:10]+'...','amount':amount,'gas_fee_sda':0.0,'side':side.upper(),'price_sda':px}
        if side=='buy':base['cost_sda']=vol;buys.append(base)
        else:base['proceeds_sda']=vol;sells.append(base)
    if not buys and not sells:return None
    trades=sorted(buys+sells,key=lambda x:(x.get('block_number',0),str(x.get('timestamp')),str(x.get('tx'))));lots={};realized={};history=[]
    for tr in trades:
        token=tr['token']
        if tr['side']=='BUY':
            lots.setdefault(token,[]).append({'amount':tr['amount'],'cost_sda':tr['cost_sda']});history.append(tr);continue
        qty=tr['amount'];removed=0.0;q=lots.setdefault(token,[])
        while qty>1e-12 and q:
            lot=q[0];take=min(qty,lot['amount']);unit=lot['cost_sda']/lot['amount'] if lot['amount'] else 0.0;removed+=take*unit;lot['amount']-=take;lot['cost_sda']-=take*unit;qty-=take
            if lot['amount']<=1e-12:q.pop(0)
        tr['cost_basis_sda']=removed;tr['matched_amount']=tr['amount']-qty;tr['unmatched_amount']=max(0.0,qty)
        # IMPORTANT: an unmatched SELL is not a profitable trade. It means the
        # retained BUY history is incomplete (or the token was acquired elsewhere).
        # Never add its whole proceeds to realized P/L.
        matched_proceeds=(tr.get('proceeds_sda',0.0)*(tr['matched_amount']/tr['amount'])) if tr['amount']>0 else 0.0
        tr['matched_proceeds_sda']=matched_proceeds
        if tr['matched_amount']>1e-12:
            realized[token]=realized.get(token,0.0)+(matched_proceeds-removed)
        history.append(tr)
    current={}
    for token,q in lots.items():
        amount=sum(z['amount'] for z in q);cost=sum(z['cost_sda'] for z in q)
        if amount<=1e-12:continue
        tb=[x for x in buys if x['token']==token]
        current[token]={'amount':amount,'cost_sda':cost,'avg_cost_sda':cost/amount if amount else 0.0,'lots':q,'first_buy_at':tb[0].get('timestamp','') if tb else '','last_buy_at':tb[-1].get('timestamp','') if tb else '','age_days':None,'symbol':tb[-1].get('symbol') if tb else token[:10]+'...'}
    return {'wallet':wallet,'router':'pinet-supabase-wallet-query','updated_at':now(),'buy_count':len(buys),'sell_count':len(sells),'trades':history[-500:],'current':current,'realized_pnl_sda':sum(realized.values()),'source':'PinetSwap token_transactions wallet query','source_rows':len(rows),'known_tx_hashes':[x['tx'] for x in history[-500:] if x.get('tx')]}

def portfolio_history(wallet,md,meta,ld,previous=None,wallet_obj=None):
    previous=previous if isinstance(previous,dict) else {}
    pinet=portfolio_from_pinet(wallet,meta)
    if pinet and pinet.get('buy_count',0)+pinet.get('sell_count',0)>0:
        wallet_data=wallet_obj if isinstance(wallet_obj,dict) else wallet_snapshot(md,meta)
        for token,cur in pinet.get('current',{}).items():
            h=next((z for z in wallet_data.get('holdings',[]) if str(z.get('address','')).lower()==token),None)
            if h:
                cur['symbol']=h.get('symbol') or cur.get('symbol');cur['price_sda']=num(h.get('price_sda'));cur['value_sda']=h.get('value_sda');cur['unrealized_pnl_sda']=num(cur.get('value_sda'))-num(cur.get('cost_sda')) if cur.get('value_sda') is not None else None;cur['unrealized_pnl_pct']=(cur['unrealized_pnl_sda']/num(cur.get('cost_sda'))*100) if num(cur.get('cost_sda')) else None;cur['cost_basis_status']='DETECTED'
        for h in wallet_data.get('holdings',[]):
            token=str(h.get('address','')).lower()
            if not token or token in pinet['current']:continue
            pinet['current'][token]={'amount':num(h.get('amount')),'cost_sda':None,'avg_cost_sda':None,'lots':[],'first_buy_at':'','last_buy_at':'','age_days':None,'symbol':h.get('symbol'),'price_sda':num(h.get('price_sda')),'value_sda':h.get('value_sda'),'unrealized_pnl_sda':None,'unrealized_pnl_pct':None,'cost_basis_status':'UNKNOWN'}
        return pinet
    return previous if isinstance(previous,dict) and previous.get('current') else {'wallet':wallet,'current':{},'realized_pnl_sda':0.0,'buy_count':0,'sell_count':0,'source':'No trusted wallet trade ledger available'}

def portfolio_recommendations(portfolio,md,ws,meta,ld):
    out=[];cur=portfolio.get('current',{}) if isinstance(portfolio,dict) else {};tokens=md.get('tokens',{}) if isinstance(md,dict) else {}
    for token,pf in cur.items():
        an=(tokens.get(token,{}) or {}).get('analysis',tokens.get(token,{}) or {});s=score(token,an,ws) if isinstance(an,dict) and an else {'confidence':0,'m1h':0,'net_1h':0,'trades_1h':0,'whale_net':0}
        pnl_raw=pf.get('unrealized_pnl_pct');cost=num(pf.get('cost_sda'),0.0);pnl=num(pnl_raw,0.0);c=s.get('confidence',0);m1=num(s.get('m1h'));flow1=num(s.get('net_1h'))
        if pnl_raw is None or cost<=0:action='HOLD / NO COST BASIS'
        elif c>=75 and m1>0 and flow1>=0:action='HOLD / TRAIL'
        elif pnl>=10 and (c<60 or m1<0 or flow1<0):action='PARTIAL SELL'
        elif c<45 and (m1<0 or flow1<0):action='SELL / EXIT'
        else:action='HOLD / WATCH'
        out.append({'token':token,'symbol':pf.get('symbol') or lbl(token,meta),'action':action,'pnl_pct':pnl,'pnl_sda':num(pf.get('unrealized_pnl_sda'),0.0),'score':c,'m1h':m1,'flow_1h':flow1})
    return sorted(out,key=lambda x:(x['action'] in {'SELL / EXIT','PARTIAL SELL'},-abs(x['pnl_pct'])),reverse=True)

def portfolio_message(portfolio,recommendations):
    lines=['📈 REAL PORTFOLIO / P&L (READ-ONLY)',''];cur=portfolio.get('current',{}) if isinstance(portfolio,dict) else {}
    if not cur:lines.append('No historical BUYs confidently detected yet.')
    else:
        for token,pf in sorted(cur.items(),key=lambda x:x[1].get('symbol',x[0])):
            pnl=num(pf.get('unrealized_pnl_sda'),0.0);pct=pf.get('unrealized_pnl_pct');cost=pf.get('cost_sda');age=pf.get('age_days');cost_text=f'{num(cost):.2f} SDA' if cost is not None else 'UNKNOWN';pnl_text=f'{pnl:+.2f} SDA ({num(pct):+.2f}%)' if pct is not None else 'UNKNOWN (cost basis not detected)';age_text=f'{num(age):.1f} days' if age is not None else 'UNKNOWN'
            lines += [f"• {pf.get('symbol') or token[:10]}",f"  Amount: {num(pf.get('amount')):.8f} | Cost: {cost_text}",f"  Now: {num(pf.get('value_sda')):.2f} SDA | P/L: {pnl_text}",f"  Avg cost: {num(pf.get('avg_cost_sda')):.8f} SDA/token | Age: {age_text}",""]
    lines += [f"Realized P/L: {num(portfolio.get('realized_pnl_sda')):+.2f} SDA",f"Detected trades: {int(portfolio.get('buy_count',0))} BUY / {int(portfolio.get('sell_count',0))} SELL",'','🧭 POSITION RECOMMENDATIONS']
    if not recommendations:lines.append('• No recommendation available.')
    else:
        for r in recommendations:
            icon='🔴' if r['action']=='SELL / EXIT' else ('🟠' if r['action']=='PARTIAL SELL' else ('🟢' if r['action']=='HOLD / TRAIL' else '🟡'));lines.append(f"• {icon} {r['symbol']}: {r['action']} | P/L {r['pnl_pct']:+.2f}% | score {r['score']}/100 | 1h {r['m1h']:+.2f}%")
    lines += ['','⚠️ Recommendations are informational only; wallet remains READ-ONLY.'];return '\n'.join(lines)

def wallet_message(w):
    lines=['👛 REAL WALLET (READ-ONLY)','','Address: '+str(w.get('wallet'))];lines.append(f"SDA: {num(w.get('native_sda')):.4f}" if w.get('native_sda') is not None else 'SDA: unavailable');hs=w.get('holdings') or [];lines += ['','📊 REAL POSITIONS']
    if hs:
        for h in sorted(hs,key=lambda x:x.get('symbol','')):
            v=h.get('value_sda');px=h.get('price_sda');lines.append(f"• {h.get('symbol') or h.get('address','')[:10]}");lines.append(f"  Amount: {num(h.get('amount')):.8f} | Price: {num(px):.8f} SDA" if px else f"  Amount: {num(h.get('amount')):.8f}");lines.append(f"  Value: {num(v):.2f} SDA" if v is not None else '  Value: UNKNOWN')
    else:lines.append('• No token balances detected')
    if w.get('holding_source'):lines.append(f"  Source: {w['holding_source']}")
    if w.get('total_value_sda') is not None:lines += ['','💰 TOTAL WALLET VALUE: '+f"{num(w['total_value_sda']):.2f} SDA"]
    if w.get('error'):lines += ['','⚠️ '+str(w['error'])[:900]]
    return '\n'.join(lines)

def main():
    md=load(MARKET_FILE,{'tokens':{}});tokens=md.get('tokens',{}) or {};ws=load(WHALE_FILE,{});meta=load(META_FILE,{});ld=load(LIQUIDITY_FILE,{});p=load(POSITIONS_FILE,{'positions':{},'closed_trades':[]})
    if not isinstance(p,dict):p={'positions':{},'closed_trades':[]}
    p.setdefault('positions',{});p.setdefault('closed_trades',[]);events=[];wallet=wallet_snapshot(md,meta);portfolio_prev=load(PORTFOLIO_FILE,{})
    try:
        portfolio=portfolio_history(WALLET_ADDRESS,md,meta,ld,portfolio_prev,wallet);portfolio_recs=portfolio_recommendations(portfolio,md,ws,meta,ld);save(PORTFOLIO_FILE,portfolio)
    except Exception as e:
        portfolio=portfolio_prev if isinstance(portfolio_prev,dict) else {'current':{},'realized_pnl_sda':0.0,'buy_count':0,'sell_count':0};portfolio_recs=[];portfolio['error']=f'portfolio history: {e}'
    for a in list(p['positions']):
        a=str(a).lower();td=tokens.get(a)
        if not td:continue
        an=td.get('analysis',td);cur=num(an.get('price_in_sda'))
        if cur<=0:continue
        pos=p['positions'][a];pos['updated_at']=now()
        if not pos.get('tp1_hit') and cur>=num(pos.get('tp1')):
            frac=min(.5,num(pos.get('remaining_fraction',1)));e=num(pos.get('entry_price'));inv=num(pos.get('investment_sda'))*frac;qty=inv/e if e>0 else 0;v=qty*cur*(1-SLIPPAGE_RATE)*(1-FEE_RATE);cost=inv*(1+FEE_RATE);profit=v-cost;roi=profit/cost*100 if cost else 0;pos['tp1_hit']=True;pos['remaining_fraction']=num(pos.get('remaining_fraction',1))-frac;pos['sl']=e;events.append(f"💰 TAKE PROFIT 1/2 {pos['label']}\n\nEntry: {price(e)} SDA\nCurrent: {price(cur)} SDA\nClosed: 50%\nProfit: {profit:+.2f} SDA\nROI: {roi:+.2f}%\n\n🔒 Remaining 50% protected at breakeven")
        if a not in p['positions']:continue
        pos=p['positions'][a]
        if cur>=num(pos.get('tp2')):
            z=close(p,a,cur,'TP2')
            if z:events.append(f"💰 TP2 {z['label']}\n\nEntry: {price(z['entry_price'])} SDA\nCurrent: {price(z['close_price'])} SDA\nProfit: {z['closed_profit_sda']:+.2f} SDA\nROI: {z['closed_roi_pct']:+.2f}%\n\nMode: PAPER TRADING")
        elif cur<=num(pos.get('sl')):
            reason='BREAKEVEN' if pos.get('tp1_hit') else 'SL';z=close(p,a,cur,reason)
            if z:events.append(f"💰 {reason} {z['label']}\n\nEntry: {price(z['entry_price'])} SDA\nCurrent: {price(z['close_price'])} SDA\nProfit: {z['closed_profit_sda']:+.2f} SDA\nROI: {z['closed_roi_pct']:+.2f}%\n\nMode: PAPER TRADING")
    ranks=[];cands=[]
    for raw,td in tokens.items():
        a=str(raw).lower()
        if a in p['positions'] or not isinstance(td,dict):continue
        an=td.get('analysis',td)
        if not isinstance(an,dict) or num(an.get('price_in_sda'))<=0:continue
        f=flow(an,'1h');vol=num(f.get('buy_volume'))+num(f.get('sell_volume'));tr=num(f.get('buy_count'))+num(f.get('sell_count'));s=score(a,an,ws);eligible=vol>=MIN_1H_VOLUME_SDA and tr>=MIN_TRADES_1H;s['eligible_for_buy']=eligible;ranks.append((s['confidence'],a,an,s,tr))
        if s['confidence']>=BUY_THRESHOLD and eligible:cands.append((s['confidence'],a,an,s))
    ranks.sort(key=lambda x:(x[0],x[4]),reverse=True);cands.sort(reverse=True);lines=['🔎 TOP BUY CANDIDATES','']
    for i,(c,a,an,s,tr) in enumerate(ranks[:5],1):
        st='🟢 BUY' if c>=BUY_THRESHOLD and tr>=MIN_TRADES_1H else ('🟠 NEAR BUY' if c>=75 else ('🟡 WATCH' if c>=55 else '⚪ WEAK'));lines += [f"{i}. {st} {lbl(a,meta)} — {c}/100",f"   1h {s['m1h']:+.2f}% | flow {s['net_1h']:+.0f} SDA | trades {int(tr)} | whale 1h {s['whale_net']:+.0f}",f"   {liquidity_text(liquidity_for(a,ld))}"]
    slots=max(0,MAX_OPEN_POSITIONS-len(p['positions']))
    for _,a,an,s in cands[:min(slots,MAX_NEW_BUYS_PER_RUN)]:
        liq=liquidity_for(a,ld);pos=create(a,an,s,meta,liq);p['positions'][a]=pos;events.append(f"🟢 BUY {pos['label']}\n\nEntry: {price(pos['entry_price'])} SDA\nTP1: {price(pos['tp1'])} SDA (+5.0%)\nTP2: {price(pos['tp2'])} SDA (+10.0%)\nSL: {price(pos['sl'])} SDA (-4.0%)\n\nInvested: {INVESTMENT_SDA:.0f} SDA\nConfidence: {pos['entry_confidence']}/100\n{liquidity_text(liq)}\n\nMode: PAPER TRADING")
    opens=['📊 OPEN PAPER POSITIONS','']
    if not p['positions']:opens.append('No open paper positions.')
    else:
        for a,pos in p['positions'].items():
            an=(tokens.get(a,{}) or {}).get('analysis',(tokens.get(a,{}) or {}));cur=num(an.get('price_in_sda'));e=num(pos.get('entry_price'));roi=(cur-e)/e*100 if e>0 and cur>0 else 0;opens += [f"• {pos.get('label',a)}",f"  Entry: {price(e)} | Now: {price(cur)}",f"  ROI: {roi:+.2f}% | Remaining: {num(pos.get('remaining_fraction',1))*100:.0f}%",f"  TP1: {price(pos.get('tp1'))} | TP2: {price(pos.get('tp2'))}",f"  SL: {price(pos.get('sl'))}",""]
    save(POSITIONS_FILE,p);send('\n'.join(lines));send('\n'.join(opens));send(wallet_message(wallet));send(portfolio_message(portfolio,portfolio_recs));send(f"🤖 SDA PAPER TRADING\n\nOpen positions: {len(p['positions'])}\nClosed trades: {len(p['closed_trades'])}\nBUY candidates: {len(cands)}\nEvents: {len(events)}\n\nInvestment: {INVESTMENT_SDA:.0f} SDA\nFee: {FEE_RATE*100:.1f}%\nSlippage: {SLIPPAGE_RATE*100:.1f}%")
    for e in events:send(e)
if __name__=='__main__':
    try:main()
    except Exception:
        e=traceback.format_exc();print(e)
        if TELEGRAM_TOKEN and CHAT_ID:
            try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json={'chat_id':CHAT_ID,'text':'PAPER TRADING ENGINE ERROR\n\n'+e[:3500]},timeout=30)
            except Exception:pass
        raise
