import json
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
RPC = "https://node.sidrachain.com"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
SWAP_SELECTOR = "0x8ab5246f"
TRADE_SIZE_SDA = 50.0
MAX_TXS = 50
SAMPLE_COUNT = 10
TIMEOUT = 8
RPC_TIMEOUT = 12
ETH_CALL_FROM_FALLBACK = "0x0000000000000000000000000000000000000001"

DISCOVERY_FILE = "sidra_swap_discovery.json"
LIQUIDITY_FILE = "liquidity_data.json"

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/11.0"})

def now():
    return datetime.now(timezone.utc).isoformat()

def get_json(url, params=None):
    r = session.get(url, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def addr(x):
    if isinstance(x, dict):
        return x.get("hash") or x.get("address_hash") or ""
    return str(x or "")

def selector(raw):
    return raw[:10].lower() if isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 10 else ""

def decode_call(raw):
    if selector(raw) != SWAP_SELECTOR:
        return None
    body = raw[10:]
    if len(body) < 192:
        return None
    return {
        "token": "0x" + body[24:64],
        "arg1": int(body[64:128], 16),
        "arg2": int(body[128:192], 16),
    }

def transfer_value(t):
    total = t.get("total")
    if isinstance(total, dict):
        raw = total.get("value")
        decimals = total.get("decimals")
    else:
        raw = t.get("value") or t.get("amount") or t.get("raw_value")
        decimals = t.get("decimals")

    try:
        raw_int = int(raw) if raw is not None else None
    except Exception:
        raw_int = None

    try:
        dec = int(decimals) if decimals is not None else None
    except Exception:
        dec = None

    formatted = None
    if raw_int is not None and dec is not None:
        formatted = float(Decimal(raw_int) / (Decimal(10) ** dec))

    return raw_int, dec, formatted

def normalize_transfer(t):
    token = t.get("token") or {}
    raw, dec, formatted = transfer_value(t)
    return {
        "token": (token.get("address_hash") or "").lower(),
        "symbol": token.get("symbol"),
        "decimals": dec,
        "raw_value": raw,
        "value": formatted,
        "from": addr(t.get("from")).lower(),
        "to": addr(t.get("to")).lower(),
        "log_index": t.get("log_index"),
        "method": t.get("method"),
    }

def list_pool_txs():
    data = get_json(
        f"{EXPLORER_API}/addresses/{POOL}/transactions",
        {"filter": "to", "items_count": MAX_TXS},
    )
    return data.get("items", []) if isinstance(data, dict) else []

def transfers_for_tx(tx_hash):
    data = get_json(f"{EXPLORER_API}/transactions/{tx_hash}/token-transfers")
    items = data.get("items", []) if isinstance(data, dict) else []
    return [normalize_transfer(x) for x in items]


def pad_word(value):
    return f"{int(value):064x}"

def build_observed_calldata(token, fee_tier, arg2):
    # Observed live calls are exactly:
    # selector + address word + fee-tier word + uint256 word.
    return (
        SWAP_SELECTOR
        + pad_word(int(token, 16))
        + pad_word(fee_tier)
        + pad_word(arg2)
    )

def eth_call(data, from_address=None, value_wei=0):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": POOL,
                "from": from_address or ETH_CALL_FROM_FALLBACK,
                "data": data,
                "value": hex(int(value_wei)),
            },
            "latest",
        ],
    }
    r = requests.post(RPC, json=payload, timeout=RPC_TIMEOUT)
    r.raise_for_status()
    out = r.json()
    if "error" in out:
        return {"ok": False, "result": None, "error": out["error"]}
    return {"ok": True, "result": out.get("result"), "error": None}

def eth_get_balance(address):
    payload={"jsonrpc":"2.0","id":2,"method":"eth_getBalance","params":[address,"latest"]}
    r=requests.post(RPC,json=payload,timeout=RPC_TIMEOUT); r.raise_for_status()
    out=r.json()
    if "error" in out:return None,out["error"]
    try:return int(out.get("result","0x0"),16),None
    except Exception:return None,{"message":"invalid eth_getBalance result"}

def choose_funded_from(samples,min_value_wei):
    candidates=[]; seen=set()
    for x in samples:
        a=(x.get("buyer") or "").lower()
        if a and a not in seen: seen.add(a); candidates.append(a)
    balances=[]
    for a in candidates[:20]:
        try:
            bal,err=eth_get_balance(a)
            balances.append({"address":a,"balance_wei":bal,"error":err})
            if bal is not None and bal>=min_value_wei:return a,balances
        except Exception as e: balances.append({"address":a,"balance_wei":None,"error":str(e)})
    return None,balances

def decode_uint256_result(result):
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    raw = result[2:]
    if len(raw) < 64:
        return None
    try:
        return int(raw[-64:], 16)
    except Exception:
        return None

def analyze(tx):
    raw = tx.get("raw_input") or ""
    if selector(raw) != SWAP_SELECTOR:
        return None

    call = decode_call(raw)
    h = tx.get("hash") or tx.get("transaction_hash")
    transfers = transfers_for_tx(h)

    target = call["token"].lower()
    wsda = WSDA.lower()
    pool = POOL.lower()

    # For this selector the actual trader can be inferred from WSDA:
    # the address sending WSDA to the pool is the buyer.
    buyers = [t["from"] for t in transfers
              if t["token"] == wsda and t["to"] == pool and t["value"] is not None]
    buyer = buyers[0] if buyers else ""

    sda_in = sum(
        t["value"] for t in transfers
        if t["token"] == wsda and t["from"] == buyer and t["to"] == pool
        and t["value"] is not None
    )
    token_out_transfers = [
        t for t in transfers
        if t["token"] == target and t["from"] == pool and t["to"] == buyer
        and t["value"] is not None
    ]
    token_out = sum(t["value"] for t in token_out_transfers)

    token_out_raw = sum(
        int(t["raw_value"]) for t in token_out_transfers
        if t.get("raw_value") is not None
    )
    token_decimals = (
        token_out_transfers[0].get("decimals")
        if token_out_transfers else None
    )
    arg2_matches_raw_output = (
        token_out_raw > 0 and call["arg2"] == token_out_raw
    )
    arg2_difference_raw = (
        call["arg2"] - token_out_raw if token_out_raw > 0 else None
    )

    # Also collect every leg, including fee/burn legs.
    return {
        "tx": h,
        "block": tx.get("block_number"),
        "status": tx.get("status"),
        "method": tx.get("method"),
        "selector": selector(raw),
        "raw_input": raw,
        "token": call["token"],
        "arg1": call["arg1"],
        "arg2": call["arg2"],
        "buyer": buyer,
        "sda_in": sda_in,
        "token_out": token_out,
        "token_out_raw": token_out_raw,
        "token_decimals": token_decimals,
        "arg2_matches_raw_output": arg2_matches_raw_output,
        "arg2_difference_raw": arg2_difference_raw,
        "tokens_per_sda": (token_out / sda_in) if sda_in > 0 else None,
        "arg2_per_sda": (call["arg2"] / sda_in) if sda_in > 0 else None,
        "transfers": transfers,
    }

def main():
    print("💧 LIQUIDITY V11 — FUNDED ETH_CALL + QUOTE PROBE")
    print(f"Pool: {POOL}")
    print(f"Target trade: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / explorer GET only / no broadcast")

    try:
        txs = list_pool_txs()
        api_error = None
    except Exception as e:
        txs = []
        api_error = str(e)

    swap_txs = [
        t for t in txs
        if selector(t.get("raw_input") or "") == SWAP_SELECTOR
    ]

    counts = {}
    for t in txs:
        s = selector(t.get("raw_input") or "")
        if s:
            counts[s] = counts.get(s, 0) + 1

    print(f"Pool calls discovered: {len(txs)}")
    print("Selector counts:")
    for s, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")
    print(f"Real {SWAP_SELECTOR} transactions: {len(swap_txs)}")
    print(f"Analyzing {min(SAMPLE_COUNT, len(swap_txs))} samples concurrently")

    samples = []
    chosen = swap_txs[:SAMPLE_COUNT]

    with ThreadPoolExecutor(max_workers=min(5, max(1, len(chosen)))) as ex:
        futures = {ex.submit(analyze, t): t for t in chosen}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    samples.append(result)
            except Exception as e:
                t = futures[fut]
                samples.append({
                    "tx": t.get("hash"),
                    "error": str(e),
                })

    samples.sort(key=lambda x: x.get("tx", ""))

    usable = []
    for a in samples:
        print()
        print(f"TX {a.get('tx')}")
        if a.get("error"):
            print(f"  ERROR: {a['error']}")
            continue

        print(f"  token={a['token']}")
        print(f"  arg1={a['arg1']}")
        print(f"  arg2={a['arg2']}")
        print(f"  buyer={a['buyer']}")
        print(f"  WSDA user→pool={a['sda_in']}")
        print(f"  token pool→user={a['token_out']}")
        print(f"  token raw output={a['token_out_raw']}")
        print(f"  token decimals={a['token_decimals']}")
        print(f"  arg2 == raw token output: {a['arg2_matches_raw_output']}")
        print(f"  arg2 - raw output={a['arg2_difference_raw']}")
        print(f"  tokens/SDA={a['tokens_per_sda']}")
        print(f"  arg2/SDA={a['arg2_per_sda']}")

        # Show only the economically relevant transfers first.
        for t in a["transfers"]:
            if t["value"] is not None:
                print(
                    f"    {t['symbol'] or t['token']} "
                    f"{t['value']} "
                    f"{t['from'][:10]}→{t['to'][:10]}"
                )

        if a["sda_in"] > 0 and a["token_out"] > 0:
            usable.append({
                "tx": a["tx"],
                "token": a["token"],
                "arg1": a["arg1"],
                "arg2": a["arg2"],
                "sda_in": a["sda_in"],
                "token_out": a["token_out"],
                "token_out_raw": a["token_out_raw"],
                "token_decimals": a["token_decimals"],
                "arg2_matches_raw_output": a["arg2_matches_raw_output"],
                "arg2_difference_raw": a["arg2_difference_raw"],
                "tokens_per_sda": a["tokens_per_sda"],
                "arg2_per_sda": a["arg2_per_sda"],
            })

    # Determine whether arg1 behaves like a V3 fee tier from observed data.
    arg1_values = sorted({x["arg1"] for x in usable})
    fee_tier_like = all(x in (100, 500, 1000, 3000, 5000, 10000) for x in arg1_values) if arg1_values else False

    # Test whether arg2 looks like a minimum/output amount by comparing it
    # to the observed output in raw token units when available.
    ratios = []
    for x in usable:
        if x["token_out"] > 0:
            ratios.append(x["arg2"] / x["token_out"])

    exact_arg2_matches = sum(
        1 for x in usable if x.get("arg2_matches_raw_output")
    )
    arg2_match_rate = (
        exact_arg2_matches / len(usable) if usable else 0.0
    )

    # Equality with the actual transfer proves that arg2 encodes the exact
    # output observed in these historical calls. It does not, by itself,
    # distinguish exact-output from amountOutMinimum semantics.
    arg2_semantics = (
        "ARG2_EQUALS_ACTUAL_TOKEN_OUTPUT_IN_ALL_SAMPLES"
        if usable and exact_arg2_matches == len(usable)
        else "ARG2_NOT_PROVEN"
    )

    discovery = {
        "updated_at": now(),
        "version": "V11",
        "pool": POOL,
        "wsda": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "api_error": api_error,
        "selector": SWAP_SELECTOR,
        "transactions_seen": len(txs),
        "selector_counts": counts,
        "samples": samples,
        "usable_input_output_pairs": usable,
        "arg1_values": arg1_values,
        "arg1_matches_common_v3_fee_tiers": fee_tier_like,
        "arg2_to_token_output_ratios": ratios,
        "conclusion": {
            "swap_direction_observed": bool(usable),
            "arg1_likely_fee_tier": fee_tier_like,
            "quote_ready": False,
        },
    }

    liquidity = {
        "updated_at": now(),
        "version": "V11",
        "pool_address": POOL,
        "wsda_address": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "UNKNOWN",
        "tokens": {},
        "errors": [],
        "eth_call_quote": {},
    }

    # Keep observed prices per token for later calibration, but don't claim
    # that these are current executable quotes.
    for x in usable:
        e = liquidity["tokens"].setdefault(x["token"].lower(), {
            "status": "OBSERVED",
            "samples": 0,
            "observed_sda_in": 0.0,
            "observed_token_out": 0.0,
        })
        e["samples"] += 1
        e["observed_sda_in"] += x["sda_in"]
        e["observed_token_out"] += x["token_out"]

    # V11: use a currently funded sampled buyer so eth_call can supply 50 SDA.
    value_wei=int(TRADE_SIZE_SDA*10**18)
    quote_probe={"attempted":False,"ok":False,"token":None,"fee_tier":None,
        "trade_size_sda":TRADE_SIZE_SDA,"value_wei":value_wei,"arg2_tests":[],
        "from_address":None,"from_balance_wei":None,"candidate_balances":[],
        "results":[],"error":None}
    if usable:
        probe_sample=usable[0]
        quote_probe["token"]=probe_sample["token"]; quote_probe["fee_tier"]=probe_sample["arg1"]
        try:
            funded,balances=choose_funded_from(samples,value_wei)
            quote_probe["candidate_balances"]=balances; quote_probe["from_address"]=funded
            if funded:
                quote_probe["from_balance_wei"]=next((x.get("balance_wei") for x in balances if x.get("address")==funded),None)
                for label,arg2 in [("zero",0),("historical_exact_output",int(probe_sample["arg2"]))]:
                    data=build_observed_calldata(probe_sample["token"],probe_sample["arg1"],arg2)
                    result=eth_call(data,from_address=funded,value_wei=value_wei)
                    decoded=decode_uint256_result(result.get("result"))
                    item={"label":label,"arg2":arg2,"calldata":data,"ok":result.get("ok"),
                          "raw_result":result.get("result"),"decoded_uint256":decoded,
                          "decoded_token_amount":(float(Decimal(decoded)/(Decimal(10)**(probe_sample.get("token_decimals") or 18))) if decoded is not None else None),
                          "error":result.get("error")}
                    quote_probe["results"].append(item); quote_probe["attempted"]=True
                    if result.get("ok"): quote_probe["ok"]=True
            else:
                quote_probe["error"]={"message":"No sampled buyer currently has enough native SDA for a 50 SDA eth_call.","required_wei":value_wei}
        except Exception as e: quote_probe["error"]=str(e)
    quote_probe["decoded_token_amount"]=next((x["decoded_token_amount"] for x in quote_probe["results"] if x.get("decoded_token_amount") is not None),None)
    quote_probe["quote_ready"]=quote_probe["decoded_token_amount"] is not None

    discovery["eth_call_quote"] = quote_probe
    discovery["conclusion"]["quote_ready"] = (
        quote_probe.get("decoded_token_amount") is not None
    )

    liquidity["eth_call_quote"] = quote_probe

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)

    with open(LIQUIDITY_FILE, "w", encoding="utf-8") as f:
        json.dump(liquidity, f, indent=2, ensure_ascii=False)

    print()
    print(f"Usable real BUY input/output pairs: {len(usable)}")
    print(f"arg1 values observed: {arg1_values}")
    print(f"arg1 looks like common V3 fee tier: {fee_tier_like}")
    print(f"arg2 exact raw-output matches: {exact_arg2_matches}/{len(usable)} ({arg2_match_rate:.1%})")
    print(f"arg2 semantic conclusion: {arg2_semantics}")
    print(f"eth_call attempted: {quote_probe['attempted']}")
    print(f"eth_call from: {quote_probe.get('from_address')}")
    print(f"funded candidates checked: {len(quote_probe.get('candidate_balances', []))}")
    print(f"eth_call successful result: {quote_probe.get("ok")}")
    for r in quote_probe.get("results", []):
        print(f"  test={r.get('label')} arg2={r.get('arg2')} ok={r.get('ok')} decoded={r.get('decoded_token_amount')}")
        if r.get("error"): print(f"    error: {r.get('error')}")
    if quote_probe.get("error"): print(f"eth_call error: {quote_probe['error']}")
    if quote_probe.get("quote_ready"): print("Quote probe produced a uint256 return value.")
    else: print("No current executable uint256 quote obtained.")
    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {LIQUIDITY_FILE}")
    print("Overall liquidity status: UNKNOWN")
    print("Next: use the measured WSDA input + token output to pin down arg2 semantics before eth_call.")

if __name__ == "__main__":
    main()
