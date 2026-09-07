import json
import time
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
TIMEOUT = 8
MAX_TXS = 100

session = requests.Session()
session.headers.update({"User-Agent": "sda-scanner/1.0"})


def now():
    return datetime.now(timezone.utc).isoformat()


def rpc(method, params):
    r = session.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def hex_int(v):
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return 0


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def explorer_transactions():
    """Try the documented Blockscout REST API in a few compatible forms."""
    urls = [
        f"{EXPLORER_API}/transactions?filter=to&to={POOL}&items_count=100",
        f"{EXPLORER_API}/transactions?filter=to&to={POOL.lower()}&items_count=100",
        f"{EXPLORER_API}/transactions?to={POOL}&items_count=100",
    ]
    last_error = None

    for url in urls:
        try:
            r = session.get(url, timeout=TIMEOUT)
            if not r.ok:
                last_error = f"{r.status_code}: {r.text[:200]}"
                continue
            data = r.json()
            items = data.get("items", [])
            if isinstance(items, list):
                return items, None
        except Exception as e:
            last_error = str(e)

    return [], last_error


def tx_detail(tx_hash):
    r = session.get(f"{EXPLORER_API}/transactions/{tx_hash}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def discover():
    items, error = explorer_transactions()

    result = {
        "updated_at": now(),
        "pool": POOL,
        "wsda": WSDA,
        "trade_size_sda": TRADE_SIZE_SDA,
        "api_error": error,
        "transactions_seen": len(items),
        "candidates": [],
    }

    for tx in items[:MAX_TXS]:
        method = str(tx.get("method") or "")
        to = str(tx.get("to", {}).get("hash", tx.get("to", ""))) if isinstance(tx.get("to"), dict) else str(tx.get("to", ""))
        tx_hash = tx.get("hash") or tx.get("tx_hash")

        if to and to.lower() != POOL.lower():
            continue

        interesting = (
            "sidraBuyWithFee" in method
            or "sidraSellWithFee" in method
            or method.startswith("0x")
        )
        if not interesting or not tx_hash:
            continue

        try:
            detail = tx_detail(tx_hash)
        except Exception as e:
            detail = {"detail_error": str(e)}

        raw_input = (
            detail.get("raw_input")
            or detail.get("rawInput")
            or tx.get("raw_input")
            or tx.get("rawInput")
            or tx.get("input")
            or ""
        )

        value = detail.get("value", tx.get("value", "0"))
        status = detail.get("status", tx.get("status"))

        candidate = {
            "hash": tx_hash,
            "method": method,
            "status": status,
            "from": (detail.get("from") or tx.get("from") or {}).get("hash")
                    if isinstance(detail.get("from") or tx.get("from"), dict)
                    else detail.get("from") or tx.get("from"),
            "to": POOL,
            "value": value,
            "raw_input": raw_input,
            "selector": raw_input[:10] if isinstance(raw_input, str) and len(raw_input) >= 10 else None,
            "decoded_input": detail.get("decoded_input") or detail.get("decodedInput") or tx.get("decoded_input"),
            "timestamp": detail.get("timestamp") or tx.get("timestamp"),
            "logs_count": len(detail.get("logs", []) or []),
        }

        # Keep successful contract calls first.
        result["candidates"].append(candidate)

        if len(result["candidates"]) >= 8:
            break

    result["candidates"].sort(
        key=lambda x: (
            0 if str(x.get("status", "")).lower() in ("ok", "success") else 1,
            0 if "sidraBuyWithFee" in str(x.get("method", "")) else 1,
        )
    )

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
    print("💧 LIQUIDITY V4 — ON-CHAIN SWAP DISCOVERY")
    print(f"Pool: {POOL}")
    print(f"Trade size: {TRADE_SIZE_SDA:.0f} SDA")
    print("Safety: READ-ONLY / no transaction is broadcast")

    market = load_json(MARKET_FILE, {})
    discovery = discover()

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(discovery, f, indent=2, ensure_ascii=False)

    print(f"Explorer transactions seen: {discovery['transactions_seen']}")
    if discovery["api_error"]:
        print(f"Explorer API warning: {discovery['api_error']}")

    if discovery["candidates"]:
        print(f"Swap candidates found: {len(discovery['candidates'])}")
        for c in discovery["candidates"][:5]:
            print(
                f"  {c.get('method')} | selector={c.get('selector')} | "
                f"value={c.get('value')} | tx={c.get('hash')}"
            )
            if c.get("decoded_input"):
                print(f"    decoded: {c['decoded_input']}")
            if c.get("raw_input"):
                print(f"    calldata: {c['raw_input'][:138]}")
    else:
        print("No sidraBuyWithFee/sidraSellWithFee transaction found yet.")

    # Do NOT invent liquidity when the exact swap ABI is unknown.
    liquidity = build_unknown_liquidity(market)
    if discovery["candidates"]:
        liquidity["discovery"] = {
            "method": discovery["candidates"][0].get("method"),
            "selector": discovery["candidates"][0].get("selector"),
            "tx_hash": discovery["candidates"][0].get("hash"),
        }
    else:
        liquidity["discovery"] = {"method": None, "selector": None, "tx_hash": None}

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(liquidity, f, indent=2, ensure_ascii=False)

    print(f"Saved {DISCOVERY_FILE}")
    print(f"Saved {OUT_FILE}")
    print("Next step: use the real selector/calldata to perform an eth_call quote.")


if __name__ == "__main__":
    main()
