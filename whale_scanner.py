# SDA whale scanner — direct PinetSwap Supabase source
# Read-only. Uses the exact token_transactions endpoint observed from PinetSwap Whale Alerts.
import os, json, traceback
from datetime import datetime, timezone, timedelta
import requests

TOKEN=os.environ.get('TELEGRAM_TOKEN')
CHAT_ID=os.environ.get('CHAT_ID')
SUPABASE_URL='https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions'
SUPABASE_HEADERS={
    'apikey':'sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z',
    'accept-profile':'public',
}
HISTORY_FILE='whale_history.json'
DATA_FILE='whale_data.json'
STATE_FILE='whale_state.json'
META_FILE='token_metadata.json'
HISTORY_HOURS=6
FETCH_LIMIT=int(os.environ.get('WHALE_FETCH_LIMIT','1000'))
MIN_WHALE_SDA=float(os.environ.get('WHALE_THRESHOLD_SDA','200'))
WINDOWS={'15m':15,'30m':30,'1h':60,'4h':240}
SOURCE='pinet-supabase-token_transactions-v2'


def load(path, default):
    try:
        with open(path,encoding='utf-8') as f: return json.load(f)
    except Exception: return default


def save(path, data):
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(data,f,indent=2,ensure_ascii=False)
    os.replace(tmp,path)


def num(v,d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d


def ts(v):
    try:
        if isinstance(v,(int,float)): return datetime.fromtimestamp(float(v),tz=timezone.utc)
        s=str(v).strip()
        if s.endswith('Z'): s=s[:-1]+'+00:00'
        d=datetime.fromisoformat(s)
        return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).astimezone(timezone.utc)
    except Exception: return None


def fetch_supabase():
    since=(datetime.now(timezone.utc)-timedelta(hours=HISTORY_HOURS)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    params={
        'select':'id,tx_hash,token_address,from_address,to_address,price_in_sda,volume_in_sda,tx_timestamp,tx_type',
        'tx_timestamp':f'gte.{since}',
        'volume_in_sda':f'gte.{MIN_WHALE_SDA:g}',
        'order':'tx_timestamp.desc',
        'limit':str(FETCH_LIMIT),
    }
    r=requests.get(SUPABASE_URL,params=params,headers=SUPABASE_HEADERS,timeout=30)
    r.raise_for_status()
    obj=r.json()
    return obj if isinstance(obj,list) else []


def normalize(rows):
    out=[]; seen=set(); skipped_side=skipped_volume=skipped_time=0
    for x in rows:
        if not isinstance(x,dict): continue
        h=str(x.get('tx_hash') or x.get('id') or '')
        a=str(x.get('token_address') or '').lower()
        side=str(x.get('tx_type') or '').lower().strip()
        volume=num(x.get('volume_in_sda'))
        stamp=ts(x.get('tx_timestamp'))
        if side not in {'buy','sell'}:
            skipped_side+=1; continue
        if volume < MIN_WHALE_SDA:
            skipped_volume+=1; continue
        if not stamp:
            skipped_time+=1; continue
        if not h or not a: continue
        if h in seen: continue
        seen.add(h)
        out.append({
            'id':x.get('id'),
            'tx_hash':h,
            'token_address':a,
            'from_address':str(x.get('from_address') or '').lower(),
            'to_address':str(x.get('to_address') or '').lower(),
            'price_in_sda':num(x.get('price_in_sda')),
            'volume_in_sda':volume,
            'tx_timestamp':stamp.isoformat(),
            'tx_type':side,
            'source':SOURCE,
        })
    return out, {'skipped_side':skipped_side,'skipped_volume':skipped_volume,'skipped_time':skipped_time}


def window(history,address,minutes):
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=minutes)
    bv=sv=0.0; bc=sc=0
    for x in history:
        if x.get('token_address')!=address: continue
        t=ts(x.get('tx_timestamp'))
        if not t or t<cutoff: continue
        v=num(x.get('volume_in_sda'))
        if x.get('tx_type')=='buy': bv+=v; bc+=1
        elif x.get('tx_type')=='sell': sv+=v; sc+=1
    return {
        'buy_volume':round(bv,6),
        'sell_volume':round(sv,6),
        'buy_count':bc,
        'sell_count':sc,
        'net_flow':round(bv-sv,6),
    }


def main():
    meta=load(META_FILE,{})
    raw=fetch_supabase()
    rows,stats=normalize(raw)

    # Do not mix the old 5000-SDA/autodiscovery history with the new source.
    old=load(HISTORY_FILE,[])
    if not isinstance(old,list): old=[]
    history=[x for x in old if isinstance(x,dict) and x.get('source')==SOURCE]

    by_hash={str(x.get('tx_hash')):x for x in history if x.get('tx_hash')}
    new=0
    for x in rows:
        h=x['tx_hash']
        if h not in by_hash:
            by_hash[h]=x
            new+=1

    cutoff=datetime.now(timezone.utc)-timedelta(hours=HISTORY_HOURS)
    history=[x for x in by_hash.values() if ts(x.get('tx_timestamp')) and ts(x.get('tx_timestamp'))>=cutoff]
    history.sort(key=lambda x:ts(x.get('tx_timestamp')) or datetime.min.replace(tzinfo=timezone.utc),reverse=True)

    data={}
    for address in sorted({x['token_address'] for x in history}):
        windows={k:window(history,address,m) for k,m in WINDOWS.items()}
        md=meta.get(address,{}) if isinstance(meta,dict) else {}
        symbol=md.get('symbol') if isinstance(md,dict) else None
        data[address]={
            'symbol':symbol or '',
            'windows':windows,
            'whale_buy_volume':windows['1h']['buy_volume'],
            'whale_sell_volume':windows['1h']['sell_volume'],
            'whale_buy_count':windows['1h']['buy_count'],
            'whale_sell_count':windows['1h']['sell_count'],
            'current_1h_net':windows['1h']['net_flow'],
            'updated_at':datetime.now(timezone.utc).isoformat(),
            'source':SOURCE,
        }

    save(HISTORY_FILE,history)
    save(DATA_FILE,data)

    buys=sum(1 for x in rows if x['tx_type']=='buy')
    sells=sum(1 for x in rows if x['tx_type']=='sell')
    rank=[]
    for address,w in data.items():
        q=w['windows']['1h']
        rank.append((abs(num(q['net_flow'])),address,q))
    rank.sort(reverse=True)

    state=load(STATE_FILE,{})
    if not isinstance(state,dict): state={}
    state.update({
        'updated_at':datetime.now(timezone.utc).isoformat(),
        'source':SOURCE,
        'threshold_sda':MIN_WHALE_SDA,
        'fetch_limit':FETCH_LIMIT,
        'history_hours':HISTORY_HOURS,
        'history_transactions':len(history),
        'tracked_tokens':len(data),
        'last_loaded_transactions':len(rows),
        'last_new_transactions':new,
        'raw_buy_count':buys,
        'raw_sell_count':sells,
        'normalize_stats':stats,
    })
    save(STATE_FILE,state)

    print('WHALE SCANNER — PINETSWAP SUPABASE')
    print(f'Source: {SOURCE}')
    print(f'Threshold: {MIN_WHALE_SDA:g} SDA')
    print(f'Fetch limit: {FETCH_LIMIT}')
    print(f'Loaded rows: {len(rows)} (BUY {buys} / SELL {sells})')
    print(f'New rows: {new}')
    print(f'6h history: {len(history)}')
    print(f'Tracked tokens: {len(data)}')
    print('Top 10 1h whale flows:')
    for i,(_,address,q) in enumerate(rank[:10],1):
        sym=(data[address].get('symbol') or '')
        name=f'{sym}/{"SDA"}' if sym else address[:10]+'...'
        print(f'{i}. {name} net={q["net_flow"]:+.0f} buy={q["buy_volume"]:.0f} sell={q["sell_volume"]:.0f} trades={q["buy_count"]}/{q["sell_count"]}')


if __name__=='__main__':
    try:
        main()
    except Exception:
        e=traceback.format_exc(); print(e)
        if TOKEN and CHAT_ID:
            try:
                requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':'WHALE SCANNER ERROR\n\n'+e[:4000]},timeout=30)
            except Exception: pass
        raise
