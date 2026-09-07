import json,time
from decimal import Decimal,getcontext
from pathlib import Path
import requests
from Crypto.Hash import keccak
getcontext().prec=60
RPC="https://node.sidrachain.com"
POOL="0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA="0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET=Path("market_data.json"); OUT=Path("liquidity_data.json")
TRADE=Decimal("50"); FEE=Decimal("0.01"); ZERO="0x0000000000000000000000000000000000000001"
def sel(s):
 h=keccak.new(digest_bits=256); h.update(s.encode()); return "0x"+h.hexdigest()[:8]
def wa(a): return a.lower().replace("0x","").rjust(64,"0")
def wu(n): return int(n).to_bytes(32,"big").hex()
def call(data,value=None):
 tx={"to":POOL,"data":data}
 if value is not None: tx["value"]=hex(int(value))
 if value is not None: tx["from"]=ZERO
 r=requests.post(RPC,json={"jsonrpc":"2.0","id":1,"method":"eth_call","params":[tx,"latest"]},timeout=30); r.raise_for_status(); return r.json()
def uint(x):
 try: return int(x,16) if x and x!="0x" else None
 except: return None
def tokens():
 d=json.loads(MARKET.read_text()); d=d.get("tokens",d); return [(a.lower(),v) for a,v in d.items() if isinstance(a,str) and len(a)==42 and isinstance(v,dict)]
def label(a,d): return d.get("symbol") or d.get("name") or (d.get("analysis") or {}).get("symbol") or a[:6]+"..."+a[-4:]
# Broad ABI probes. Successful calls are persisted; no router balances are treated as reserves.
QUOTE=["quoteBuy(address,uint256)","quoteBuy(address,uint256,uint256)","getBuyQuote(address,uint256)","getBuyQuote(address,uint256,uint256)","getAmountOut(address,uint256)","getAmountOut(uint256,address)","sidraQuoteBuy(address,uint256)","sidraQuoteBuy(address,uint256,uint256)"]
BUY=["sidraBuyWithFee(address,uint256)","sidraBuyWithFee(address,uint256,address)","sidraBuyWithFee(address,uint256,uint256)","sidraBuyWithFee(address,uint256,uint256,address)","sidraBuyWithFee(uint256,address)","sidraBuyWithFee(uint256,address,uint256)","sidraBuyWithFee(uint256,address,uint256,address)"]
def probe(a):
 amt=int(TRADE*Decimal(10)**18); aw=wu(amt); tw=wa(a)
 for s in QUOTE:
  nargs=s.split("(",1)[1].split(")",1)[0].split(",")
  opts=[]
  if nargs==["address","uint256"]: opts=[[tw,aw],[aw,tw]] if s.startswith("getAmountOut") else [[tw,aw]]
  elif len(nargs)==3: opts=[[tw,aw,wu(1)],[wa(WSDA),tw,aw],[tw,wa(WSDA),aw]]
  for args in opts:
   try:
    r=call(sel(s)+"".join(args))
    if "error" not in r and (v:=uint(r.get("result"))) is not None and v>0: return {"kind":"quote_view","signature":s,"raw_output":v}
   except: pass
 for s in BUY:
  n=len(s.split("(",1)[1].split(")",1)[0].split(",")); opts=[]
  if n==2: opts=[[tw,aw],[aw,tw]]
  elif n==3: opts=[[tw,aw,wa(ZERO)],[aw,tw,wu(1)]]
  elif n==4: opts=[[tw,aw,wu(1),wa(ZERO)],[aw,tw,wu(1),wa(ZERO)]]
  for args in opts:
   try:
    r=call(sel(s)+"".join(args),amt)
    if "error" not in r: return {"kind":"buy_eth_call_success","signature":s,"raw_output":uint(r.get("result")),"raw_result":r.get("result")}
   except: pass
 return None
def main():
 ts=tokens(); res={"updated_at":int(time.time()),"rpc":RPC,"swap_v3_pool":POOL,"wsda_address":WSDA,"trade_size_sda":50.0,"platform_fee_rate":.01,"method":"sidra_dex_abi_probe","status":"probe","warning":"Probes the current official Swap V3 Pool. Only a successful on-chain quote is used; no router balance is treated as V3 reserve.","tokens":{},"probe_hits":[],"errors":[]}
 try:
  c=call("0x"); res["contract_code_bytes"]=max(0,(len(c.get("result",""))-2)//2)
 except Exception as e: res["errors"].append({"stage":"contract_code","error":str(e)})
 for a,d in ts:
  e={"symbol":label(a,d),"token_address":a,"pool_contract":POOL,"buy_50_sda":None,"estimated_price_impact_pct":None,"source":None,"quote_probe":None}
  h=probe(a)
  if h:
   e["quote_probe"]=h; raw=h.get("raw_output")
   if raw:
    dec=int(d.get("decimals") or 18); out=Decimal(raw)/Decimal(10**dec); e["buy_50_sda"]=float(out)
    p=(d.get("analysis") or {}).get("price_in_sda")
    try:
     p=Decimal(str(p)); ideal=TRADE*(1-FEE)/p
     e["estimated_price_impact_pct"]=float(max(Decimal(0),(1-out/ideal)*100)) if p>0 else None
    except: pass
   e["source"]=h["kind"]; res["probe_hits"].append({"token":a,"symbol":e["symbol"],**h})
  res["tokens"][a]=e
 OUT.write_text(json.dumps(res,indent=2),encoding="utf-8")
 print(f"Sidra ABI probe: {len(res['probe_hits'])}/{len(ts)} numeric/successful quote probes")
 print(f"Contract code bytes: {res.get('contract_code_bytes','unknown')}")
 for x in res["probe_hits"][:20]: print("HIT",x["symbol"],x["signature"],x["kind"],x.get("raw_output"))
if __name__=="__main__": main()
