import json
import time
from decimal import Decimal, getcontext
from pathlib import Path

import requests

getcontext().prec = 60

RPC_URL = "https://node.sidrachain.com"
FACTORY_ADDRESS = "0xCFE41fb5dA87916D84E7F22889087b4Ff7163cDE"
WSDA_ADDRESS = "0xE4095a910209D7BE03B55D02F40d4554B1666182"

MARKET_FILE = Path("market_data.json")
OUTPUT_FILE = Path("liquidity_data.json")

# Standard paper/live-sized trade for this bot.
TRADE_SDA = Decimal("50")

# Sidra DEX platform fee for < 300 SDA.
PLATFORM_FEE_RATE = Decimal("0.01")

# Uniswap-V3-style pool fee tiers used by the public Sidra DEX factory frontend.
POOL_FEES = (500, 3000, 10000)

# Selectors / ABI fragments.
GET_POOL_SELECTOR = ""  # encoded below with a tiny ABI-free encoder
TOKEN0_SELECTOR = "0x0dfe1681"
TOKEN1_SELECTOR = "0xd21220a7"
SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
DECIMALS_SELECTOR = "0x313ce567"


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
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected RPC batch response: {data}")
    return {int(x["id"]): x for x in data}


def rpc_call(to, data):
    r = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def pad_address(address):
    return address.lower().replace("0x", "").rjust(64, "0")


def encode_get_pool(token_a, token_b, fee):
    # getPool(address,address,uint24)
    selector = "0x1698ee82"
    return selector + pad_address(token_a) + pad_address(token_b) + f"{fee:064x}"


def decode_address(result):
    if not result or result == "0x":
        return None
    return "0x" + result[-40:]


def decode_uint(result):
    if not result or result == "0x":
        return None
    return int(result, 16)


def decode_slot0(result):
    if not result or result == "0x":
        return None
    raw = result[2:]
    # ABI words: sqrtPriceX96, tick, observationIndex, observationCardinality,
    # observationCardinalityNext, feeProtocol, unlocked.
    if len(raw) < 64 * 7:
        return None
    sqrt_price_x96 = int(raw[0:64], 16)
    tick_word = int(raw[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick}


def normalize_address(address):
    if not isinstance(address, str):
        return None
    if address.startswith("0x") and len(address) == 42:
        return address.lower()
    return None


def load_tokens():
    if not MARKET_FILE.exists():
        return []
    raw = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    out = []
    for key, value in raw.items():
        address = normalize_address(key)
        if address and isinstance(value, dict):
            out.append((address, value))
    return out


def token_label(address, data):
    analysis = data.get("analysis") or {}
    return (
        data.get("symbol")
        or data.get("name")
        or analysis.get("symbol")
        or address[:6] + "..." + address[-4:]
    )


def decimals(token, cache):
    if token.lower() in cache:
        return cache[token.lower()]
    value = decode_uint(rpc_call(token, DECIMALS_SELECTOR))
    if value is None:
        raise RuntimeError("decimals() returned empty result")
    cache[token.lower()] = value
    return value


def v3_reserves(sqrt_price_x96, liquidity):
    # Virtual reserves for the current active Uniswap-V3 liquidity range.
    # These are suitable for a local price-impact estimate, not total TVL.
    if not sqrt_price_x96 or not liquidity:
        return 0.0, 0.0
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    L = float(liquidity)
    if sqrt_p <= 0 or L <= 0:
        return 0.0, 0.0
    return L / sqrt_p, L * sqrt_p


def amount_out_v3(token_in_is_token0, amount_in_raw, sqrt_price_x96, liquidity, pool_fee):
    if amount_in_raw <= 0 or liquidity <= 0 or sqrt_price_x96 <= 0:
        return None
    s = Decimal(sqrt_price_x96) / (Decimal(2) ** 96)
    L = Decimal(liquidity)
    fee_mult = Decimal(1) - Decimal(pool_fee) / Decimal(1_000_000)
    dx = Decimal(str(amount_in_raw)) * fee_mult

    if token_in_is_token0:
        # zeroForOne: sqrtP' = L*sqrtP / (L + dx*sqrtP)
        s2 = (L * s) / (L + dx * s)
        out = L * (s - s2)
    else:
        # oneForZero: sqrtP' = sqrtP + dx/L
        s2 = s + dx / L
        out = L * (Decimal(1) / s - Decimal(1) / s2)

    return out if out > 0 else None


def impact_pct(token_in_is_token0, amount_in_raw, amount_out_raw, sqrt_price_x96):
    if amount_in_raw <= 0 or amount_out_raw <= 0:
        return None
    s = Decimal(sqrt_price_x96) / (Decimal(2) ** 96)
    spot_out_per_in = s * s if token_in_is_token0 else Decimal(1) / (s * s)
    exec_out_per_in = Decimal(amount_out_raw) / Decimal(amount_in_raw)
    impact = Decimal(1) - (exec_out_per_in / spot_out_per_in)
    return float(abs(impact) * Decimal(100))


def main():
    started = time.time()
    tokens = load_tokens()
    dec_cache = {}

    result = {
        "updated_at": int(time.time()),
        "rpc": RPC_URL,
        "factory_address": FACTORY_ADDRESS,
        "wsda_address": WSDA_ADDRESS,
        "trade_size_sda": float(TRADE_SDA),
        "platform_fee_rate": float(PLATFORM_FEE_RATE),
        "pool_fee_tiers": list(POOL_FEES),
        "method": "uniswap_v3_factory_pool_slot0_liquidity_estimate",
        "warning": (
            "Pool discovery uses the Sidra V3 factory and active V3 liquidity. "
            "The 50 SDA price-impact figure is an estimate over the current active "
            "tick range; exact executable quotes may differ if the swap crosses ticks."
        ),
        "tokens": {},
        "errors": [],
    }

    # Native SDA is wrapped as WSDA for the V3 pools.
    # Discover all fee-tier pools for every token in one RPC batch.
    discovery = []
    for address, data in tokens:
        for fee in POOL_FEES:
            discovery.append((address, data, fee))

    calls = [(FACTORY_ADDRESS, encode_get_pool(address, WSDA_ADDRESS, fee))
             for address, _, fee in discovery]

    try:
        responses = rpc_batch(calls)
    except Exception as exc:
        result["errors"].append({"stage": "factory_batch", "error": str(exc)})
        responses = {}

    pools_by_token = {}
    for i, (address, data, fee) in enumerate(discovery):
        response = responses.get(i)
        pool = None
        if response and "result" in response:
            pool = decode_address(response.get("result"))
            if pool and int(pool, 16) == 0:
                pool = None
        if pool:
            pools_by_token.setdefault(address, []).append((fee, pool))

    # Fetch V3 state for every discovered pool in one batch.
    pool_items = []
    pool_calls = []
    for address, pools in pools_by_token.items():
        for fee, pool in pools:
            pool_items.append((address, fee, pool))
            pool_calls.extend([
                (pool, TOKEN0_SELECTOR),
                (pool, TOKEN1_SELECTOR),
                (pool, SLOT0_SELECTOR),
                (pool, LIQUIDITY_SELECTOR),
            ])

    try:
        pool_responses = rpc_batch(pool_calls) if pool_calls else {}
    except Exception as exc:
        result["errors"].append({"stage": "pool_batch", "error": str(exc)})
        pool_responses = {}

    pool_state = {}
    for i, (address, fee, pool) in enumerate(pool_items):
        base = i * 4
        try:
            r0 = pool_responses[base]["result"]
            r1 = pool_responses[base + 1]["result"]
            r2 = pool_responses[base + 2]["result"]
            r3 = pool_responses[base + 3]["result"]
            token0 = decode_address(r0)
            token1 = decode_address(r1)
            slot0 = decode_slot0(r2)
            liq = decode_uint(r3)
            if not token0 or not token1 or not slot0 or not liq:
                continue
            pool_state[(address, fee)] = {
                "pool": pool,
                "fee": fee,
                "token0": token0.lower(),
                "token1": token1.lower(),
                "sqrtPriceX96": slot0["sqrtPriceX96"],
                "tick": slot0["tick"],
                "liquidity": liq,
            }
        except Exception:
            continue

    for address, data in tokens:
        label = token_label(address, data)
        candidates = []
        for fee, pool in pools_by_token.get(address, []):
            state = pool_state.get((address, fee))
            if not state:
                continue
            if WSDA_ADDRESS.lower() not in (state["token0"], state["token1"]):
                continue
            if address.lower() not in (state["token0"], state["token1"]):
                continue
            candidates.append(state)

        if not candidates:
            result["tokens"][address] = {
                "symbol": label,
                "pool_found": False,
                "liquidity_sda_equivalent": None,
                "buy_50_sda": None,
                "source": "Sidra V3 factory getPool(address,address,uint24)",
            }
            continue

        # Same selection strategy as the public Sidra V3 frontend: highest active liquidity.
        best = max(candidates, key=lambda x: x["liquidity"])
        d0 = decimals(best["token0"], dec_cache)
        d1 = decimals(best["token1"], dec_cache)
        reserve0_raw, reserve1_raw = v3_reserves(best["sqrtPriceX96"], best["liquidity"])
        reserve0 = reserve0_raw / (10 ** d0)
        reserve1 = reserve1_raw / (10 ** d1)

        token_is_0 = address.lower() == best["token0"]
        wsda_is_0 = WSDA_ADDRESS.lower() == best["token0"]
        wsda_decimals = d0 if wsda_is_0 else d1
        token_decimals = d0 if token_is_0 else d1
        wsda_virtual_reserve = reserve0 if wsda_is_0 else reserve1
        token_virtual_reserve = reserve0 if token_is_0 else reserve1

        # 50 SDA notional, then 1% platform fee, then pool fee inside the V3 curve.
        amount_after_platform = TRADE_SDA * (Decimal(1) - PLATFORM_FEE_RATE)
        amount_in_raw = amount_after_platform * (Decimal(10) ** wsda_decimals)
        out_raw = amount_out_v3(
            token_in_is_token0=wsda_is_0,
            amount_in_raw=amount_in_raw,
            sqrt_price_x96=best["sqrtPriceX96"],
            liquidity=best["liquidity"],
            pool_fee=best["fee"],
        )

        buy = None
        if out_raw:
            out_token = out_raw / (Decimal(10) ** token_decimals)
            impact = impact_pct(
                token_in_is_token0=wsda_is_0,
                amount_in_raw=amount_in_raw,
                amount_out_raw=out_raw,
                sqrt_price_x96=best["sqrtPriceX96"],
            )
            buy = {
                "amount_sda_in": float(TRADE_SDA),
                "amount_sda_after_platform_fee": float(amount_after_platform),
                "estimated_token_out": float(out_token),
                "estimated_price_impact_pct": impact,
                "pool_fee_pct": best["fee"] / 10000,
            }

        result["tokens"][address] = {
            "symbol": label,
            "pool_found": True,
            "pool_address": best["pool"],
            "pool_fee_tier": best["fee"],
            "token0": best["token0"],
            "token1": best["token1"],
            "tick": best["tick"],
            "active_liquidity": float(best["liquidity"]),
            "virtual_reserve_wsda": float(wsda_virtual_reserve),
            "virtual_reserve_token": float(token_virtual_reserve),
            "liquidity_sda_equivalent": float(wsda_virtual_reserve),
            "buy_50_sda": buy,
            "source": "Sidra V3 factory + pool slot0/liquidity",
        }

    OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print("LIQUIDITY SCANNER V2")
    print(f"Factory: {FACTORY_ADDRESS}")
    print(f"WSDA: {WSDA_ADDRESS}")
    print(f"Tokens checked: {len(tokens)}")
    found = sum(1 for x in result["tokens"].values() if x.get("pool_found"))
    print(f"Pools found: {found}")
    print(f"Errors: {len(result['errors'])}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Runtime: {time.time() - started:.1f}s")
    for address, item in list(result["tokens"].items())[:10]:
        buy = item.get("buy_50_sda") or {}
        impact = buy.get("estimated_price_impact_pct")
        print(
            f"{item['symbol']}: "
            f"liquidity {item.get('liquidity_sda_equivalent') or 0:.4f} SDA | "
            f"50 SDA impact {impact if impact is not None else 'n/a'}% | "
            f"pool {item.get('pool_address', 'none')}"
        )


if __name__ == "__main__":
    main()
