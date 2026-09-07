import json, time
from decimal import Decimal, getcontext
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from Crypto.Hash import keccak

getcontext().prec = 60

RPC = "https://node.sidrachain.com"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET = Path("market_data.json")
OUT = Path("liquidity_data.json")
TRADE = Decimal("50")
FEE = Decimal("0.01")
TIMEOUT = 6

session = requests.Session()

# We only probe READ calls. Never execute sidraBuyWithFee itself.
READ_SIGNATURES = [
    "quoteBuy(address,uint256)",
    "quoteBuy(address,uint256,uint256)",
    "getBuyQuote(address,uint256)",
    "getBuyQuote(address,uint256,uint256)",
    "getAmountOut(address,uint256)",
    "getAmountOut(uint256,address)",
    "getAmountOut(uint256,address,uint256)",
    "sidraQuoteBuy(address,uint256)",
    "sidraQuoteBuy(address,uint256,uint256)",
    "quote(address,uint256)",
    "quote(address,uint256,uint256)",
]


def selector(sig):
    k = keccak.new(digest_bits=256); k.update(sig.encode())
    return k.hexdigest()[:8]


def word_addr(a): return a.lower().replace("0x", "").rjust(64, "0")
def word_uint(x): return int(x).to_bytes(32, "big").hex()


def rpc(method, params):
    r = session.post(RPC, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=TIMEOUT)
    r.raise_for_status(); j = r.json()
    if "error" in j: raise RuntimeError(str(j["error"]))
    return j.get("result")


def call(data): return rpc("eth_call", [{"to":POOL,"data":data}, "latest"])


def load_market():
    if not MARKET.exists(): return []
    raw = json.loads(MARKET.read_text(encoding="utf-8")); data = raw.get("tokens", raw)
    return [(a.lower(),v) for a,v in data.items() if isinstance(a,str) and len(a)==42 and a.startswith("0x") and isinstance(v,dict) and a.lower()!=WSDA.lower()]


def label(a,d): return d.get("symbol") or d.get("name") or (d.get("analysis") or {}).get("symbol") or f"{a[:6]}...{a[-4:]}"


def decimals(d):
    try: return int(d.get("decimals",18))
    except: return 18


def encode(sig, token, amount):
    types = sig.split("(",1)[1].split(")",1)[0].split(",")
    if types == ["address","uint256"]: return word_addr(token)+word_uint(amount)
    if types == ["uint256","address"]: return word_uint(amount)+word_addr(token)
    if types == ["address","uint256","uint256"]: return word_addr(token)+word_uint(amount)+word_uint(1)
    if types == ["uint256","address","uint256"]: return word_uint(amount)+word_addr(token)+word_uint(1)
    return None


def probe(token, sig):
    enc=encode(sig,token,int(TRADE*Decimal(10**18)))
    if not enc: return None
    try:
        raw=call("0x"+selector(sig)+enc)
        if raw and raw != "0x":
            # Accept only a clean single uint256 return (32 bytes).
            if len(raw)==66:
                v=int(raw,16)
                if v>0: return {"signature":sig,"selector":"0x"+selector(sig),"raw_output":v}
    except Exception: pass
    return None


def main():
    started=time.time(); tokens=load_market()
    result={"updated_at":int(time.time()),"rpc":RPC,"swap_v3_pool":POOL,"wsda_address":WSDA,
            "trade_size_sda":50.0,"platform_fee_rate":0.01,"status":"UNKNOWN",
            "method":"sidra_dex_liquidity_v3","quote_method":None,"tokens":{},"errors":[],
            "warning":"Liquidity is UNKNOWN until a real read-only quote is found. Zero is never used to mean unknown."}
    try:
        code=rpc("eth_getCode",[POOL,"latest"]); result["contract_code_bytes"]=max(0,(len(code or "")-2)//2)
    except Exception as e: result["errors"].append({"stage":"code","error":str(e)})

    # Probe a few active tokens only. This keeps Actions fast.
    def rank(item):
        d=item[1]; f=((d.get("analysis") or {}).get("flow") or {}).get("1h") or {}
        return float(f.get("buy_count",0))+float(f.get("sell_count",0))
    probe_tokens=sorted(tokens,key=rank,reverse=True)[:3]
    print(f"LIQUIDITY V3 | tokens={len(tokens)} probe_tokens={len(probe_tokens)}")
    print(f"Pool={POOL} code_bytes={result.get('contract_code_bytes','?')}")

    found=None
    def discover(a):
        for sig in READ_SIGNATURES:
            hit = probe(a, sig)
            if hit:
                return sig, hit
        return None
    with ThreadPoolExecutor(max_workers=3) as ex:
        fs={ex.submit(discover,a):(a,d) for a,d in probe_tokens}
        for f in as_completed(fs):
            a,d=fs[f]
            try: hit=f.result()
            except Exception as e: hit=None; result["errors"].append({"stage":"probe","token":a,"error":str(e)})
            print(f"Probe {label(a,d)}: {hit[0] if hit else 'NO READ QUOTE'}")
            if hit and not found: found=hit[0]

    result["quote_method"]=found
    if found:
        result["status"]="QUOTE_FOUND"
        print("QUOTE METHOD FOUND:",found)
        def one(item):
            a,d=item; hit=probe(a,found); e={"symbol":label(a,d),"token_address":a,"source":None,"buy_50_sda":None,"estimated_price_impact_pct":None,"status":"UNKNOWN"}
            if hit:
                out=Decimal(hit["raw_output"])/Decimal(10**decimals(d)); e.update({"source":"official_pool_eth_call","buy_50_sda":float(out),"quote_probe":hit,"status":"OK"})
                try:
                    spot=Decimal(str((d.get("analysis") or {}).get("price_in_sda"))); ideal=TRADE*(1-FEE)/spot
                    if spot>0: e["estimated_price_impact_pct"]=float(max(Decimal(0),(1-out/ideal)*100))
                except: pass
            return a,e
        with ThreadPoolExecutor(max_workers=8) as ex:
            for f in as_completed([ex.submit(one,x) for x in tokens]):
                a,e=f.result(); result["tokens"][a]=e
    else:
        for a,d in tokens:
            result["tokens"][a]={"symbol":label(a,d),"token_address":a,"source":None,"buy_50_sda":None,"estimated_price_impact_pct":None,"status":"UNKNOWN"}
        print("NO READ-ONLY QUOTE FOUND; liquidity remains UNKNOWN")

    result["elapsed_seconds"]=round(time.time()-started,2)
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
    ok=sum(1 for x in result["tokens"].values() if x.get("status")=="OK")
    print(f"Finished in {result['elapsed_seconds']}s | quote={found or 'none'} | valid={ok}/{len(tokens)}")

if __name__=="__main__": main()
