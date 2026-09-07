import json
from datetime import datetime, timezone
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
TRADE_WEI = int(TRADE_SIZE_SDA * 10**18)
TIMEOUT = 10
MAX_TXS = 50
SWAP_SELECTOR = "0x8ab5246f"
SELL_SELECTOR = "0x75b5c5d8"
FEE_RATE = 0.01  # 50 SDA is below the official <300 SDA tier.

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/6.0"})


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


def rpc(method, params):
    r = session.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def explorer_transactions():
    url = f"{EXPLORER_API}/addresses/{POOL}/transactions"
    r = session.get(url, params={"filter": "to", "items_count": MAX_TXS}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("items", [])


def discover_pool_calls():
    try:
        items = explorer_transactions()
        api_error = None
    except Exception as e:
        items = []
        api_error = str(e)

    calls = []
    counts = {}
    for tx in items[:MAX_TXS]:
        to = addr_hash(tx.get("to"))
        if to and to.lower() != POOL.lower():
            continue
        raw = tx.get("raw_input") or tx.get("rawInput") or ""
        sel = selector(raw)
        if sel:
            counts[sel] = counts.get(sel, 0) + 1
        calls.append({
            "hash": tx.get("hash"),
            "block_number": tx.get("block_number"),
            "timestamp": tx.get("timestamp"),
            "status": tx.get("status"),
            "method": tx.get("method") or "",
            "selector": sel,
            "from": addr_hash(tx.get("from")),
            "to": to,
            "value": tx.get("value", "0"),
            "raw_input": raw,
            "decoded_input": tx.get("decoded_input"),
        })
    return calls, counts, api_error


def word_hex(value):
    return f"{int(value):064x}"


def encode_call(token, a, b):
    return SWAP_SELECTOR + token.lower().replace("0x", "").rjust(64, "0") + word_hex(a) + word_hex(b)


def decode_words(result):
    if not isinstance(result, str) or not result.startswith("0x"):
        return []
    body = result[2:]
    if len(body) % 64:
        return []
    return [int(body[i:i+64], 16) for i in range(0, len(body), 64)]


def try_eth_call(to, data, from_addr=None, block_tag="latest"):
    call = {"to": to, "data": data}
    if from_addr:
        call["from"] = from_addr
    try:
        result = rpc("eth_call", [call, block_tag])
        words = decode_words(result)
        return {"ok": True, "result": result, "words": words, "error": None}
    except Exception as e:
        return {"ok": False, "result": None, "words": [], "error": str(e)}


def token_decimals(token, meta):
    entry = meta.get(token.lower(), {}) if isinstance(meta, dict) else {}
    for key in ("decimals", "token_decimals"):
        try:
            if entry.get(key) is not None:
                return int(entry[key])
        except Exception:
            pass
    try:
        raw = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        words = decode_words(raw)
        if words:
            return int(words[0])
    except Exception:
        pass
    return 18


def current_quote_for_token(token, decimals, probe_from=None):
    # The real pool call has three ABI words after selector:
    # address token + uint256 + uint256.  We do not assume which uint is
    # amountIn/minOut.  V6 tests the three safe permutations with minOut=0.
    candidates = [
        (TRADE_WEI, 0, "amountIn_second"),
        (0, TRADE_WEI, "amountIn_third"),
        (TRADE_WEI, TRADE_WEI, "both_trade_size"),
    ]
    attempts = []
    for a, b, label in candidates:
        data = encode_call(token, a, b)
        res = try_eth_call(POOL, data, probe_from, "latest")
        attempt = {
            "variant": label,
            "arg1": a,
            "arg2": b,
            "calldata": data,
            **res,
        }
        attempts.append(attempt)
        if res["ok"] and res["words"]:
            # Prefer a positive word that fits a plausible token amount.
            positives = [w for w in res["words"] if w > 0]
            if positives:
                amount_raw = positives[-1]
                amount_tokens = amount_raw / (10 ** decimals)
                if amount_tokens > 0:
                    return {
                        "status": "OK",
                        "variant": label,
                        "amount_out_raw": amount_raw,
                        "amount_out_tokens": amount_tokens,
                        "raw_result": res["result"],
                        "words": res["words"],
                        "attempts": attempts,
                    }
    return {"status": "UNKNOWN", "attempts": attempts}


def historical_selector_analysis(calls):
    samples = [c for c in calls if c.get("selector") == SWAP_SELECTOR and c.get("raw_input")]
    decoded = []
    for c in samples[:10]:
        raw = c["raw_input"]
        body = raw[10:]
        if len(body) >= 192:
            words = [int(body[i:i+64], 16) for i in range(0, 192, 64)]
            token = "0x" + body[24:64]
            decoded.append({
                "tx": c.get("hash"),
                "block": c.get("block_number"),
                "from": c.get("from"),
                "token": token,
                "arg1": words[1],
                "arg2": words[2],
            })
    return decoded


def main():
    print("💧 LIQUIDITY V6 — REAL 0x8ab5246f ETH_CALL QUOTE")
    print(f"Pool: {POOL}")
    print(f"Trade size: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / eth_call only / no transaction is broadcast")

    market = load_json(MARKET_FILE, {})
    meta = load_json(META_FILE, {})
    calls, selector_counts, api_error = discover_pool_calls()
    historical = historical_selector_analysis(calls)

    discovery = {
        "updated_at": now(),
        "version": "V6",
        "pool": POOL,
        "wsda": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "api_error": api_error,
        "transactions_seen": len(calls),
        "selector_counts": selector_counts,
        "historical_8ab5246f": historical,
    }

    print(f"Pool calls discovered: {len(calls)}")
    print("Selector counts:")
    for s, n in sorted(selector_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {s}: {n}")
    print(f"Historical 0x8ab5246f samples: {len(historical)}")

    liquidity = {
        "updated_at": now(),
        "pool_address": POOL,
        "wsda_address": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "QUOTE_PROBE",
        "selector": SWAP_SELECTOR,
        "tokens": {},
        "errors": [],
    }

    # Only probe active tokens to keep Actions fast.
    active = []
    for address, item in (market.items() if isinstance(market, dict) else []):
        analysis = item.get("analysis", {}) if isinstance(item, dict) else {}
        if analysis.get("active"):
            active.append((address, item))
    if not active:
        active = list(market.items())[:10] if isinstance(market, dict) else []

    probe_from = historical[0].get("from") if historical else None
    print(f"Active token quote probes: {len(active)}")

    for address, item in active[:12]:
        token = str(address).lower()
        if not token.startswith("0x") or len(token) != 42:
            continue
        decimals = token_decimals(token, meta)
        quote = current_quote_for_token(token, decimals, probe_from)
        market_analysis = item.get("analysis", {}) if isinstance(item, dict) else {}
        spot = market_analysis.get("price_in_sda")

        out = {
            "status": quote.get("status"),
            "token_decimals": decimals,
            "liquidity_sda": None,
            "impact_50_sda_pct": None,
            "quote_tokens": quote.get("amount_out_tokens"),
            "quote_raw": quote.get("amount_out_raw"),
            "quote_variant": quote.get("variant"),
            "spot_price_sda": spot,
            "attempts": quote.get("attempts", []),
        }

        # A positive eth_call return is a quote candidate. Compare execution
        # price with scanner spot price. We deliberately do NOT invent a
        # V3 reserve/liquidity value from this impact.
        if quote.get("status") == "OK" and quote.get("amount_out_tokens") and spot:
            effective_price = TRADE_SIZE_SDA / quote["amount_out_tokens"]
            impact = ((effective_price / float(spot)) - 1.0) * 100.0
            out["effective_price_sda"] = effective_price
            out["impact_50_sda_pct"] = impact
            out["status"] = "QUOTED"
            print(
                f"  {token}: QUOTE {quote['amount_out_tokens']:.8g} tokens | "
                f"exec {effective_price:.10g} SDA | impact {impact:+.3f}%"
            )
        else:
            print(f"  {token}: UNKNOWN (eth_call did not return a usable quote)")

        liquidity["tokens"][token] = out

    liquidity["status"] = "QUOTED" if any(
        v.get("status") == "QUOTED" for v in liquidity["tokens"].values()
    ) else "UNKNOWN"

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(liquidity, f, indent=2, ensure_ascii=False)

    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {OUT_FILE}")
    print(f"Overall liquidity status: {liquidity['status']}")


if __name__ == "__main__":
    main()
