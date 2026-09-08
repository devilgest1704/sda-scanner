# SDA whale scanner — rolling 15m/30m/1h/4h
import os, json, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

TOKEN=os.environ.get("TELEGRAM_TOKEN")
CHAT_ID=os.environ.get("CHAT_ID")
SUPABASE_URL="https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
HEADERS={"apikey":"sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z","accept-profile":"public"}
WHALE_DATA_FILE="whale_data.json"
WHALE_HISTORY_FILE="whale_history.json"
WHALE_STATE_FILE="whale_state.json"
FETCH_LIMIT=1000
WHALE_THRESHOLD_SDA=5000.0
HISTORY_HOURS=6
WINDOWS={"15m":15,"30m":30,"1h":60,"4h":240}

def load_json(path, default):
    try:
        with open(path,encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save_json(path,data):
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(data,f,indent=2,ensure_ascii=False)
    os.replace(tmp,path)

def num(v,d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d

def ts(v):
    try:
        if isinstance(v,(int,float)): return datetime.fromtimestamp(float(v),tz=timezone.utc)
        s=str(v).strip()
        if s.endswith("Z"): s=s[:-1]+"+00:00"
        d=datetime.fromisoformat(s)
        return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d).astimezone(timezone.utc)
    except Exception: return None

def normalize_history(history):
    if not isinstance(history,list): return []
    out=[]
    for x in history:
        if not isinstance(x,dict): continue
        a=str(x.get("token_address") or "").lower()
        h=x.get("tx_hash")
        typ=str(x.get("tx_type") or "").lower()
        t=x.get("tx_timestamp")
        if h and a and typ in {"buy","sell"} and ts(t):
            out.append({"tx_hash":str(h),"token_address":a,"tx_type":typ,
                        "volume_in_sda":num(x.get("volume_in_sda")),"tx_timestamp":t})
    return out

def fetch():
    p={"select":"id,tx_hash,token_address,price_in_sda,volume_in_sda,tx_timestamp,tx_type",
       "volume_in_sda":f"gte.{WHALE_THRESHOLD_SDA}","order":"tx_timestamp.desc","limit":FETCH_LIMIT}
    r=requests.get(SUPABASE_URL,params=p,headers=HEADERS,timeout=30)
    r.raise_for_status()
    x=r.json()
    return x if isinstance(x,list) else []

def merge(history,rows):
    seen={str(x.get("tx_hash")) for x in history if isinstance(x,dict) and x.get("tx_hash")}
    n=0
    for x in rows:
        h=x.get("tx_hash"); a=str(x.get("token_address") or "").lower()
        typ=str(x.get("tx_type") or "").lower(); t=x.get("tx_timestamp")
        if not h or str(h) in seen or not a or typ not in {"buy","sell"} or not ts(t): continue
        history.append({"tx_hash":str(h),"token_address":a,"tx_type":typ,
                        "volume_in_sda":num(x.get("volume_in_sda")),"tx_timestamp":t})
        seen.add(str(h)); n+=1
    return history,n

def trim(history):
    cutoff=datetime.now(timezone.utc)-timedelta(hours=HISTORY_HOURS)
    return [x for x in history if ts(x.get("tx_timestamp")) and ts(x.get("tx_timestamp"))>=cutoff]

def window(history,address,minutes):
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=minutes)
    bv=sv=0.0; bc=sc=0
    for x in history:
        if x.get("token_address")!=address: continue
        t=ts(x.get("tx_timestamp"))
        if not t or t<cutoff: continue
        v=num(x.get("volume_in_sda"))
        if x.get("tx_type")=="buy": bv+=v; bc+=1
        elif x.get("tx_type")=="sell": sv+=v; sc+=1
    return {"buy_volume":round(bv,6),"sell_volume":round(sv,6),
            "buy_count":bc,"sell_count":sc,"net_flow":round(bv-sv,6)}

def main():
    print("WHALE SCANNER — ROLLING 15m/30m/1h/4h")
    rows=fetch()
    history,n=merge(normalize_history(load_json(WHALE_HISTORY_FILE,[])),rows)
    history=trim(history)
    data={}
    for a in sorted({x["token_address"] for x in history}):
        w={k:window(history,a,m) for k,m in WINDOWS.items()}
        data[a]={"windows":w,
                 "whale_buy_volume":w["1h"]["buy_volume"],
                 "whale_sell_volume":w["1h"]["sell_volume"],
                 "whale_buy_count":w["1h"]["buy_count"],
                 "whale_sell_count":w["1h"]["sell_count"],
                 "current_1h_net":w["1h"]["net_flow"],
                 "updated_at":datetime.now(timezone.utc).isoformat()}
    save_json(WHALE_HISTORY_FILE,history); save_json(WHALE_DATA_FILE,data)
    state=load_json(WHALE_STATE_FILE,{})
    if not isinstance(state,dict): state={}
    state.update({"updated_at":datetime.now(timezone.utc).isoformat(),
                  "history_transactions":len(history),"tracked_tokens":len(data),
                  "last_loaded_transactions":len(rows),"last_new_transactions":n})
    save_json(WHALE_STATE_FILE,state)
    rank=[]
    for a,w in data.items():
        if not isinstance(w,dict): continue
        q=w.get("windows",{}).get("1h",{})
        rank.append((num(q.get("net_flow")),a,num(q.get("buy_volume")),
                     num(q.get("sell_volume")),int(num(q.get("buy_count"))),int(num(q.get("sell_count")))))
    rank.sort(key=lambda x:abs(x[0]),reverse=True)
    print(f"Loaded transactions: {len(rows)}")
    print(f"New transactions: {n}")
    print(f"History transactions: {len(history)}")
    print(f"Tracked tokens: {len(data)}")
    for r in rank[:10]: print(f"{r[1][:10]}... buy={r[2]:.0f} sell={r[3]:.0f} net={r[0]:+.0f} trades={r[4]}/{r[5]}")

if __name__=="__main__":
    try: main()
    except Exception:
        e=traceback.format_exc(); print(e)
        if TOKEN and CHAT_ID:
            try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",json={"chat_id":CHAT_ID,"text":"WHALE SCANNER ERROR\n\n"+e[:4000]},timeout=30)
            except Exception: pass
        raise