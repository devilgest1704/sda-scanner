import json
from datetime import datetime, timezone
from decimal import Decimal
import requests

RPC = "https://node.sidrachain.com"
EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET_FILE = "market_data.json"
META_FILE = "token_metadata.json"
OUT_FILE = "liquidity_data.json"
DISCOVERY_FILE = "sidra_swap_discovery.json"
TRADE_SIZE_SDA = 50.0
SWAP_SELECTOR = "0x8ab5246f"
SELL_SELECTOR = "0x75b5c5d8"
TIMEOUT = 12
MAX_TXS = 50
DETAIL_LIMIT = 20

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/7.0"})

def now():
    return datetime.now(timezone.utc).isoformat()

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def addr_hash(x):
    if isinstance(x, dict):
        return x.get("hash") or x.get("address_hash") or ""
    return str(x or "")

def selector(raw):
    if isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 10:
        return raw[:10].lower()
    return None

def get_json(url, params=None):
    r = session.get(url, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def rpc(method, params):
    r = session.post(RPC, json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
                     timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(str(d["error"]))
    return d.get("result")

def explorer_transactions():
    url = f"{EXPLORER_API}/addresses/{POOL}/transactions"
    return get_json(url, {"filter":"to", "items_count":MAX_TXS}).get("items", [])

def transaction_detail(tx_hash):
    return get_json(f"{EXPLORER_API}/transactions/{tx_hash}")

def transaction_token_transfers(tx_hash):
    try:
        d = get_json(f"{EXPLORER_API}/transactions/{tx_hash}/token-transfers")
        return d.get("items", []) if isinstance(d, dict) else []
    except Exception:
        return []

def decode_selector_call(raw):
    if selector(raw) != SWAP_SELECTOR:
        return None
    body = raw[10:]
    if len(body) < 192:
        return None
    return {
        "token": "0x" + body[24:64],
        "arg1": int(body[64:128],16),
        "arg2": int(body[128:192],16),
    }

def amount_value(obj):
    for k in ("total", "value", "amount", "raw_value", "value_formatted"):
        if obj.get(k) is not None:
            return obj.get(k)
    return None

def transfer_token(t):
    tok=t.get("token") or {}
    return (tok.get("address_hash") or tok.get("hash") or
            tok.get("contract_address") or t.get("token_contract_address_hash") or "")

def transfer_from(t):
    return addr_hash(t.get("from")) or t.get("from_address_hash") or ""

def transfer_to(t):
    return addr_hash(t.get("to")) or t.get("to_address_hash") or ""

def transfer_decimals(t):
    tok=t.get("token") or {}
    for k in ("decimals","token_decimals"):
        try:
            if tok.get(k) is not None:
                return int(tok[k])
        except Exception:
            pass
    return None

def normalize_transfer(t):
    raw=amount_value(t)
    dec=transfer_decimals(t)
    formatted=None
    if isinstance(raw, str) and raw.isdigit() and dec is not None:
        try:
            formatted=float(Decimal(raw)/(Decimal(10)**dec))
        except Exception:
            pass
    return {
        "token": transfer_token(t).lower(),
        "from": transfer_from(t),
        "to": transfer_to(t),
        "raw_value": raw,
        "decimals": dec,
        "value": formatted,
        "symbol": (t.get("token") or {}).get("symbol") if isinstance(t.get("token"),dict) else None,
    }

def analyze_tx(tx):
    h=tx.get("hash")
    detail={}
    errors=[]
    try:
        detail=transaction_detail(h)
    except Exception as e:
        errors.append("detail: "+str(e))
    transfers=transaction_token_transfers(h)
    raw=detail.get("raw_input") or tx.get("raw_input") or ""
    call=decode_selector_call(raw)
    token=call["token"].lower() if call else ""
    user=addr_hash(detail.get("from") or tx.get("from")).lower()
    sda_in=sda_out=token_in=token_out=0.0
    norm=[]
    for x in transfers:
        t=normalize_transfer(x)
        norm.append(t)
        val=t.get("value")
        if val is None: continue
        tk=t["token"]; frm=t["from"].lower(); to=t["to"].lower()
        sym=(t.get("symbol") or "").upper()
        is_sda=(tk==WSDA.lower() or sym in ("SDA","WSDA"))
        is_target=bool(token) and tk==token
        if is_sda and frm==user and to==POOL.lower(): sda_in+=val
        if is_sda and frm==POOL.lower() and to==user: sda_out+=val
        if is_target and frm==POOL.lower() and to==user: token_out+=val
        if is_target and frm==user and to==POOL.lower(): token_in+=val
    return {
        "hash":h,
        "block":detail.get("block") or tx.get("block_number"),
        "status":detail.get("status") or tx.get("status"),
        "from":user,
        "to":addr_hash(detail.get("to") or tx.get("to")),
        "value":detail.get("value",tx.get("value","0")),
        "method":detail.get("method") or tx.get("method") or "",
        "selector":selector(raw),
        "raw_input":raw,
        "call":call,
        "flow":{"sda_in":sda_in,"sda_out":sda_out,"token_in":token_in,
                "token_out":token_out,"target_token":token,
                "transfer_count":len(norm)},
        "transfers":norm,
        "errors":errors,
    }

def main():
    print("💧 LIQUIDITY V7 — REVERSE ENGINEER REAL SWAP TRANSACTIONS")
    print(f"Pool: {POOL}")
    print(f"Target trade: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / explorer GET + eth_call only / no broadcast")

    market=load_json(MARKET_FILE,{})
    try:
        txs=explorer_transactions()
        api_error=None
    except Exception as e:
        txs=[]; api_error=str(e)

    calls=[tx for tx in txs[:MAX_TXS]
           if not addr_hash(tx.get("to")) or addr_hash(tx.get("to")).lower()==POOL.lower()]
    counts={}
    for tx in calls:
        s=selector(tx.get("raw_input") or "")
        if s: counts[s]=counts.get(s,0)+1
    swap_txs=[x for x in calls if selector(x.get("raw_input") or "")==SWAP_SELECTOR]

    print(f"Pool calls discovered: {len(calls)}")
    print("Selector counts:")
    for s,n in sorted(counts.items(),key=lambda x:-x[1])[:10]:
        print(f"  {s}: {n}")
    print(f"Real 0x8ab5246f transactions selected: {len(swap_txs)}")

    analyses=[]
    for tx in swap_txs[:DETAIL_LIMIT]:
        a=analyze_tx(tx); analyses.append(a)
        c=a.get("call") or {}; f=a["flow"]
        print(f"\nTX {a.get('hash')}")
        print(f"  token={c.get('token')} arg1={c.get('arg1')} arg2={c.get('arg2')}")
        print(f"  SDA user→pool={f['sda_in']} | pool→user={f['sda_out']}")
        print(f"  token user→pool={f['token_in']} | pool→user={f['token_out']}")
        print(f"  transfers={f['transfer_count']} errors={a.get('errors')}")
        if a.get("transfers"):
            for t in a["transfers"][:12]:
                print(f"    {t.get('symbol') or t.get('token')} "
                      f"{t.get('value')} {t.get('from','')[:10]}→{t.get('to','')[:10]}")

    pairs=[]
    for a in analyses:
        c=a.get("call") or {}; f=a["flow"]
        if f["sda_in"]>0 and f["token_out"]>0:
            pairs.append({
                "tx":a["hash"],"token":c.get("token"),
                "arg1":c.get("arg1"),"arg2":c.get("arg2"),
                "sda_in":f["sda_in"],"token_out":f["token_out"],
                "tokens_per_sda":f["token_out"]/f["sda_in"]
            })

    discovery={
        "updated_at":now(),"version":"V7","pool":POOL,"wsda":WSDA,
        "trade_size_sda":TRADE_SIZE_SDA,"api_error":api_error,
        "transactions_seen":len(calls),"selector_counts":counts,
        "selector":SWAP_SELECTOR,"samples":analyses,
        "usable_input_output_pairs":pairs,
    }
    liquidity={
        "updated_at":now(),"version":"V7","pool_address":POOL,
        "wsda_address":WSDA,"trade_size_sda":TRADE_SIZE_SDA,
        "status":"UNKNOWN","selector":SWAP_SELECTOR,"tokens":{},
        "errors":[],"reason":"Reverse-engineering real swap input/output flows",
    }
    for r in pairs:
        e=liquidity["tokens"].setdefault(r["token"],{
            "status":"OBSERVED","observed_sda_in":0.0,
            "observed_token_out":0.0,"samples":0})
        e["observed_sda_in"]+=r["sda_in"]
        e["observed_token_out"]+=r["token_out"]
        e["samples"]+=1
        e["observed_tokens_per_sda"]=e["observed_token_out"]/e["observed_sda_in"]

    with open(DISCOVERY_FILE,"w",encoding="utf-8") as f:
        json.dump(discovery,f,indent=2,ensure_ascii=False)
    with open(OUT_FILE,"w",encoding="utf-8") as f:
        json.dump(liquidity,f,indent=2,ensure_ascii=False)

    print(f"\nObserved input/output pairs: {len(pairs)}")
    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {OUT_FILE}")
    print("Overall liquidity status: UNKNOWN")
    print("Next: use observed SDA/token transfers to identify exact arg semantics.")

if __name__=="__main__":
    main()
