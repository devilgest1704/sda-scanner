import json
import time
from decimal import Decimal, getcontext
from pathlib import Path

import requests

getcontext().prec = 50

RPC_URL = "https://node.sidrachain.com"
POOL_ADDRESS = "0xCB94460F967f49E3a955278f252Ca9D6056ecE75"
WSDA_ADDRESS = "0xE4095a910209D7BE03B55D02F40d4554B1666182"

MARKET_FILE = Path("market_data.json")
OUTPUT_FILE = Path("liquidity_data.json")

# Our paper trade size.
TRADE_SDA = Decimal("10000")

# Sidra DEX fee schedule: >= 500 SDA => 2%.
PLATFORM_FEE_RATE = Decimal("0.02")

# ERC-20 selectors
BALANCE_OF_SELECTOR = "70a08231"
DECIMALS_SELECTOR = "313ce567"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def rpc_call(method, params):
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def pad_address(address):
    return address.lower().replace("0x", "").rjust(64, "0")


def erc20_balance(token, owner):
    data = "0x" + BALANCE_OF_SELECTOR + pad_address(owner)
    result = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    return int(result, 16)


def erc20_decimals(token):
    data = "0x" + DECIMALS_SELECTOR
    result = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    return int(result, 16)


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
    tokens = []

    # market_data.json is keyed by token address in the current scanner.
    for key, value in raw.items():
        address = normalize_address(key)
        if not address or not isinstance(value, dict):
            continue
        tokens.append((address, value))

    return tokens


def token_label(address, data):
    analysis = data.get("analysis") or {}
    return (
        data.get("symbol")
        or data.get("name")
        or analysis.get("symbol")
        or address[:6] + "..." + address[-4:]
    )


def estimate_constant_product_buy(
    reserve_sda,
    reserve_token,
    amount_sda_in,
):
    """
    Conservative fallback estimate based on a constant-product pool.

    IMPORTANT:
    The Sidra DEX identifies this contract as a "Swap V3 Pool".
    Until we have the exact quote ABI/curve implementation, this value
    is explicitly marked as an ESTIMATE, not an on-chain quote.

    We use the post-platform-fee SDA amount as the swap input.
    """
    if reserve_sda <= 0 or reserve_token <= 0 or amount_sda_in <= 0:
        return None

    amount_after_fee = amount_sda_in * (Decimal("1") - PLATFORM_FEE_RATE)

    # x*y=k, output = y * dx / (x+dx)
    token_out = reserve_token * amount_after_fee / (reserve_sda + amount_after_fee)

    spot = reserve_token / reserve_sda
    execution_price = amount_after_fee / token_out if token_out else Decimal("0")

    # Relative deviation from the starting spot price.
    impact = Decimal("1") - (execution_price / (Decimal("1") / spot))
    impact = abs(impact)

    return {
        "amount_sda_after_platform_fee": float(amount_after_fee),
        "estimated_token_out": float(token_out),
        "estimated_price_impact_pct": float(impact * 100),
    }


def estimate_constant_product_sell(
    reserve_sda,
    reserve_token,
    token_amount,
):
    if reserve_sda <= 0 or reserve_token <= 0 or token_amount <= 0:
        return None

    sda_out_before_fee = reserve_sda * token_amount / (reserve_token + token_amount)
    sda_out_after_fee = sda_out_before_fee * (Decimal("1") - PLATFORM_FEE_RATE)

    spot_sda_per_token = reserve_sda / reserve_token
    execution_price = sda_out_before_fee / token_amount

    impact = Decimal("1") - (execution_price / spot_sda_per_token)
    impact = abs(impact)

    return {
        "estimated_sda_out_before_platform_fee": float(sda_out_before_fee),
        "estimated_sda_out_after_platform_fee": float(sda_out_after_fee),
        "estimated_price_impact_pct": float(impact * 100),
    }


def main():
    started = time.time()
    tokens = load_tokens()

    result = {
        "updated_at": int(time.time()),
        "rpc": RPC_URL,
        "pool_address": POOL_ADDRESS,
        "wsda_address": WSDA_ADDRESS,
        "trade_size_sda": float(TRADE_SDA),
        "platform_fee_rate": float(PLATFORM_FEE_RATE),
        "method": "on_chain_pool_balances_constant_product_estimate",
        "warning": (
            "Liquidity is read directly from the Sidra Swap V3 Pool contract. "
            "Price impact is currently an ESTIMATE until the exact Sidra quote "
            "function/curve ABI is confirmed. Do not treat estimated impact as "
            "an executable quote."
        ),
        "tokens": {},
        "errors": [],
    }

    for address, data in tokens:
        try:
            decimals = erc20_decimals(address)
            raw_token_balance = erc20_balance(address, POOL_ADDRESS)
            raw_wsda_balance = erc20_balance(WSDA_ADDRESS, POOL_ADDRESS)

            reserve_token = Decimal(raw_token_balance) / (Decimal(10) ** decimals)
            wsda_decimals = erc20_decimals(WSDA_ADDRESS)
            reserve_sda = Decimal(raw_wsda_balance) / (Decimal(10) ** wsda_decimals)

            label = token_label(address, data)

            buy = estimate_constant_product_buy(
                reserve_sda, reserve_token, TRADE_SDA
            )

            # For a paper position opened with 10k SDA, approximate token amount
            # received and then simulate selling that exact amount.
            sell = None
            if buy and buy["estimated_token_out"] > 0:
                sell = estimate_constant_product_sell(
                    reserve_sda,
                    reserve_token,
                    Decimal(str(buy["estimated_token_out"])),
                )

            result["tokens"][address] = {
                "symbol": label,
                "decimals": decimals,
                "pool_reserve_wsda": float(reserve_sda),
                "pool_reserve_token": float(reserve_token),
                "liquidity_sda_equivalent": float(reserve_sda),
                "buy_10k_sda": buy,
                "sell_of_buy_10k": sell,
                "source": "on-chain ERC20 balanceOf(pool)",
            }

        except Exception as exc:
            result["errors"].append({
                "address": address,
                "error": str(exc),
            })

    OUTPUT_FILE.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("LIQUIDITY SCANNER")
    print(f"Pool: {POOL_ADDRESS}")
    print(f"Tokens checked: {len(tokens)}")
    print(f"Successful: {len(result['tokens'])}")
    print(f"Errors: {len(result['errors'])}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Runtime: {time.time() - started:.1f}s")

    # Show the most relevant candidates first.
    for address, item in list(result["tokens"].items())[:10]:
        buy = item.get("buy_10k_sda") or {}
        print(
            f"{item['symbol']}: "
            f"liquidity {item['liquidity_sda_equivalent']:.2f} SDA | "
            f"estimated 10k impact {buy.get('estimated_price_impact_pct', 0):.3f}%"
        )


if __name__ == "__main__":
    main()
