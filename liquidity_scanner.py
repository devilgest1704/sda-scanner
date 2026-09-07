import json
import time
from decimal import Decimal, getcontext
from pathlib import Path

import requests

getcontext().prec = 60

RPC_URL = "https://node.sidrachain.com"
SWAP_V3_POOL = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA_ADDRESS = "0xE4095a910209D7BE03B55D02F40d4554B1666182"
MARKET_FILE = Path("market_data.json")
OUTPUT_FILE = Path("liquidity_data.json")
TRADE_SDA = Decimal("50")
PLATFORM_FEE_RATE = Decimal("0.01")

# ERC-20 selectors
BALANCE_OF = "0x70a08231"
DECIMALS = "0x313ce567"


def pad_address(address):
    return address.lower().replace("0x", "").rjust(64, "0")


def rpc_batch(calls):
    payload = []
    for i, (to, data) in enumerate(calls):
        payload.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        })
    r = requests.post(RPC_URL, json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"Unexpected RPC response: {body}")
    return {int(x["id"]): x for x in body}


def decode_uint(result):
    if not result or result == "0x":
        return None
    try:
        return int(result, 16)
    except Exception:
        return None


def normalize_address(address):
    if isinstance(address, str) and address.startswith("0x") and len(address) == 42:
        return address.lower()
    return None


def load_tokens():
    if not MARKET_FILE.exists():
        return []
    raw = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    out = []
    for address, data in raw.items():
        address = normalize_address(address)
        if address and isinstance(data, dict):
            out.append((address, data))
    return out


def label(address, data):
    analysis = data.get("analysis") or {}
    return data.get("symbol") or data.get("name") or analysis.get("symbol") or f"{address[:6]}...{address[-4:]}"


def market_price(data):
    analysis = data.get("analysis") or {}
    try:
        p = Decimal(str(analysis.get("price_in_sda")))
        return p if p > 0 else None
    except Exception:
        return None


def main():
    tokens = load_tokens()
    result = {
        "updated_at": int(time.time()),
        "rpc": RPC_URL,
        "swap_v3_pool": SWAP_V3_POOL,
        "wsda_address": WSDA_ADDRESS,
        "trade_size_sda": float(TRADE_SDA),
        "platform_fee_rate": float(PLATFORM_FEE_RATE),
        "method": "direct_swap_v3_pool_balance_diagnostic",
        "status": "diagnostic",
        "warning": (
            "The current Sidra DEX exposes one Swap V3 Pool contract and routes buys through "
            "sidraBuyWithFee. The exact executable quote ABI is not publicly exposed here. "
            "This scanner deliberately does NOT pretend that a token balance at the router is "
            "the V3 reserve. It reports only on-chain balances and an indicative 50 SDA impact "
            "when a defensible pair-balance estimate is possible. Do not use this field as a BUY gate yet."
        ),
        "tokens": {},
        "errors": [],
    }

    # Query the current WSDA balance of the actual Swap V3 Pool contract.
    calls = [(WSDA_ADDRESS, BALANCE_OF + pad_address(SWAP_V3_POOL))]
    token_rows = []
    for address, data in tokens:
        token_rows.append((address, data))
        calls.append((address, BALANCE_OF + pad_address(SWAP_V3_POOL)))
        calls.append((address, DECIMALS))

    try:
        responses = rpc_batch(calls)
    except Exception as exc:
        result["errors"].append({"stage": "rpc_batch", "error": str(exc)})
        responses = {}

    wsda_raw = decode_uint((responses.get(0) or {}).get("result"))
    wsda_balance = None
    if wsda_raw is not None:
        wsda_balance = Decimal(wsda_raw) / (Decimal(10) ** 18)

    for i, (address, data) in enumerate(token_rows):
        bal_raw = decode_uint((responses.get(1 + i * 2) or {}).get("result"))
        dec = decode_uint((responses.get(2 + i * 2) or {}).get("result"))
        if dec is None:
            dec = 18
        token_balance = None
        if bal_raw is not None:
            token_balance = Decimal(bal_raw) / (Decimal(10) ** dec)

        price = market_price(data)
        entry = {
            "symbol": label(address, data),
            "token_address": address,
            "pool_contract": SWAP_V3_POOL,
            "pool_wsda_balance": float(wsda_balance) if wsda_balance is not None else None,
            "pool_token_balance": float(token_balance) if token_balance is not None else None,
            "token_decimals": dec,
            "market_price_sda": float(price) if price is not None else None,
            "buy_50_sda": None,
            "estimated_price_impact_pct": None,
            "source": "router_balance_only",
        }

        # Only calculate an indicative constant-product impact if BOTH sides are
        # actually held by the contract and the market price agrees with reserves.
        # This is intentionally labelled indicative; it is NOT a V3 quote.
        if wsda_balance and wsda_balance > 0 and token_balance and token_balance > 0 and price:
            implied_price = wsda_balance / token_balance
            deviation = abs(implied_price / price - Decimal(1))
            if deviation <= Decimal("0.25"):
                net_in = TRADE_SDA * (Decimal(1) - PLATFORM_FEE_RATE)
                k = wsda_balance * token_balance
                new_wsda = wsda_balance + net_in
                new_token = k / new_wsda
                out = token_balance - new_token
                spot_out = net_in / price
                impact = (Decimal(1) - (out / spot_out)) * Decimal(100)
                entry["buy_50_sda"] = float(out)
                entry["estimated_price_impact_pct"] = float(max(Decimal(0), impact))
                entry["source"] = "router_balance_indicative_cp"
                entry["reserve_price_deviation_pct"] = float(deviation * 100)

        result["tokens"][address] = entry

    # Always write a file so the GitHub workflow never fails just because the
    # DEX ABI is unavailable.
    OUTPUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")

    valid = [x for x in result["tokens"].values() if x.get("buy_50_sda") is not None]
    print(f"Liquidity diagnostic: {len(valid)}/{len(result['tokens'])} tokens have an indicative 50 SDA estimate")
    if wsda_balance is not None:
        print(f"Swap V3 Pool WSDA balance: {wsda_balance:.6f} WSDA")
    for x in sorted(valid, key=lambda z: z.get("estimated_price_impact_pct", 999))[:10]:
        print(
            f"{x['symbol']}: impact={x['estimated_price_impact_pct']:.4f}% "
            f"out={x['buy_50_sda']:.8f} source={x['source']}"
        )


if __name__ == "__main__":
    main()
