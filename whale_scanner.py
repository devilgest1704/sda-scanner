# PinetSwap whale adapter + rolling whale aggregation.
# Read-only. Uses PinetSwap /whales as primary source and Supabase as fallback.
import os, json, re, traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import requests

TOKEN=os.environ.get('TELEGRAM_TOKEN'); CHAT_ID=os.environ.get('CHAT_ID')
PINET_PAGE=os.environ.get('PINETSWAP_WHALES_URL','https://pinetswap.app/whales')
PINET_API=os.environ.get('PINETSWAP_WHALES_API','').strip()
SUPABASE_URL='https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions'
SUPABASE_HEADERS={'apikey':'sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z','accept-profile':'public'}
HISTORY_FILE='whale_history.json'; DATA_FILE='whale_data.json'; STATE_FILE='whale_state.json'
HOURS=6; WINDOWS={'15m':15,'30m':30,'1h':60,'4h':240}; MIN_WHALE_SDA=float(os.environ.get('WHALE_THRESHOLD_SDA','5000'))
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36'


def load(p,d):
    try:
        with open(p,encoding='utf-8') as f:return json.load(f)
    except:return d

def save(p,d):
    t=p+'.tmp'
    with open(t,'w',encoding='utf-8') as f:json.dump(d,f,indent=2,ensure_ascii=False)
    os.replace(t,p)

def num(v,d=0.0):
    try:return d if v is None else float(v)
    except:return d

def ts(v):
    try:
        if isinstance(v,(int,float)):return datetime.fromtimestamp(float(v),tz=timezone.utc)
        s=str(v).strip().replace('Z','+00:00')
        d=datetime.fromisoformat(s)
        return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).astimezone(timezone.utc)
    except:return None

def lower(v): return str(v or '').lower()

def extract_rows(obj):
    if isinstance(obj,list): return obj
    if isinstance(obj,dict):
        for k in ('items','data','results','rows','whales','transactions','alerts'):
            if isinstance(obj.get(k),list):return obj[k]
        # Some APIs wrap data one level deeper.
        for v in obj.values():
            if isinstance(v,list):return v
    return []

def first(d, keys):
    for k in keys:
        v=d.get(k) if isinstance(d,dict) else None
        if v is not None and v!='':return v
    return None

def _nested_get(obj, keys):
    if not isinstance(obj,dict): return None
    for k in keys:
        if k in obj and obj[k] not in (None,''): return obj[k]
    for k in ('trade','transaction','token','whale','data','attributes','stats','volume','amount'):
        v=obj.get(k)
        if isinstance(v,dict):
            z=_nested_get(v,keys)
            if z not in (None,''): return z
    return None

def _find_number_by_key(obj, wanted):
    if isinstance(obj,dict):
        for k,v in obj.items():
            lk=str(k).lower().replace('-','_')
            if any(w in lk for w in wanted):
                if isinstance(v,(int,float)): return float(v)
                if isinstance(v,str):
                    x=v.replace(',','').replace(' SDA','').replace('$','').strip()
                    try:return float(x)
                    except:pass
                if isinstance(v,dict):
                    z=_find_number_by_key(v,wanted)
                    if z is not None:return z
            if isinstance(v,dict):
                z=_find_number_by_key(v,wanted)
                if z is not None:return z
    elif isinstance(obj,list):
        for v in obj:
            z=_find_number_by_key(v,wanted)
            if z is not None:return z
    return None

def normalize_row(r):
    if not isinstance(r,dict):return None
    token=_nested_get(r,['token_address','tokenAddress','contract_address','contract','token'])
    if isinstance(token,dict): token=_nested_get(token,['address','address_hash','contract_address','token_address'])
    token=lower(token)
    symbol=_nested_get(r,['symbol','token_symbol','tokenSymbol','ticker','token_name'])
    if isinstance(symbol,dict):symbol=_nested_get(symbol,['symbol','ticker','name'])

    side=lower(_nested_get(r,['tx_type','transaction_type','type','side','direction','action','trade_type','event_type']))
    if side in {'in','buy','bought','purchase','purchase_buy','swap_in','buying','accumulation','accumulate'}: side='buy'
    elif side in {'out','sell','sold','sale','swap_out','selling','distribution','distribute'}: side='sell'
    else:
        # PinetSwap may expose booleans rather than a textual side.
        if r.get('is_buy') is True or r.get('isBuy') is True: side='buy'
        elif r.get('is_sell') is True or r.get('isSell') is True: side='sell'

    volume=_nested_get(r,['volume_in_sda','volume_sda','sda_volume','sda_amount','sdaAmount','value_in_sda','value_sda','amount_in_sda','amount_sda','trade_value_sda','tradeValueSda','buy_volume_sda','sell_volume_sda'])
    if volume is None:
        volume=_find_number_by_key(r,['sda','volume_sda','value_sda','trade_value','tradevalue'])
    if isinstance(volume,dict): volume=_nested_get(volume,['sda','value','amount','raw'])
    volume=num(volume)

    stamp=_nested_get(r,['tx_timestamp','timestamp','created_at','updated_at','time','date','event_time','block_timestamp','blockTimestamp'])
    dt=ts(stamp)
    h=_nested_get(r,['tx_hash','transaction_hash','hash','tx','transaction'])
    if isinstance(h,dict):h=_nested_get(h,['hash','tx_hash'])
    wallet=_nested_get(r,['wallet','wallet_address','trader','trader_address','address_from','sender','owner','from'])
    if isinstance(wallet,dict):wallet=_nested_get(wallet,['address','hash','address_hash'])
    score=_nested_get(r,['score','whale_score','whaleScore'])

    # Some PinetSwap responses put token/side/value inside a trade object.
    if not token:
        token=_nested_get(r.get('trade') if isinstance(r.get('trade'),dict) else {},['token_address','contract_address','token'])
        if isinstance(token,dict): token=_nested_get(token,['address','address_hash'])
        token=lower(token)
    if not side:
        raw=lower(_nested_get(r.get('trade') if isinstance(r.get('trade'),dict) else {},['side','type','direction']))
        side='buy' if raw in {'buy','in','bought'} else ('sell' if raw in {'sell','out','sold'} else '')
    if volume<=0:
        volume=_find_number_by_key(r,['sda','sda_value','sda_amount','volume']) or 0

    if not token or not dt or not side or volume<=0:return None
    if volume < MIN_WHALE_SDA:return None
    return {'tx_hash':str(h or f'pinet-{token}-{dt.timestamp()}-{volume}-{side}'),'token_address':token,
            'symbol':str(symbol or ''),'tx_type':side,'volume_in_sda':volume,'tx_timestamp':dt.isoformat(),
            'wallet':str(wallet or ''),'source':'pinetswap','source_score':num(score) if score is not None else None}

def http_json(url, params=None):
    r=requests.get(url,params=params,headers={'User-Agent':UA,'Accept':'application/json,text/plain,*/*'},timeout=25)
    r.raise_for_status(); return r.json()

def discover_api_urls(html):
    urls=set()
    # Absolute URLs and same-origin relative API paths containing whale.
    for m in re.findall(r'https?://[^\"\'<>\\ ]+',html):
        if 'whale' in m.lower():urls.add(m.rstrip('),;'))
    for m in re.findall(r'[\"\'](/[^\"\']*(?:whale|Whale)[^\"\']*)[\"\']',html):
        urls.add(urljoin(PINET_PAGE,m))
    scripts=re.findall(r'<script[^>]+src=[\"\']([^\"\']+)',html,re.I)
    return urls,scripts

def fetch_pinet():
    candidates=[]; notes=[]; raw_count=0; normalized=0; rejected={'token':0,'time':0,'side':0,'volume':0,'threshold':0}
    if PINET_API:candidates.append(PINET_API)
    html=''
    try:
        r=requests.get(PINET_PAGE,headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml'},timeout=25); r.raise_for_status(); html=r.text
        # Next.js / RSC often embeds the actual whale rows in the HTML.
        for marker in ('__NEXT_DATA__','self.__next_f.push','whale'):
            if marker in html: notes.append('page contains '+marker)
        urls,scripts=discover_api_urls(html); candidates += list(urls)
        # Capture all likely data endpoints, not just those with whale in the path.
        for m in re.findall(r'["\'](/(?:api|_next/data)/[^"\']+)["\']',html):
            if any(x in m.lower() for x in ('whale','smart','trader','transfer','transaction')): candidates.append(urljoin(PINET_PAGE,m))
        scripts=re.findall(r'<script[^>]+src=["\']([^"\']+)',html,re.I)
        for sref in scripts[:40]:
            u=urljoin(PINET_PAGE,sref)
            try:
                j=requests.get(u,headers={'User-Agent':UA,'Accept':'*/*'},timeout=20).text
                for m in re.findall(r'(?:https?://[^"\' ]+|/(?:api|_next/data)/[^"\' ]+)',j):
                    if any(x in m.lower() for x in ('whale','smart','trader','transfer','transaction')):
                        candidates.append(urljoin(PINET_PAGE,m).rstrip('),;'))
            except Exception: pass
    except Exception as e: notes.append('page: '+str(e))
    candidates += [urljoin(PINET_PAGE,x) for x in [
        '/api/whales','/api/v1/whales','/api/analytics/whales','/api/whale-alerts',
        '/api/analytics/whale-transactions','/api/market/whales','/api/top-traders','/api/smart-money'
    ]]
    seen=set(); rows=[]
    for u in candidates:
        base=u.split('?')[0]
        if base in seen:continue
        seen.add(base)
        try:
            obj=http_json(u,params={'limit':1000,'days':7,'page':1})
            rr=extract_rows(obj); raw_count+=len(rr)
            ok=0
            for raw in rr:
                z=normalize_row(raw)
                if z:rr2=z;rows.append(rr2);ok+=1
            if ok: notes.append(f'Pinet source: {base} raw={len(rr)} normalized={ok}')
        except Exception as e: notes.append(f'{base}: {e}')
    out=[];seenh=set()
    for x in rows:
        if x['tx_hash'] in seenh:continue
        seenh.add(x['tx_hash']);out.append(x)
    return out,notes,raw_count

def fetch_supabase():
    p={'select':'*','order':'tx_timestamp.desc','limit':'1000'}
    r=requests.get(SUPABASE_URL,params=p,headers=SUPABASE_HEADERS,timeout=30);r.raise_for_status()
    obj=r.json(); out=[]
    for raw in obj if isinstance(obj,list) else []:
        z=normalize_row(raw)
        if z:z['source']='supabase';out.append(z)
    return out

def window(history,address,minutes):
    cut=datetime.now(timezone.utc)-timedelta(minutes=minutes);bv=sv=0;bc=sc=0
    for x in history:
        if x.get('token_address')!=address:continue
        t=ts(x.get('tx_timestamp'))
        if not t or t<cut:continue
        v=num(x.get('volume_in_sda'))
        if x.get('tx_type')=='buy':bv+=v;bc+=1
        elif x.get('tx_type')=='sell':sv+=v;sc+=1
    return {'buy_volume':round(bv,6),'sell_volume':round(sv,6),'buy_count':bc,'sell_count':sc,'net_flow':round(bv-sv,6)}

def main():
    hist=load(HISTORY_FILE,[]); hist=hist if isinstance(hist,list) else []
    pinet,notes,raw_pinet=fetch_pinet()
    source='PinetSwap' if pinet else 'Supabase fallback'
    rows=pinet
    if not rows:
        try:
            rows=fetch_supabase(); notes.append(f'Supabase fallback qualifying rows: {len(rows)}')
        except Exception as e: notes.append('Supabase: '+str(e))
    byhash={str(x.get('tx_hash')):x for x in hist if isinstance(x,dict) and x.get('tx_hash')}
    new=0
    for x in rows:
        h=str(x['tx_hash'])
        if h not in byhash:byhash[h]=x;new+=1
    cutoff=datetime.now(timezone.utc)-timedelta(hours=HOURS)
    hist=[x for x in byhash.values() if ts(x.get('tx_timestamp')) and ts(x.get('tx_timestamp'))>=cutoff]
    data={}
    for a in sorted({x['token_address'] for x in hist}):
        w={k:window(hist,a,m) for k,m in WINDOWS.items()}
        sym=next((x.get('symbol') for x in reversed(hist) if x.get('token_address')==a and x.get('symbol')), '')
        data[a]={'symbol':sym,'windows':w,'whale_buy_volume':w['1h']['buy_volume'],'whale_sell_volume':w['1h']['sell_volume'],
                 'whale_buy_count':w['1h']['buy_count'],'whale_sell_count':w['1h']['sell_count'],'current_1h_net':w['1h']['net_flow'],
                 'updated_at':datetime.now(timezone.utc).isoformat(),'source':source}
    save(HISTORY_FILE,hist);save(DATA_FILE,data)
    state=load(STATE_FILE,{}) if isinstance(load(STATE_FILE,{}),dict) else {}
    state.update({'updated_at':datetime.now(timezone.utc).isoformat(),'source':source,'history_transactions':len(hist),'tracked_tokens':len(data),'last_loaded_transactions':len(rows),'last_new_transactions':new,'notes':notes[-20:]})
    save(STATE_FILE,state)
    print('WHALE SCANNER — PINETSWAP PRIMARY')
    print('Source:',source);print('Rows:',len(rows),'New:',new,'History:',len(hist),'Tokens:',len(data))
    for n in notes[-10:]:print(' ',n)

if __name__=='__main__':
    try:main()
    except Exception:
        e=traceback.format_exc();print(e)
        if TOKEN and CHAT_ID:
            try:requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':'WHALE SCANNER ERROR\n\n'+e[:4000]},timeout=30)
            except Exception:pass
        raise
