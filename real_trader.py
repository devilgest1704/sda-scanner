"""Sidra real-trading executor.

Safety model:
- REAL_TRADING_ENABLED is false by default.
- REAL_TRADING_DRY_RUN is true by default.
- Private key is read only from REAL_TRADING_PRIVATE_KEY.
- No key/seed is ever written to disk, Telegram, GitHub or logs.
- Every swap is quoted with eth_call first; a failed quote aborts the trade.
- Position size is clamped to 10-20 SDA and total capital is capped at 200 SDA.

The module is intentionally standalone. Existing paper trading remains unchanged
until the real-trading integration is explicitly enabled later.
"""

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from eth_account import Account

import real_trading_config as cfg

ZERO = "0x0000000000000000000000000000000000000000"
UINT256_MAX = (1 << 256) - 1


def now():
    return datetime.now(timezone.utc).isoformat()


def addr(value):
    if not isinstance(value, str) or not value:
        return ""
    return value.lower()


def word(value):
    return f"{int(value):064x}"


def address_word(value):
    value = value.lower().replace("0x", "")
    if len(value) != 40:
        raise ValueError(f"Invalid address: {value}")
    return value.rjust(64, "0")


def rpc(method, params):
    response = requests.post(
        cfg.RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
        headers={"Content-Type": "application/json", "User-Agent": "sda-real-trader/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_state(data):
    path = Path(cfg.STATE_FILE)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    state = load_json(cfg.STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("positions", {})
    state.setdefault("trades", [])
    state.setdefault("daily_realized_pnl_sda", 0.0)
    state.setdefault("updated_at", None)
    return state


def private_account():
    key = os.getenv(cfg.PRIVATE_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"Missing {cfg.PRIVATE_KEY_ENV}")
    account = Account.from_key(key)
    configured = os.getenv(cfg.WALLET_ADDRESS_ENV, "").strip().lower()
    if configured and configured != account.address.lower():
        raise RuntimeError("REAL_TRADING_WALLET_ADDRESS does not match the private key")
    return account


def wallet_address():
    configured = os.getenv(cfg.WALLET_ADDRESS_ENV, "").strip()
    if configured:
        return configured
    key = os.getenv(cfg.PRIVATE_KEY_ENV, "").strip()
    if key:
        return Account.from_key(key).address
    return ""


def chain_id():
    return int(rpc("eth_chainId", []), 16)


def native_balance(address):
    return int(rpc("eth_getBalance", [address, "latest"]), 16)


def token_decimals(token):
    metadata = load_json("token_metadata.json", {})
    item = metadata.get(addr(token)) if isinstance(metadata, dict) else None
    if isinstance(item, dict) and item.get("decimals") is not None:
        return int(item["decimals"])
    # The current Sidra catalog is predominantly 18-decimal. Do not silently
    # guess for an unknown token when executing a real trade.
    raise RuntimeError(f"Unknown decimals for token {token}; refresh token_metadata.json first")


def token_balance(token, owner):
    data = cfg.BALANCE_OF_SELECTOR + address_word(owner)
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def allowance(token, owner, spender):
    data = cfg.ALLOWANCE_SELECTOR + address_word(owner) + address_word(spender)
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def fee_tier_for(token):
    liquidity = load_json("liquidity_data.json", {})
    quotes = liquidity.get("quotes_50_sda", {}) if isinstance(liquidity, dict) else {}
    item = quotes.get(addr(token)) if isinstance(quotes, dict) else None
    if isinstance(item, dict) and item.get("fee") is not None and item.get("quote_status") == "VALIDATED_READ_ONLY":
        return int(item["fee"])
    raise RuntimeError(f"No validated fee tier for {token} in liquidity_data.json")


def build_exact_input_single(token_in, token_out, fee, recipient, deadline, amount_in, amount_out_min):
    return (
        cfg.BUY_SELECTOR
        + address_word(token_in)
        + word(fee)
        + address_word(recipient)
        + word(deadline)
        + word(amount_in)
        + word(amount_out_min)
        + word(0)
    )


def quote_exact_input_single(token_in, token_out, fee, recipient, amount_in, value_wei=0):
    deadline = int(time.time()) + cfg.DEADLINE_SECONDS
    calldata = build_exact_input_single(token_in, token_out, fee, recipient, deadline, amount_in, 0)
    result = rpc(
        "eth_call",
        [{"from": recipient, "to": cfg.ROUTER_ADDRESS, "data": calldata, "value": hex(value_wei)}, "latest"],
    )
    return int(result, 16), calldata, deadline


def gas_price():
    return int(rpc("eth_gasPrice", []), 16)


def next_nonce(owner):
    return int(rpc("eth_getTransactionCount", [owner, "pending"]), 16)


def estimate_gas(owner, to, data, value=0):
    return int(rpc("eth_estimateGas", [{"from": owner, "to": to, "data": data, "value": hex(value)}]), 16)


def send_transaction(account, to, data, value=0, gas_limit=None):
    nonce = next_nonce(account.address)
    gp = gas_price()
    gas = gas_limit or estimate_gas(account.address, to, data, value)
    tx = {
        "chainId": cfg.CHAIN_ID,
        "nonce": nonce,
        "to": to,
        "value": value,
        "gas": int(gas),
        "gasPrice": gp,
        "data": data,
    }
    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw])
    return tx_hash, gas, gp


def wait_receipt(tx_hash, timeout=120):
    started = time.time()
    while time.time() - started < timeout:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            status = int(receipt.get("status", "0x0"), 16)
            return receipt, status == 1
        time.sleep(2)
    raise TimeoutError(f"Transaction not confirmed within {timeout}s: {tx_hash}")


def min_out(quoted_raw):
    # Keep the DEX's on-chain minimum-received protection. We deliberately do
    # not use a looser slippage than the configured 1% maximum.
    return int(Decimal(quoted_raw) * (Decimal("1") - Decimal(str(cfg.MAX_SLIPPAGE_PCT)) / Decimal("100")))


def approve_if_needed(account, token, amount):
    current = allowance(token, account.address, cfg.ROUTER_ADDRESS)
    if current >= amount:
        return None
    data = cfg.APPROVE_SELECTOR + address_word(cfg.ROUTER_ADDRESS) + word(UINT256_MAX)
    tx_hash, gas, gp = send_transaction(account, token, data, 0, 100000)
    receipt, ok = wait_receipt(tx_hash)
    if not ok:
        raise RuntimeError(f"Approval failed: {tx_hash}")
    return {"tx": tx_hash, "gas": gas, "gas_price": gp}


def safety_check(position_sda, state):
    position_sda = float(position_sda)
    if position_sda < cfg.POSITION_MIN_SDA or position_sda > cfg.POSITION_MAX_SDA:
        raise ValueError("Real position must be between 10 and 20 SDA")
    open_count = len(state.get("positions", {}))
    if open_count >= cfg.MAX_OPEN_POSITIONS:
        raise RuntimeError("Maximum real open positions reached")
    open_cost = sum(float(x.get("cost_sda") or 0) for x in state.get("positions", {}).values())
    if open_cost + position_sda > cfg.TRADING_WALLET_CAPITAL_SDA:
        raise RuntimeError("Real trading capital cap of 200 SDA would be exceeded")
    if float(state.get("daily_realized_pnl_sda") or 0) <= -cfg.MAX_DAILY_LOSS_SDA:
        raise RuntimeError("Daily real-trading loss limit reached")


def prepare_buy(token, symbol, position_sda=None):
    position_sda = cfg.position_sda(position_sda)
    state = load_state()
    safety_check(position_sda, state)
    owner = wallet_address()
    if not owner:
        raise RuntimeError("Set REAL_TRADING_WALLET_ADDRESS for dry-run or REAL_TRADING_PRIVATE_KEY for execution")
    token = addr(token)
    fee = fee_tier_for(token)
    amount_in = int(Decimal(str(position_sda)) * Decimal(10 ** 18))
    quoted, calldata, deadline = quote_exact_input_single(
        cfg.WSDA_ADDRESS, token, fee, owner, amount_in, value_wei=amount_in
    )
    output_min = min_out(quoted)
    calldata = build_exact_input_single(cfg.WSDA_ADDRESS, token, fee, owner, deadline, amount_in, output_min)
    return {
        "action": "BUY",
        "symbol": symbol,
        "token": token,
        "position_sda": position_sda,
        "fee_tier": fee,
        "quoted_out_raw": quoted,
        "minimum_out_raw": output_min,
        "deadline": deadline,
        "router": cfg.ROUTER_ADDRESS,
        "wallet": owner,
        "value_wei": amount_in,
        "calldata": calldata,
    }


def execute_buy(token, symbol, position_sda=None):
    plan = prepare_buy(token, symbol, position_sda)
    if not cfg.REAL_TRADING_ENABLED or cfg.REAL_TRADING_DRY_RUN:
        plan["status"] = "DRY_RUN"
        return plan
    account = private_account()
    if account.address.lower() != plan["wallet"].lower():
        raise RuntimeError("Trading wallet mismatch")
    if chain_id() != cfg.CHAIN_ID:
        raise RuntimeError("Wrong Sidra Chain ID")
    if native_balance(account.address) < plan["value_wei"]:
        raise RuntimeError("Insufficient SDA balance for BUY + gas")
    tx_hash, gas, gp = send_transaction(account, cfg.ROUTER_ADDRESS, plan["calldata"], plan["value_wei"], cfg.GAS_LIMIT)
    receipt, ok = wait_receipt(tx_hash)
    if not ok:
        raise RuntimeError(f"BUY failed: {tx_hash}")
    state = load_state()
    state["positions"][addr(token)] = {
        "symbol": symbol,
        "token": addr(token),
        "amount_sda": plan["position_sda"],
        "quoted_out_raw": plan["quoted_out_raw"],
        "minimum_out_raw": plan["minimum_out_raw"],
        "opened_at": now(),
        "buy_tx": tx_hash,
    }
    state["trades"].append({"action": "BUY", "symbol": symbol, "token": addr(token), "amount_sda": plan["position_sda"], "tx": tx_hash, "at": now()})
    state["updated_at"] = now()
    save_state(state)
    return {**plan, "status": "CONFIRMED", "tx": tx_hash, "gas": gas, "gas_price": gp, "receipt": receipt}


def prepare_sell(token, symbol, token_amount_raw):
    owner = wallet_address()
    if not owner:
        raise RuntimeError("Set REAL_TRADING_WALLET_ADDRESS for dry-run or REAL_TRADING_PRIVATE_KEY for execution")
    token = addr(token)
    fee = fee_tier_for(token)
    token_amount_raw = int(token_amount_raw)
    if token_amount_raw <= 0:
        raise ValueError("token_amount_raw must be positive")
    quoted, calldata, deadline = quote_exact_input_single(
        token, cfg.WSDA_ADDRESS, fee, owner, token_amount_raw, value_wei=0
    )
    output_min = min_out(quoted)
    calldata = build_exact_input_single(token, cfg.WSDA_ADDRESS, fee, owner, deadline, token_amount_raw, output_min)
    return {
        "action": "SELL",
        "symbol": symbol,
        "token": token,
        "token_amount_raw": token_amount_raw,
        "fee_tier": fee,
        "quoted_wsda_raw": quoted,
        "minimum_wsda_raw": output_min,
        "deadline": deadline,
        "router": cfg.ROUTER_ADDRESS,
        "wallet": owner,
        "calldata": calldata,
    }


def execute_sell(token, symbol, token_amount_raw):
    plan = prepare_sell(token, symbol, token_amount_raw)
    if not cfg.REAL_TRADING_ENABLED or cfg.REAL_TRADING_DRY_RUN:
        plan["status"] = "DRY_RUN"
        return plan
    account = private_account()
    if account.address.lower() != plan["wallet"].lower():
        raise RuntimeError("Trading wallet mismatch")
    if chain_id() != cfg.CHAIN_ID:
        raise RuntimeError("Wrong Sidra Chain ID")
    approval = approve_if_needed(account, token, plan["token_amount_raw"])
    tx_hash, gas, gp = send_transaction(account, cfg.ROUTER_ADDRESS, plan["calldata"], 0, cfg.GAS_LIMIT)
    receipt, ok = wait_receipt(tx_hash)
    if not ok:
        raise RuntimeError(f"SELL failed: {tx_hash}")

    # Unwrap WSDA to native SDA only after the swap itself is confirmed.
    unwrap_data = cfg.UNWRAP_SELECTOR + word(plan["minimum_wsda_raw"])
    unwrap_tx = None
    if plan["minimum_wsda_raw"] > 0:
        unwrap_tx, ugas, ugp = send_transaction(account, cfg.WSDA_ADDRESS, unwrap_data, 0, 100000)
        _, unwrap_ok = wait_receipt(unwrap_tx)
        if not unwrap_ok:
            raise RuntimeError(f"WSDA unwrap failed: {unwrap_tx}")

    state = load_state()
    state["positions"].pop(addr(token), None)
    state["trades"].append({"action": "SELL", "symbol": symbol, "token": addr(token), "tx": tx_hash, "unwrap_tx": unwrap_tx, "at": now()})
    state["updated_at"] = now()
    save_state(state)
    return {**plan, "status": "CONFIRMED", "tx": tx_hash, "unwrap_tx": unwrap_tx, "approval": approval, "gas": gas, "gas_price": gp}


if __name__ == "__main__":
    print(json.dumps({
        "enabled": cfg.REAL_TRADING_ENABLED,
        "dry_run": cfg.REAL_TRADING_DRY_RUN,
        "wallet": wallet_address(),
        "capital_sda": cfg.TRADING_WALLET_CAPITAL_SDA,
        "position_range_sda": [cfg.POSITION_MIN_SDA, cfg.POSITION_MAX_SDA],
        "router": cfg.ROUTER_ADDRESS,
        "chain_id": cfg.CHAIN_ID,
    }, indent=2))
