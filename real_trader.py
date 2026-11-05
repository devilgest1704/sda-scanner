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
    state.setdefault("daily_pnl_date", datetime.now(timezone.utc).date().isoformat())
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
        + address_word(token_out)
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


def _reset_daily_pnl_if_needed(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("daily_pnl_date") != today:
        state["daily_pnl_date"] = today
        state["daily_realized_pnl_sda"] = 0.0


def safety_check(position_sda, state):
    _reset_daily_pnl_if_needed(state)
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
    existing = load_state().get("positions", {}).get(addr(token))
    if isinstance(existing, dict):
        return {"action": "BUY", "symbol": symbol, "token": addr(token),
                "status": "ALREADY_OPEN", "existing_buy_tx": existing.get("buy_tx")}
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
    try:
        token_balance_before = token_balance(token, account.address)
    except Exception:
        token_balance_before = None
    tx_hash, gas, gp = send_transaction(account, cfg.ROUTER_ADDRESS, plan["calldata"], plan["value_wei"], cfg.GAS_LIMIT)
    receipt, ok = wait_receipt(tx_hash)
    if not ok:
        raise RuntimeError(f"BUY failed: {tx_hash}")
    # Record actual received tokens when the ERC-20 balance query succeeds.
    # The on-chain minimum output is a conservative fallback, never the quote.
    try:
        token_balance_after = token_balance(token, account.address)
        received_raw = (token_balance_after - token_balance_before
                        if token_balance_before is not None else 0)
    except Exception:
        received_raw = 0
    if received_raw <= 0:
        received_raw = int(plan["minimum_out_raw"])
    state = load_state()
    state["positions"][addr(token)] = {
        "symbol": symbol,
        "token": addr(token),
        "amount_sda": plan["position_sda"],
        "quoted_out_raw": plan["quoted_out_raw"],
        "minimum_out_raw": plan["minimum_out_raw"],
        "token_amount_raw": received_raw,
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


def _finalize_confirmed_sell(token, symbol, state, position, tx_hash, unwrap_tx,
                              received_wsda_raw, approval=None, gas=None, gp=None):
    proceeds_sda = float(Decimal(received_wsda_raw) / Decimal(10 ** 18))
    cost_sda = float(position.get("amount_sda") or 0)
    realized_pnl_sda = proceeds_sda - cost_sda
    state["positions"].pop(addr(token), None)
    _reset_daily_pnl_if_needed(state)
    state["daily_realized_pnl_sda"] = float(state.get("daily_realized_pnl_sda") or 0) + realized_pnl_sda
    state["trades"].append({
        "action": "SELL", "symbol": symbol, "token": addr(token), "tx": tx_hash,
        "unwrap_tx": unwrap_tx, "received_wsda_raw": received_wsda_raw,
        "proceeds_sda": proceeds_sda, "cost_sda": cost_sda,
        "realized_pnl_sda": realized_pnl_sda, "at": now()
    })
    state["updated_at"] = now()
    save_state(state)
    return {"action": "SELL", "status": "CONFIRMED", "token": addr(token),
            "tx": tx_hash, "unwrap_tx": unwrap_tx,
            "received_wsda_raw": received_wsda_raw, "proceeds_sda": proceeds_sda,
            "realized_pnl_sda": realized_pnl_sda, "approval": approval,
            "gas": gas, "gas_price": gp}


def execute_sell(token, symbol, token_amount_raw):
    token = addr(token)
    state = load_state()
    position = state.get("positions", {}).get(token)
    if not isinstance(position, dict):
        return {"action": "SELL", "token": token, "status": "ALREADY_CLOSED"}

    if not cfg.REAL_TRADING_ENABLED or cfg.REAL_TRADING_DRY_RUN:
        plan = prepare_sell(token, symbol, token_amount_raw)
        plan["status"] = "DRY_RUN"
        return plan

    account = private_account()
    if chain_id() != cfg.CHAIN_ID:
        raise RuntimeError("Wrong Sidra Chain ID")
    if account.address.lower() != wallet_address().lower():
        raise RuntimeError("Trading wallet mismatch")

    pending = position.get("pending_sell")
    approval = None
    gas = gp = None

    if not isinstance(pending, dict):
        plan = prepare_sell(token, symbol, token_amount_raw)
        approval = approve_if_needed(account, token, plan["token_amount_raw"])
        wsda_before = token_balance(cfg.WSDA_ADDRESS, account.address)
        # Persist an intent before broadcast. If the process dies in the
        # broadcast/response gap, refuse an automatic second sell.
        position["pending_sell"] = {
            "stage": "submitting", "wsda_before_raw": wsda_before,
            "token_amount_raw": plan["token_amount_raw"], "symbol": symbol,
            "created_at": now(), "approval": approval,
        }
        state["positions"][token] = position
        save_state(state)
        tx_hash, gas, gp = send_transaction(
            account, cfg.ROUTER_ADDRESS, plan["calldata"], 0, cfg.GAS_LIMIT
        )
        pending = position["pending_sell"]
        pending.update({"stage": "swap_submitted", "tx": tx_hash,
                        "gas": gas, "gas_price": gp})
        save_state(state)
    else:
        if pending.get("stage") == "submitting" and not pending.get("tx"):
            raise RuntimeError(
                "SELL broadcast outcome is uncertain; automatic retry blocked. "
                "Reconcile wallet nonce and transaction history before proceeding."
            )
        approval = pending.get("approval")
        gas = pending.get("gas")
        gp = pending.get("gas_price")

    tx_hash = pending.get("tx")
    if not tx_hash:
        raise RuntimeError("Pending SELL has no transaction hash; manual reconciliation required")

    received_wsda_raw = pending.get("received_wsda_raw")
    if received_wsda_raw is None:
        receipt, ok = wait_receipt(tx_hash)
        if not ok:
            position.pop("pending_sell", None)
            state["positions"][token] = position
            save_state(state)
            raise RuntimeError(f"SELL failed: {tx_hash}")
        wsda_after = token_balance(cfg.WSDA_ADDRESS, account.address)
        received_wsda_raw = max(0, wsda_after - int(pending.get("wsda_before_raw") or 0))
        pending["stage"] = "swap_confirmed"
        pending["received_wsda_raw"] = received_wsda_raw
        pending["receipt"] = receipt
        save_state(state)

    unwrap_tx = pending.get("unwrap_tx")
    if received_wsda_raw > 0:
        if unwrap_tx:
            unwrap_receipt, unwrap_ok = wait_receipt(unwrap_tx)
            if not unwrap_ok:
                # The swap is already complete. Clear only the failed unwrap
                # hash so a later retry unwraps the same proceeds, never swaps again.
                pending.pop("unwrap_tx", None)
                save_state(state)
                raise RuntimeError(f"WSDA unwrap failed: {unwrap_tx}")
        else:
            unwrap_data = cfg.UNWRAP_SELECTOR + word(received_wsda_raw)
            unwrap_tx, _, _ = send_transaction(
                account, cfg.WSDA_ADDRESS, unwrap_data, 0, 100000
            )
            pending["unwrap_tx"] = unwrap_tx
            pending["stage"] = "unwrap_submitted"
            save_state(state)
            _, unwrap_ok = wait_receipt(unwrap_tx)
            if not unwrap_ok:
                pending.pop("unwrap_tx", None)
                save_state(state)
                raise RuntimeError(f"WSDA unwrap failed: {unwrap_tx}")

    return _finalize_confirmed_sell(
        token, symbol, state, position, tx_hash, unwrap_tx,
        int(received_wsda_raw), approval, gas, gp
    )


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
