import json
import time
from decimal import Decimal, getcontext
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from Crypto.Hash import keccak

getcontext().prec = 60

RPC = "https://node.sidrachain.com"
BLOCKSCOUT = "https://ledger.sidrachain.com/api/v2"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET = Path("market_data.json")
OUT = Path("liquidity_data.json")
TRADE_SDA = Decimal("50")
FEE = Decimal("0.01")
RPC_TIMEOUT = 7
PROBE_TOKEN_COUNT = 3
ZERO = "0x0000000000000000000000000000000000000001"

# Diagnostic only. Never executes a swap and never treats router balances as V3 reserves.
QUOTE_SIGNATURES = [
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

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


def selector(signature):
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return h.hexdigest()[:8]


def word_address(address):
    return address.lower().replace("0x", "").rjust(64, "0")


def word_uint(value):
    return int(value).to_bytes(32, "big").hex()


def rpc(method, params, timeout=RPC_TIMEOUT):
    r = session.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def eth_call(data):
    return rpc("eth_call", [{"to": POOL, "data": data}, "latest"])


def uint_result(result):
    if not result or result == "0x":
        return None
    try:
        return int(result, 16)
    except Exception:
        return None


def load_tokens():
    if not MARKET.exists():
        return []
    raw = json.loads(MARKET.read_text(encoding="utf-8"))
    data = raw.get("tokens", raw)
    return [(a.lower(), v) for a, v in data.items()
            if isinstance(a, str) and len(a) == 42 and a.startswith("0x")
            and isinstance(v, dict) and a.lower() != WSDA.lower()]


def token_label(address, data):
    return data.get("symbol") or data.get("name") or (data.get("analysis") or {}).get("symbol") or f"{address[:6]}...{address[-4:]}"


def token_decimals(data):
    try:
        d = int(data.get("decimals", 18))
        return d if 0 <= d <= 36 else 18
    except Exception:
        return 18


def encode_args(signature, token, amount):
    args = signature.split("(", 1)[1].split(")", 1)[0]
    types = [] if not args else args.split(",")
    if types == ["address", "uint256"]:
        return word_address(token) + word_uint(amount)
    if types == ["uint256", "address"]:
        return word_uint(amount) + word_address(token)
    if types == ["address", "uint256", "uint256"]:
        return word_address(token) + word_uint(amount) + word_uint(1)
    if types == ["uint256", "address", "uint256"]:
        return word_uint(amount) + word_address(token) + word_uint(1)
    return None


def probe_signature(token, signature):
    amount = int(TRADE_SDA * (10 ** 18))
    encoded = encode_args(signature, token, amount)
    if encoded is None:
        return None
    try:
        result = eth_call("0x" + selector(signature) + encoded)
        value = uint_result(result)
        if value is not None and value > 0:
            return {"signature": signature, "selector": "0x" + selector(signature), "raw_output": value}
    except Exception:
        pass
    return None


def probe_token(token):
    for signature in QUOTE_SIGNATURES:
        hit = probe_signature(token, signature)
        if hit:
            return hit
    return None


def try_blockscout_contract():
    try:
        r = session.get(f"{BLOCKSCOUT}/smart-contracts/{POOL}/", timeout=8)
        if r.ok:
            data = r.json()
            abi = data.get("abi")
            if abi:
                return {"verified": True, "abi_functions": [x.get("name") for x in abi if isinstance(x, dict) and x.get("type") == "function"]}
            return {"verified": False}
        return {"http_status": r.status_code}
    except Exception as e:
        return {"error": str(e)}


def main():
    started = time.time()
    tokens = load_tokens()
    result = {
        "updated_at": int(time.time()),
        "rpc": RPC,
        "swap_v3_pool": POOL,
        "wsda_address": WSDA,
        "trade_size_sda": 50.0,
        "platform_fee_rate": 0.01,
        "method": "sidra_dex_fast_quote_probe_v2",
        "status": "diagnostic",
        "warning": "No router balance is treated as V3 liquidity. No state-changing swap method is executed. Only successful eth_call quote probes are accepted.",
        "contract_code_bytes": None,
        "blockscout": None,
        "probe_tokens": [],
        "quote_method": None,
        "tokens": {},
        "probe_hits": [],
        "errors": [],
    }

    try:
        code = rpc("eth_getCode", [POOL, "latest"])
        result["contract_code_bytes"] = max(0, (len(code or "") - 2) // 2)
    except Exception as e:
        result["errors"].append({"stage": "eth_getCode", "error": str(e)})

    result["blockscout"] = try_blockscout_contract()

    def rank(item):
        _, data = item
        analysis = data.get("analysis") or {}
        flow = (analysis.get("flow") or {}).get("1h") or {}
        trades = float(flow.get("buy_count", 0)) + float(flow.get("sell_count", 0))
        return trades

    probe_tokens = sorted(tokens, key=rank, reverse=True)[:PROBE_TOKEN_COUNT]
    result["probe_tokens"] = [{"address": a, "symbol": token_label(a, d)} for a, d in probe_tokens]

    print(f"Sidra liquidity v2: {len(tokens)} tokens loaded; probing {len(probe_tokens)} tokens only")
    print(f"Pool: {POOL}")
    print(f"Contract code bytes: {result['contract_code_bytes']}")
    print(f"Blockscout ABI: {result['blockscout']}")

    hits = []
    with ThreadPoolExecutor(max_workers=min(6, len(probe_tokens) or 1)) as executor:
        futures = {executor.submit(probe_token, address): (address, data) for address, data in probe_tokens}
        for future in as_completed(futures):
            address, data = futures[future]
            symbol = token_label(address, data)
            try:
                hit = future.result()
            except Exception as e:
                hit = None
                result["errors"].append({"stage": "probe", "token": address, "error": str(e)})
            print(f"Probe {symbol}: {'HIT ' + hit['signature'] if hit else 'no quote hit'}")
            if hit:
                result["quote_method"] = hit["signature"]
                hits.append((address, data, hit))

    chosen = result["quote_method"]

    if chosen:
        print(f"Using discovered quote method: {chosen}")
        def quote_one(item):
            address, data = item
            return address, data, probe_signature(address, chosen)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(quote_one, item) for item in tokens]
            for future in as_completed(futures):
                address, data, hit = future.result()
                entry = {
                    "symbol": token_label(address, data), "token_address": address,
                    "pool_contract": POOL, "buy_50_sda": None,
                    "estimated_price_impact_pct": None, "source": None, "quote_probe": None,
                }
                if hit:
                    decimals = token_decimals(data)
                    out_amount = Decimal(hit["raw_output"]) / Decimal(10 ** decimals)
                    entry["buy_50_sda"] = float(out_amount)
                    entry["source"] = "official_pool_eth_call"
                    entry["quote_probe"] = hit
                    try:
                        spot = Decimal(str((data.get("analysis") or {}).get("price_in_sda")))
                        ideal = TRADE_SDA * (Decimal(1) - FEE) / spot
                        if spot > 0 and ideal > 0:
                            entry["estimated_price_impact_pct"] = float(max(Decimal(0), (Decimal(1) - out_amount / ideal) * 100))
                    except Exception:
                        pass
                result["tokens"][address] = entry
    else:
        for address, data in tokens:
            result["tokens"][address] = {
                "symbol": token_label(address, data), "token_address": address,
                "pool_contract": POOL, "buy_50_sda": None,
                "estimated_price_impact_pct": None, "source": None, "quote_probe": None,
            }

    result["probe_hits"] = [{"token": a, "symbol": token_label(a, d), **hit} for a, d, hit in hits]
    result["elapsed_seconds"] = round(time.time() - started, 2)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Sidra liquidity v2 finished in {result['elapsed_seconds']}s: {len(result['probe_hits'])} discovery hits")
    print(f"QUOTE METHOD FOUND: {chosen}" if chosen else "NO QUOTE METHOD FOUND")


if __name__ == "__main__":
    main()
