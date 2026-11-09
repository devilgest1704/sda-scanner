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
import subprocess
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


def _reset_daily_pnl_if_needed(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("daily_pnl_date") != today:
        state["daily_pnl_date"] = today
        state["daily_realized_pnl_sda"] = 0.0
    return state


def load_state():
    path = Path(cfg.STATE_FILE)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Refusing to reset unreadable real trading state {path}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise RuntimeError(f"Refusing to reset invalid real trading state {path}")
    else:
        state = {}
    state.setdefault("positions", {})
    state.setdefault("pending_buys", {})
    state.setdefault("pending_sells", {})
    state.setdefault("trades", [])
    state.setdefault("daily_realized_pnl_sda", 0.0)
    state.setdefault("updated_at", None)
    _reset_daily_pnl_if_needed(state)
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
    # Read-only eth_call quotes in dry-run can use the dedicated bot wallet.
    # This fallback is never used to sign or broadcast transactions.
    if cfg.REAL_TRADING_DRY_RUN or not cfg.REAL_TRADING_ENABLED:
        return "0x9e3643f2ac15c91ee83aa2814015f15b18d573f2"
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
    """Return only a token-specific, historically observed fee tier.

    Entries copied from another token by the old fallback-fee heuristic are
    explicitly rejected: they can produce plausible-looking but false P/L
    quotes and unsafe SELL calldata.
    """
    liquidity = load_json("liquidity_data.json", {})
    quotes = liquidity.get("quotes_50_sda", {}) if isinstance(liquidity, dict) else {}
    item = quotes.get(addr(token)) if isinstance(quotes, dict) else None
    if not isinstance(item, dict):
        raise RuntimeError(f"No token-specific fee tier for {token}")
    source = str(item.get("source") or "").strip().lower()
    if source not in {"historical_token", "historical_buy", "live_pool_probe"}:
        raise RuntimeError(f"Untrusted fee tier source for {token}: {source or 'missing'}")
    if item.get("fee") is None or item.get("quote_status") != "VALIDATED_READ_ONLY":
        raise RuntimeError(f"No validated token-specific fee tier for {token}")
    fee = int(item["fee"])
    if fee not in {100, 500, 3000, 10000}:
        raise RuntimeError(f"Unsupported fee tier {fee} for {token}")
    return fee


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

def quote_best_live_fee(token_in, token_out, token, recipient, amount_in, value_wei=0):
    """Get a current router quote using a token-specific fee tier.

    Prefer the token's validated historical fee tier. If it is missing or the
    live quote fails, probe the supported V3 fee tiers with the actual trade
    amount and choose the best successful output. No fee copied from another
    token is ever trusted.
    """
    preferred = None
    try:
        preferred = fee_tier_for(token)
    except RuntimeError:
        pass

    fees = ([preferred] if preferred is not None else []) + [
        fee for fee in (100, 500, 3000, 10000) if fee != preferred
    ]
    successful = []
    errors = []
    for fee in fees:
        try:
            quoted, calldata, deadline = quote_exact_input_single(
                token_in, token_out, fee, recipient, amount_in, value_wei=value_wei
            )
            if quoted > 0:
                if fee == preferred:
                    return fee, quoted, calldata, deadline
                successful.append((quoted, fee, calldata, deadline))
        except Exception as exc:
            errors.append(f"{fee}:{str(exc)[:80]}")

    if successful:
        quoted, fee, calldata, deadline = max(successful, key=lambda item: item[0])
        return fee, quoted, calldata, deadline
    detail = "; ".join(errors[:4])
    raise RuntimeError(f"No live PinetSwap quote for {token} across fee tiers: {detail}")


def gas_price():
    return int(rpc("eth_gasPrice", []), 16)


def next_nonce(owner):
    return int(rpc("eth_getTransactionCount", [owner, "pending"]), 16)


def estimate_gas(owner, to, data, value=0):
    return int(rpc("eth_estimateGas", [{"from": owner, "to": to, "data": data, "value": hex(value)}]), 16)


def persist_real_intent_before_broadcast():
    """Commit the pending signed transaction intent before any live RPC broadcast."""
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true":
        raise RuntimeError("Live trading requires the durable GitHub Actions state writer")
    if not os.getenv("GH_TOKEN") or not os.getenv("GITHUB_REPOSITORY"):
        raise RuntimeError("Live trading requires GH_TOKEN and GITHUB_REPOSITORY")

    subprocess.run(["git", "config", "user.name", "sda-scanner-bot"], check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "scanner@github.local"], check=True, timeout=10)
    subprocess.run(["git", "add", "--", cfg.STATE_FILE], check=True, timeout=10)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", cfg.STATE_FILE],
        check=False, timeout=10,
    )
    if staged.returncode == 0:
        raise RuntimeError("Refusing live broadcast without a newly staged durable transaction intent")
    if staged.returncode != 1:
        raise RuntimeError("Unable to inspect staged real transaction intent")

    subprocess.run(
        ["git", "commit", "-m", "Update SDA scanner state", "--", cfg.STATE_FILE],
        check=True, timeout=20, capture_output=True, text=True,
    )
    for attempt in range(5):
        pushed = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            check=False, timeout=30, capture_output=True, text=True,
        )
        if pushed.returncode == 0:
            return
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=False, timeout=10, capture_output=True, text=True,
        )
        if status.returncode != 0:
            raise RuntimeError("Unable to inspect scanner worktree before state rebase")
        stash_created = False
        if status.stdout.strip():
            stashed = subprocess.run(
                ["git", "stash", "push", "-u", "-m", "real-intent-persist-rebase"],
                check=False, timeout=30, capture_output=True, text=True,
            )
            if stashed.returncode != 0:
                raise RuntimeError("Unable to protect scanner worktree before state rebase")
            stash_created = "No local changes to save" not in (stashed.stdout + stashed.stderr)
        fetched = subprocess.run(
            ["git", "fetch", "origin", "main"],
            check=False, timeout=30, capture_output=True, text=True,
        )
        if fetched.returncode != 0:
            if stash_created:
                subprocess.run(["git", "stash", "pop"], check=False, timeout=30)
            raise RuntimeError("Unable to fetch main while persisting live transaction intent")
        rebased = subprocess.run(
            ["git", "rebase", "origin/main"],
            check=False, timeout=30, capture_output=True, text=True,
        )
        if rebased.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], check=False, timeout=10)
            if stash_created:
                subprocess.run(["git", "stash", "pop"], check=False, timeout=30)
            raise RuntimeError("Conflict while durably persisting live transaction intent; broadcast aborted")
        if stash_created:
            restored = subprocess.run(
                ["git", "stash", "pop"],
                check=False, timeout=30, capture_output=True, text=True,
            )
            if restored.returncode != 0:
                raise RuntimeError("Unable to restore scanner worktree after state rebase; broadcast aborted")
    raise RuntimeError("Unable to durably persist live transaction intent after five attempts")


def send_transaction(account, to, data, value=0, gas_limit=None, before_broadcast=None):
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
    expected_hash = signed.hash.hex()
    if not expected_hash.startswith("0x"):
        expected_hash = "0x" + expected_hash
    if before_broadcast:
        before_broadcast({"tx_hash": expected_hash, "nonce": nonce, "gas": gas, "gas_price": gp})
    if cfg.REAL_TRADING_ENABLED and not cfg.REAL_TRADING_DRY_RUN and before_broadcast:
        persist_real_intent_before_broadcast()
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
    _reset_daily_pnl_if_needed(state)
    position_sda = float(position_sda)
    if position_sda < cfg.POSITION_MIN_SDA or position_sda > cfg.POSITION_MAX_SDA:
        raise ValueError("Real position must be between 10 and 20 SDA")
    positions = state.get("positions", {})
    pending_buys = state.get("pending_buys", {})
    open_count = len(positions) + len(pending_buys)
    if open_count >= cfg.MAX_OPEN_POSITIONS:
        raise RuntimeError("Maximum real open positions reached")
    open_cost = sum(float(x.get("amount_sda") or x.get("cost_sda") or 0) for x in positions.values())
    pending_cost = sum(float(x.get("position_sda") or 0) for x in pending_buys.values())
    if open_cost + pending_cost + position_sda > cfg.TRADING_WALLET_CAPITAL_SDA:
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
    amount_in = int(Decimal(str(position_sda)) * Decimal(10 ** 18))
    fee, quoted, calldata, deadline = quote_best_live_fee(
        cfg.WSDA_ADDRESS, token, token, owner, amount_in, value_wei=amount_in
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


def _finalize_confirmed_buy(token, symbol, plan, tx_hash, gas, gp, token_balance_before=None):
    account = private_account()
    try:
        token_balance_after = token_balance(token, account.address)
        received_raw = (token_balance_after - token_balance_before
                        if token_balance_before is not None else 0)
    except Exception:
        received_raw = 0
    if received_raw <= 0:
        # Never invent the acquired token amount from a quote: fees, transfer
        # taxes, partial fills or stale RPC reads can differ from min-out.
        # Keep the confirmed transaction pending for a later balance read.
        raise RuntimeError(
            f"BUY confirmed ({tx_hash}) but acquired token balance is unverified; "
            "leaving pending intent for reconciliation"
        )
    state = load_state()
    state.setdefault("pending_buys", {}).pop(addr(token), None)
    state["positions"][addr(token)] = {
        "symbol": symbol, "token": addr(token), "amount_sda": plan["position_sda"],
        "quoted_out_raw": plan["quoted_out_raw"], "minimum_out_raw": plan["minimum_out_raw"],
        "token_amount_raw": received_raw, "opened_at": now(), "buy_tx": tx_hash,
    }
    state["trades"].append({"action": "BUY", "symbol": symbol, "token": addr(token),
                            "amount_sda": plan["position_sda"], "tx": tx_hash, "at": now()})
    state["updated_at"] = now()
    save_state(state)
    return {"action": "BUY", "status": "CONFIRMED", "token": addr(token),
            "tx": tx_hash, "token_amount_raw": received_raw, "gas": gas, "gas_price": gp}


def execute_buy(token, symbol, position_sda=None):
    token = addr(token)
    state = load_state()
    existing = state.get("positions", {}).get(token)
    if isinstance(existing, dict):
        return {"action": "BUY", "symbol": symbol, "token": token,
                "status": "ALREADY_OPEN", "existing_buy_tx": existing.get("buy_tx")}

    # Persist the deterministic signed hash before broadcast. A lost RPC
    # response must never cause a second BUY on the next scanner cycle.
    pending = state.setdefault("pending_buys", {}).get(token)
    if isinstance(pending, dict):
        tx_hash = pending.get("tx_hash")
        if not tx_hash:
            return {"action": "BUY", "token": token, "status": "MANUAL_RECONCILIATION_REQUIRED"}
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            return {"action": "BUY", "token": token, "status": "PENDING_CONFIRMATION", "tx": tx_hash}
        if int(receipt.get("status", "0x0"), 16) != 1:
            state["pending_buys"].pop(token, None)
            save_state(state)
            raise RuntimeError(f"BUY failed: {tx_hash}")
        account = private_account()
        if chain_id() != cfg.CHAIN_ID or account.address.lower() != pending["wallet"].lower():
            raise RuntimeError("Pending BUY wallet or chain mismatch")
        return _finalize_confirmed_buy(token, pending.get("symbol") or symbol,
            pending["plan"], tx_hash, pending.get("gas"), pending.get("gas_price"),
            pending.get("token_balance_before"))

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
        balance_before = token_balance(token, account.address)
    except Exception:
        balance_before = None

    def persist_intent(signed):
        current = load_state()
        current.setdefault("pending_buys", {})[token] = {
            "stage": "broadcasting", "tx_hash": signed["tx_hash"], "nonce": signed["nonce"],
            "gas": signed["gas"], "gas_price": signed["gas_price"], "symbol": symbol,
            "wallet": account.address, "position_sda": plan["position_sda"], "plan": plan,
            "token_balance_before": balance_before, "created_at": now(),
        }
        current["updated_at"] = now()
        save_state(current)

    tx_hash, gas, gp = send_transaction(account, cfg.ROUTER_ADDRESS, plan["calldata"],
        plan["value_wei"], cfg.GAS_LIMIT, before_broadcast=persist_intent)
    receipt, ok = wait_receipt(tx_hash)
    if not ok:
        current = load_state()
        current.setdefault("pending_buys", {}).pop(token, None)
        save_state(current)
        raise RuntimeError(f"BUY failed: {tx_hash}")
    return _finalize_confirmed_buy(token, symbol, plan, tx_hash, gas, gp, balance_before)


def prepare_sell(token, symbol, token_amount_raw):
    owner = wallet_address()
    if not owner:
        raise RuntimeError("Set REAL_TRADING_WALLET_ADDRESS for dry-run or REAL_TRADING_PRIVATE_KEY for execution")
    token = addr(token)
    token_amount_raw = int(token_amount_raw)
    if token_amount_raw <= 0:
        raise ValueError("token_amount_raw must be positive")
    fee, quoted, calldata, deadline = quote_best_live_fee(
        token, cfg.WSDA_ADDRESS, token, owner, token_amount_raw, value_wei=0
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


def _finalize_confirmed_sell(token, pending):
    account = private_account()
    native_after = native_balance(account.address)
    native_before = int(pending.get("native_balance_before") or native_after)
    proceeds_net_sda = (native_after - native_before) / 10**18
    cost_sda = float(pending.get("cost_sda") or 0)
    realized_pnl = proceeds_net_sda - cost_sda
    state = load_state()
    state.setdefault("pending_sells", {}).pop(addr(token), None)
    state["positions"].pop(addr(token), None)
    state["daily_realized_pnl_sda"] = float(state.get("daily_realized_pnl_sda") or 0) + realized_pnl
    state["trades"].append({
        "action": "SELL", "symbol": pending.get("symbol"), "token": addr(token),
        "tx": pending.get("swap_tx"), "unwrap_tx": pending.get("unwrap_tx"),
        "amount_sda": cost_sda, "realized_pnl_sda": realized_pnl, "at": now(),
    })
    state["updated_at"] = now()
    save_state(state)
    return {
        "action": "SELL", "status": "CONFIRMED", "token": addr(token),
        "tx": pending.get("swap_tx"), "unwrap_tx": pending.get("unwrap_tx"),
        "realized_pnl_sda": realized_pnl,
    }


def execute_sell(token, symbol, token_amount_raw):
    """Fail closed: all automatic REAL SELLs must pass Swing SELL V2."""
    raise RuntimeError(
        "Legacy full-position SELL executor is disabled. "
        "Use real_swing_sell_executor.evaluate_position with a fresh market observation."
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
