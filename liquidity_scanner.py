import json
from datetime import datetime, timezone
import requests

RPC = "https://node.sidrachain.com"
EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET_FILE = "market_data.json"
OUT_FILE = "liquidity_data.json"
DISCOVERY_FILE = "sidra_swap_discovery.json"
TRADE_SIZE_SDA = 50.0
TIMEOUT = 10
MAX_TXS = 100

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/1.0"})


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


def explorer_address_transactions():
    # The documented Blockscout v2 endpoint is the address-scoped endpoint.
    # The previous V4 used /transactions?to=..., which can silently ignore
    # the filter. This version asks Blockscout directly for this pool's txs.
    url = f"{EXPLORER_API}/addresses/{POOL}/transactions"
    params = {"filter": "to", "items_count": 50}
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("items", []), data.get("next_page_params")


def get_selector(raw_input):
    if isinstance(raw_input, str) and raw_input.startswith("0x") and len(raw_input) >= 10:
        return raw_input[:10].lower()
    return None


def discover():
    try:
        items, next_page = explorer_address_transactions()
        api_error = None
    except Exception as e:
        items, next_page = [], None
        api_error = str(e)

    candidates = []
    calls = []
    selector_counts = {}

    for tx in items[:MAX_TXS]:
        to = addr_hash(tx.get("to"))
        raw = tx.get("raw_input") or tx.get("rawInput") or ""
        selector = get_selector(raw)
        method = str(tx.get("method") or "")
        status = str(tx.get("status") or "")

        # Address endpoint can contain transfers involving the pool. Keep only
        # top-level transactions whose destination is the pool.
        if to and to.lower() != POOL.lower():
            continue

        if selector:
            selector_counts[selector] = selector_counts.get(selector, 0) + 1

        item = {
            "hash": tx.get("hash"),
            "block_number": tx.get("block_number"),
            "timestamp": tx.get("timestamp"),
            "status": status,
            "method": method,
            "selector": selector,
            "from": addr_hash(tx.get("from")),
            "to": to,
            "value": tx.get("value", "0"),
            "raw_input": raw,
            "decoded_input": tx.get("decoded_input"),
            "result": tx.get("result"),
            "revert_reason": tx.get("revert_reason"),
        }
        calls.append(item)

        # Prefer a decoded swap method, then any successful non-transfer call.
        interesting = (
            "sidraBuyWithFee" in method
            or "sidraSellWithFee" in method
            or method.lower().startswith("sidra")
        )
        if interesting or (selector and method not in ("", "transfer", "transferFrom")):
            candidates.append(item)

    # If Blockscout doesn't decode the method, print selector frequency and
    # retain the raw calls so the next version can identify the swap selector.
    result = {
        "updated_at": now(),
        "pool": POOL,
        "wsda": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "api_error": api_error,
        "transactions_seen": len(items),
        "pool_calls": len(calls),
        "selector_counts": selector_counts,
        "candidates": candidates[:20],
        "recent_pool_calls": calls[:30],
        "next_page_params": next_page,
    }
    return result


def build_unknown_liquidity(market):
    tokens = market if isinstance(market, dict) else {}
    return {
        "updated_at": now(),
        "pool_address": POOL,
        "wsda_address": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "status": "DISCOVERY_ONLY",
        "tokens": {
            addr.lower(): {
                "status": "UNKNOWN",
                "liquidity_sda": None,
                "impact_50_sda_pct": None,
            }
            for addr in tokens.keys()
        },
        "errors": [],
    }


def main():
    print("💧 LIQUIDITY V5 — CORRECT POOL TRANSACTION DISCOVERY")
    print(f"Pool: {POOL}")
    print(f"Trade size: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / no transaction is broadcast")

    market = load_json(MARKET_FILE, {})
    discovery = discover()

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)

    print(f"Explorer transactions seen: {discovery['transactions_seen']}")
    print(f"Pool-directed calls: {discovery['pool_calls']}")
    if discovery["api_error"]:
        print(f"Explorer API ERROR: {discovery['api_error']}")

    print("Selector counts:")
    for selector, count in sorted(discovery["selector_counts"].items(), key=lambda x: -x[1])[:15]:
        print(f"  {selector}: {count}")

    if discovery["candidates"]:
        print(f"Decoded/interesting candidates: {len(discovery['candidates'])}")
        for c in discovery["candidates"][:8]:
            print(
                f"  {c.get('method') or '<undecoded>'} | selector={c.get('selector')} | "
                f"value={c.get('value')} | status={c.get('status')} | tx={c.get('hash')}"
            )
            if c.get("decoded_input"):
                print(f"    decoded: {c['decoded_input']}")
            if c.get("raw_input"):
                print(f"    calldata: {c['raw_input'][:202]}")
    else:
        print("No decoded Sidra swap method yet. Raw selectors were saved for reverse engineering.")

    liquidity = build_unknown_liquidity(market)
    if discovery["candidates"]:
        c = discovery["candidates"][0]
        liquidity["discovery"] = {
            "method": c.get("method"),
            "selector": c.get("selector"),
            "tx_hash": c.get("hash"),
        }
    else:
        liquidity["discovery"] = {"method": None, "selector": None, "tx_hash": None}

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(liquidity, f, indent=2, ensure_ascii=False)

    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {OUT_FILE}")
    print("Next step: identify the real swap selector/calldata, then simulate with eth_call.")


if __name__ == "__main__":
    main()
