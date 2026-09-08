# SDA whale scanner — robust rolling 15m/30m/1h/4h
import os,json,traceback
from datetime import datetime,timezone,timedelta
import requests

TOKEN=os.environ.get("TELEGRAM_TOKEN"); CHAT_ID=os.environ.get("CHAT_ID")
URL="https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
HEADERS={"apikey":"sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z","accept-profile":"public"}
DATA="whale_data.json"; HISTORY="whale_history.json"; STATE="whale_state.json"
FETCH_LIMIT=1000; THRESHOLD=5000.0; HISTORY_HOURS=6
WINDOWS={"15m":15,"30m":30,"1h":60,"4h":240}

def load(p,d):
    try:
        with open(p,encoding="utf-8") as f:return json.load(f)
    except:return d

def save(p,d):
    t=p+".tmp"
    with open(t,"w",encoding="utf-8") as f:json.dump(d,f,indent=2,ensure_ascii=False)
    os.replace(t,p)

def num(v,d=0.0):
    try:return d if v is None or v=="" else float(v)
    except:return d

def ts(v):
    try:
        if isinstance(v,(int,float)):return datetime.fromtimestamp(float(v),tz=timezone.utc)
        s=str(v).strip()
        if s.endswith("Z"):s=s[:-1]+"+00:00"
        d=datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except:return None

def first(x,keys):
    for k in keys:
        v=x.get(k) if isinstance(x,dict) else None
        if v is not None and v!="":return v
    return None

def classify(x):
    raw=str(first(x,["tx_type","trade_type","side","type","action","direction"]) or "").strip().lower()
    if raw in {"buy","b","purchase","swap_buy","token_buy","in"}: return "buy"
    if raw in {"sell","s","sale","swap_sell","token_sell","out"}: return "sell"
    return ""

def normalize(x):
    if not isinstance(x,dict):return None,"bad_row"
    h=first(x,["tx_hash","hash","transaction_hash","id"])
    a=first(x,["token_address","token","address","contract_address"])
    t=first(x,["tx_timestamp","timestamp","block_timestamp","created_at"])
    typ=classify(x)
    v=first(x,["volume_in_sda","volume_sda","amount_in_sda","sda_amount","value_sda","trade_volume_sda","volume"])
    if not h:return None,"no_hash"
    if not a:return None,"no_token"
    if not typ:return None,"unknown_side"
    dt=ts(t)
    if not dt:return None,"bad_timestamp"
    vol=num(v)
    if vol < THRESHOLD:return None,"below_threshold"
    return {"tx_hash":str(h),"token_address":str(a).lower(),"tx_timestamp":dt.isoformat(),"tx_type":typ,"volume_in_sda":vol},"ok"

def fetch():
    params={"select":"*","order":"tx_timestamp.desc","limit":FETCH_LIMIT}
    r=requests.get(URL,params=params,headers=HEADERS,timeout=30);r.raise_for_status()
    x=r.json();return x if isinstance(x,list) else []

def merge(old,rows):
    by={str(x.get("tx_hash")):x for x in old if isinstance(x,dict) and x.get("tx_hash")}
    stats={"ok":0,"below_threshold":0,"unknown_side":0,"no_hash":0,"no_token":0,"bad_timestamp":0,"bad_row":0}
    new=0
    for row in rows:
        z,reason=normalize(row);stats[reason]=stats.get(reason,0)+1
        if not z:continue
        stats["ok"]+=1
        h=z["tx_hash"]
        if h not in by:new+=1
        by[h]=z
    return list(by.values()),new,stats

def trim(hist):
    cutoff=datetime.now(timezone.utc)-timedelta(hours=HISTORY_HOURS)
    return [x for x in hist if (ts(x.get("tx_timestamp")) or datetime.min.replace(tzinfo=timezone.utc))>=cutoff]

def window(hist,address,minutes):
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=minutes); b=s=0.0;bc=sc=0
    for x in hist:
        if x.get("token_address")!=address:continue
        t=ts(x.get("tx_timestamp"))
        if not t or t<cutoff:continue
        v=num(x.get("volume_in_sda"))
        if x.get("tx_type")=="buy":b+=v;bc+=1
        elif x.get("tx_type")=="sell":s+=v;sc+=1
    return {"buy_volume":round(b,6),"sell_volume":round(s,6),"net_flow":round(b-s,6),"buy_count":bc,"sell_count":sc,"trade_count":bc+sc}

def send(s):
    print(s)
    if TOKEN and CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":s},timeout=30)
        except Exception:pass

def main():
    rows=fetch(); old=load(HISTORY,[]); hist,new,stats=merge(old,rows); hist=trim(hist)
    out={}; now=datetime.now(timezone.utc)
    for a in sorted({x["token_address"] for x in hist}):
        w={k:window(hist,a,m) for k,m in WINDOWS.items()}
        out[a]={"windows":w,"whale_buy_volume":w["1h"]["buy_volume"],"whale_sell_volume":w["1h"]["sell_volume"],"whale_buy_count":w["1h"]["buy_count"],"whale_sell_count":w["1h"]["sell_count"],"current_1h_net":w["1h"]["net_flow"],"updated_at":now.isoformat()}
    save(HISTORY,hist);save(DATA,out)
    state={"updated_at":now.isoformat(),"history_points":len(hist),"tokens":len(out),"source_rows":len(rows),"new_rows":new,"threshold_sda":THRESHOLD,"classification":stats}
    save(STATE,state)
    rank=sorted(((v["windows"]["1h"]["net_flow"],a,v["windows"]["1h"]) for a,v in out.items()),key=lambda z:abs(z[0]),reverse=True)
    send("🐋 WHALE DEBUG\n\n"+f"Loaded rows: {len(rows)}\nNormalized >= {THRESHOLD:.0f} SDA: {stats.get('ok',0)}\nNew qualifying: {new}\n6h history: {len(hist)}\nTracked tokens: {len(out)}\n\nTop 10 1h whale flows:")
    for net,a,w in rank[:10]:send(f"{a[:10]}... | buy {w['buy_volume']:.0f} | sell {w['sell_volume']:.0f} | net {net:+.0f} | trades {w['buy_count']}/{w['sell_count']}")

if __name__=="__main__":
    try:main()
    except Exception:
        e=traceback.format_exc();print(e);send("❌ WHALE SCANNER ERROR\n\n"+e[:3500]);raise
