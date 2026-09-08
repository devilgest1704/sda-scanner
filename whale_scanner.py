# Rolling whale scanner: 6h history, 15m/30m/1h/4h windows
import json,traceback
from datetime import datetime,timezone,timedelta
import requests
DATA="whale_data.json";HISTORY="whale_history.json";STATE="whale_state.json"
URL=("https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions?"
     "select=id,tx_hash,token_address,price_in_sda,volume_in_sda,tx_timestamp,tx_type"
     "&volume_in_sda=gte.5000&order=tx_timestamp.desc&limit=1000")
HEADERS={"apikey":"sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z","accept-profile":"public"}
WINDOWS={"15m":15,"30m":30,"1h":60,"4h":240}
def load(p,d):
    try:
        with open(p,encoding="utf-8") as f:return json.load(f)
    except:return d
def save(p,d):
    t=p+".tmp"
    with open(t,"w",encoding="utf-8") as f:json.dump(d,f,indent=2,ensure_ascii=False)
    import os;os.replace(t,p)
def ts(v):
    try:
        x=datetime.fromisoformat(str(v).replace("Z","+00:00"));return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except:return None
def main():
    r=requests.get(URL,headers=HEADERS,timeout=30);r.raise_for_status();rows=r.json()
    old=load(HISTORY,[]); by={str(x.get("tx_hash")):x for x in old if isinstance(x,dict) and x.get("tx_hash")}
    for x in rows:
        h=x.get("tx_hash");t=ts(x.get("tx_timestamp"));a=str(x.get("token_address") or "").lower();typ=str(x.get("tx_type") or "").lower();v=float(x.get("volume_in_sda") or 0)
        if h and t and a and typ in ("buy","sell") and v>0:by[str(h)]={"tx_hash":str(h),"token_address":a,"tx_timestamp":t.isoformat(),"tx_type":typ,"volume_in_sda":v}
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=360)
    hist=[x for x in by.values() if (ts(x.get("tx_timestamp")) or cutoff)>=cutoff]
    hist.sort(key=lambda x:x.get("tx_timestamp",""),reverse=True);save(HISTORY,hist)
    out={};n=datetime.now(timezone.utc)
    for a in {x["token_address"] for x in hist}:
        win={}
        for name,mins in WINDOWS.items():
            c=n-timedelta(minutes=mins);v=[x for x in hist if x["token_address"]==a and (ts(x["tx_timestamp"]) or n)>=c]
            b=sum(x["volume_in_sda"] for x in v if x["tx_type"]=="buy");s=sum(x["volume_in_sda"] for x in v if x["tx_type"]=="sell")
            win[name]={"buy_volume":b,"sell_volume":s,"net_flow":b-s,"buy_count":sum(x["tx_type"]=="buy" for x in v),"sell_count":sum(x["tx_type"]=="sell" for x in v),"trade_count":len(v)}
        out[a]={"windows":win,"whale_buy_volume":win["1h"]["buy_volume"],"whale_sell_volume":win["1h"]["sell_volume"],"whale_buy_count":win["1h"]["buy_count"],"whale_sell_count":win["1h"]["sell_count"],"current_1h_net":win["1h"]["net_flow"],"updated_at":n.isoformat()}
    save(DATA,out);save(STATE,{"updated_at":n.isoformat(),"history_points":len(hist),"tokens":len(out),"source_rows":len(rows)})
    print(f"🐋 WHALE DEBUG\n\nLoaded transactions: {len(rows)}\nRolling history: {len(hist)}\nTracked tokens: {len(out)}")
try:main()
except Exception:print(traceback.format_exc());raise
