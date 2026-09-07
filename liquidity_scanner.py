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

DISCOVERY_FILE = "sidra_swap_discovery.json"
LIQUIDITY_FILE = "liquidity_data.json"

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/9.0"})

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
    token_out = sum(
        t["value"] for t in transfers
        if t["token"] == target and t["from"] == pool and t["to"] == buyer
        and t["value"] is not None
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
        "tokens_per_sda": (token_out / sda_in) if sda_in > 0 else None,
        "arg2_per_sda": (call["arg2"] / sda_in) if sda_in > 0 else None,
        "transfers": transfers,
    }

def main():
    print("💧 LIQUIDITY V9 — FIX TRANSFER DECODING + REAL BUY FLOW")
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

    discovery = {
        "updated_at": now(),
        "version": "V9",
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
        "version": "V9",
        "pool_address": POOL,
        "wsda_address": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "UNKNOWN",
        "tokens": {},
        "errors": [],
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

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)

    with open(LIQUIDITY_FILE, "w", encoding="utf-8") as f:
        json.dump(liquidity, f, indent=2, ensure_ascii=False)

    print()
    print(f"Usable real BUY input/output pairs: {len(usable)}")
    print(f"arg1 values observed: {arg1_values}")
    print(f"arg1 looks like common V3 fee tier: {fee_tier_like}")
    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {LIQUIDITY_FILE}")
    print("Overall liquidity status: UNKNOWN")
    print("Next: use the measured WSDA input + token output to pin down arg2 semantics before eth_call.")

if __name__ == "__main__":
    main()
