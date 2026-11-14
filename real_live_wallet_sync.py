"""Incremental, read-only live wallet synchronization for the REAL runtime.

The persisted block cursor and imported trade identities are committed in the
same atomic REAL state write. A failed RPC/reconciliation cycle never advances
the cursor. This module never signs, broadcasts, or submits a transaction.
"""
from __future__ import annotations

import fcntl
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import real_trading_config as cfg

LOCK_FILE = Path("/tmp/sda-real-live-wallet-sync.lock")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SYNC_INTERVAL_SECONDS = int(__import__("os").getenv("REAL_WALLET_SYNC_INTERVAL_SECONDS", "45"))
FRESH_SECONDS = int(__import__("os").getenv("REAL_WALLET_FRESH_SECONDS", "120"))
DEGRADED_SECONDS = int(__import__("os").getenv("REAL_WALLET_DEGRADED_SECONDS", "300"))
BACKOFF_SECONDS = (5, 15, 30, 60)


def _now():
    return datetime.now(timezone.utc)


def _iso(value=None):
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _hexint(value):
    return int(value, 16) if isinstance(value, str) else int(value or 0)


def _address_topic(address):
    return "0x" + str(address).lower().replace("0x", "").rjust(64, "0")


def _log_address(value):
    return "0x" + str(value)[-40:].lower()


def _age(value, now=None):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return max(0.0, ((now or _now()) - stamp.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def freshness(state, now=None):
    age = _age(state.get("lastSuccessfulWalletSyncAt"), now)
    status = str(state.get("syncStatus") or "").upper()
    if age is None or age > DEGRADED_SECONDS or status in {"FAILED", "STALE"}:
        return "STALE", age
    if age > FRESH_SECONDS or status == "DEGRADED":
        return "DEGRADED", age
    return "FRESH", age


def _backoff(state):
    failures = int(state.get("walletSyncFailures") or 0)
    base = BACKOFF_SECONDS[min(max(failures - 1, 0), len(BACKOFF_SECONDS) - 1)]
    return max(1, int(base * random.uniform(0.8, 1.2)))


def _event_identity(tx_hash, log_index, token):
    return f"{str(tx_hash).lower()}:{_hexint(log_index)}:{str(token).lower()}"


def _transfer_amount(log):
    data = str(log.get("data") or "0x0")
    return _hexint(data)


def _known_tokens(state):
    tokens = set()
    for token in (state.get("positions") or {}):
        if str(token).startswith("0x"):
            tokens.add(str(token).lower())
    for trade in state.get("trades") or []:
        token = str(trade.get("token") or "").lower() if isinstance(trade, dict) else ""
        if token.startswith("0x"):
            tokens.add(token)
    return tokens


def sync_once(real_trader, *, now=None, max_blocks_per_cycle=250):
    """Perform one bounded incremental RPC sync; no writes happen on failure."""
    now = now or _now()
    attempted = _iso(now)
    lock = LOCK_FILE.open("a+")
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {"ok": False, "syncStatus": "IN_PROGRESS",
                    "syncError": "live wallet sync already running", "lastSyncAttemptAt": attempted}
        try:
            state = real_trader.load_state()
            wallet = str(real_trader.wallet_address() or "").lower()
            if not wallet:
                raise RuntimeError("REAL wallet address unavailable")
            if _hexint(real_trader.rpc("eth_chainId", [])) != cfg.CHAIN_ID:
                raise RuntimeError("RPC chain ID mismatch")
            latest = _hexint(real_trader.rpc("eth_blockNumber", []))
            saved_cursor = state.get("lastProcessedBlock")
            initial_lookback = int(__import__("os").getenv("REAL_WALLET_SYNC_INITIAL_LOOKBACK_BLOCKS", "2000"))
            start = (int(saved_cursor) + 1 if saved_cursor is not None
                     else max(0, latest - max(1, initial_lookback) + 1))
            if saved_cursor is not None and latest < int(saved_cursor):
                raise RuntimeError("RPC latest block is behind persisted wallet cursor")
            end = min(latest, start + max(1, max_blocks_per_cycle) - 1)
            cursor_complete = end >= latest

            # Stage all changes in memory. Cursor is advanced only in the final
            # atomic save after every block, receipt and balance query succeeds.
            staged = dict(state)
            staged["positions"] = dict(state.get("positions") or {})
            staged["trades"] = list(state.get("trades") or [])
            staged["manual_imports"] = list(state.get("manual_imports") or [])
            today_utc = now.date().isoformat()
            if staged.get("daily_pnl_date") != today_utc:
                staged["daily_pnl_date"] = today_utc
                staged["daily_realized_pnl_sda"] = 0.0
            seen = set(state.get("processedBlockchainEvents") or [])
            known = _known_tokens(staged)
            discovered = []
            latest_block_timestamp = None

            for number in range(start, end + 1):
                block = real_trader.rpc("eth_getBlockByNumber", [hex(number), True])
                if not isinstance(block, dict):
                    raise RuntimeError(f"RPC returned no block for {number}")
                block_time = datetime.fromtimestamp(_hexint(block.get("timestamp")), timezone.utc)
                latest_block_timestamp = _iso(block_time)
                for tx in block.get("transactions") or []:
                    if not isinstance(tx, dict) or str(tx.get("from") or "").lower() != wallet:
                        continue
                    tx_hash = str(tx.get("hash") or "").lower()
                    if not tx_hash:
                        raise RuntimeError(f"transaction hash missing in block {number}")
                    receipt = real_trader.rpc("eth_getTransactionReceipt", [tx_hash])
                    if not isinstance(receipt, dict):
                        raise RuntimeError(f"receipt unavailable for {tx_hash}")
                    if _hexint(receipt.get("status")) != 1:
                        continue
                    logs = receipt.get("logs") or []
                    incoming = []
                    outgoing = []
                    for log in logs:
                        topics = log.get("topics") or []
                        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                            continue
                        token = str(log.get("address") or "").lower()
                        sender = _log_address(topics[1])
                        recipient = _log_address(topics[2])
                        amount_raw = _transfer_amount(log)
                        if recipient == wallet and sender != wallet:
                            incoming.append((token, amount_raw, log))
                        elif sender == wallet and recipient != wallet:
                            outgoing.append((token, amount_raw, log))

                    # A confirmed swap BUY is a wallet-originated transaction
                    # receiving a non-WSDA token while spending native SDA or WSDA.
                    native_spend = Decimal(_hexint(tx.get("value"))) / Decimal(10**18)
                    wsda_spend = sum(Decimal(q) for token, q, _ in outgoing
                                     if token == cfg.WSDA_ADDRESS.lower()) / Decimal(10**18)
                    spend = native_spend if native_spend > 0 else wsda_spend
                    for token, raw_amount, log in incoming:
                        if token == cfg.WSDA_ADDRESS.lower() or raw_amount <= 0 or spend <= 0:
                            continue
                        identity = _event_identity(tx_hash, log.get("logIndex"), token)
                        if identity in seen:
                            continue
                        decimals = int(real_trader.token_decimals(token))
                        quantity = Decimal(raw_amount) / (Decimal(10) ** decimals)
                        if quantity <= 0:
                            continue
                        # Keep one open position per token. If the token already
                        # exists, record the lot identity and leave explicit
                        # multi-lot aggregation to the accounting layer.
                        if token not in staged["positions"]:
                            pending_intent = (staged.get("pending_buys") or {}).get(token)
                            known_bot_tx = any(
                                isinstance(item, dict)
                                and str(item.get("tx") or item.get("tx_hash") or "").lower() == tx_hash
                                and str(item.get("action") or "").upper() in {"BUY", "BUY_RECONCILED"}
                                for item in staged["trades"]
                            )
                            bot_buy = (known_bot_tx or
                                       (isinstance(pending_intent, dict)
                                        and str(pending_intent.get("tx_hash") or "").lower() == tx_hash))
                            source = "BOT" if bot_buy else "MANUAL"
                            gas_fee = Decimal(
                                _hexint(receipt.get("gasUsed")) *
                                _hexint(receipt.get("effectiveGasPrice") or tx.get("gasPrice"))
                            ) / Decimal(10**18)
                            position = {
                                "symbol": token[:10], "token": token, "status": "OPEN",
                                "source": source, "manual_imported": source == "MANUAL",
                                "sellManagementEnabled": True,
                                "amount_sda": float(spend), "initial_cost_basis": str(spend),
                                "remaining_cost_basis": str(spend), "sold_cost_basis": "0",
                                "realized_proceeds": "0", "realized_pnl": "0",
                                "token_amount_raw": raw_amount, "initial_quantity": raw_amount,
                                "remaining_quantity": raw_amount, "confirmed_sold_quantity": 0,
                                "opened_at": _iso(block_time), "buy_tx": tx_hash,
                                "position_id": identity, "accounting_identity": identity,
                                "network_fee_sda": str(gas_fee),
                                "execution_price_sda": str(spend / quantity),
                                "all_in_cost_price_sda": str(spend / quantity),
                                "profit_partial_done": False, "trailing_armed": False,
                            }
                            staged["positions"][token] = position
                            staged["trades"].append({
                                "action": "MANUAL_BUY" if source == "MANUAL" else "BUY_RECONCILED", "source": source, "status": "OPEN",
                                "token": token, "symbol": position["symbol"], "tx": tx_hash,
                                "instruction_index": _hexint(log.get("logIndex")),
                                "quantity": str(quantity), "quantity_raw": raw_amount,
                                "decimals": decimals, "invested_sda": str(spend),
                                "cost_basis_sda": str(spend),
                                "execution_price_sda": str(spend / quantity),
                                "network_fee_sda": str(gas_fee),
                                "accounting_identity": identity,
                                "sellManagementEnabled": True, "at": _iso(block_time),
                            })
                            if source == "MANUAL":
                                staged["manual_imports"].append({
                                    "tx": tx_hash, "token": token, "event_id": identity,
                                    "at": _iso(block_time), "accounting_identity": identity,
                                })
                            if isinstance(pending_intent, dict):
                                staged.setdefault("pending_buys", {}).pop(token, None)
                            discovered.append({"tx": tx_hash, "token": token,
                                               "quantity_raw": raw_amount, "cost_basis_sda": str(spend)})
                        seen.add(identity)
                    # Record outgoing token transfers as processed evidence. The
                    # Swing executor's wallet-balance reconciliation applies
                    # manual full/partial disposals to the existing REAL position.
                    pending_auto_sell = any(
                        isinstance(item, dict) and str(item.get("tx_hash") or "").lower() == tx_hash
                        for item in (staged.get("pending_sells") or {}).values()
                    )
                    incoming_wsda = sum(Decimal(q) for token, q, _ in incoming
                                        if token == cfg.WSDA_ADDRESS.lower()) / Decimal(10**18)
                    for token, sold_raw, log in outgoing:
                        identity = _event_identity(tx_hash, log.get("logIndex"), token)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        staged.setdefault("manualTransferEvents", []).append({
                            "event_id": identity, "tx": tx_hash, "token": token,
                            "at": _iso(block_time), "direction": "OUT",
                        })
                        position = staged["positions"].get(token)
                        if pending_auto_sell or not isinstance(position, dict):
                            continue
                        recorded = int(position.get("remaining_quantity",
                                                   position.get("token_amount_raw", 0)) or 0)
                        sold = min(recorded, sold_raw)
                        if recorded <= 0 or sold <= 0:
                            continue
                        cost_before = Decimal(str(position.get("remaining_cost_basis",
                                                              position.get("amount_sda", 0)) or 0))
                        sold_cost = cost_before * Decimal(sold) / Decimal(recorded)
                        remaining = recorded - sold
                        proceeds = incoming_wsda if incoming_wsda > 0 else None
                        # A bare wallet transfer is not a SELL. Do not reduce
                        # BOT quantity or invent realized PnL without swap proceeds.
                        if proceeds is None:
                            continue
                        gas_fee = Decimal(
                            _hexint(receipt.get("gasUsed")) *
                            _hexint(receipt.get("effectiveGasPrice") or tx.get("gasPrice"))
                        ) / Decimal(10**18)
                        net_pnl = proceeds - sold_cost - gas_fee if proceeds is not None else None
                        position["remaining_quantity"] = remaining
                        position["confirmed_sold_quantity"] = int(position.get("confirmed_sold_quantity") or 0) + sold
                        position["remaining_cost_basis"] = str(max(Decimal(0), cost_before - sold_cost))
                        position["sold_cost_basis"] = str(Decimal(str(position.get("sold_cost_basis") or 0)) + sold_cost)
                        position["realized_proceeds"] = (str(Decimal(str(position.get("realized_proceeds") or 0)) + proceeds)
                                                         if proceeds is not None else position.get("realized_proceeds"))
                        position["realized_pnl"] = (str(Decimal(str(position.get("realized_pnl") or 0)) + net_pnl)
                                                    if net_pnl is not None else position.get("realized_pnl"))
                        if net_pnl is not None and block_time.date().isoformat() == today_utc:
                            staged["daily_realized_pnl_sda"] = (
                                float(staged.get("daily_realized_pnl_sda") or 0) + float(net_pnl)
                            )
                        position["wallet_available_quantity"] = remaining
                        if remaining <= 0:
                            staged["positions"].pop(token, None)
                        staged["trades"].append({
                            "action": "MANUAL_SELL", "source": "MANUAL", "status": "CONFIRMED",
                            "token": token, "contract": token, "symbol": position.get("symbol"),
                            "tx": tx_hash, "buy_tx": position.get("buy_tx"),
                            "instruction_index": _hexint(log.get("logIndex")),
                            "sold_quantity_raw": sold, "cost_basis_sda": str(sold_cost),
                            "proceeds_sda": str(proceeds) if proceeds is not None else None,
                            "network_fee_sda": str(gas_fee),
                            "net_pnl_sda": str(net_pnl) if net_pnl is not None else None,
                            "at": _iso(block_time),
                            "accounting_identity": identity,
                        })

            # Current balances are read only and validated before the cursor commit.
            native = real_trader.native_balance(wallet)
            known.update(staged["positions"].keys())
            balances = {}
            for token in sorted(known):
                raw_balance = int(real_trader.token_balance(token, wallet))
                balances[token] = str(raw_balance)
                position = staged["positions"].get(token)
                if isinstance(position, dict):
                    tracked = int(position.get("remaining_quantity",
                                               position.get("token_amount_raw", 0)) or 0)
                    if raw_balance < tracked:
                        raise RuntimeError(
                            f"wallet balance below reconciled REAL position for {token}: "
                            f"wallet={raw_balance}, ledger={tracked}; missing SELL history"
                        )
            staged["walletBalances"] = {"native_sda_wei": str(native),
                                        "tokens_raw": balances, "wallet": wallet,
                                        "chain_id": cfg.CHAIN_ID, "checked_at": attempted}
            staged["lastProcessedBlock"] = end
            staged["lastProcessedSignature"] = (
                str((block.get("transactions") or [])[-1].get("hash"))
                if end >= start and (block.get("transactions") or []) else
                state.get("lastProcessedSignature")
            )
            staged["lastBlockchainDataAt"] = latest_block_timestamp or attempted
            staged["lastSyncAttemptAt"] = attempted
            staged["lastSuccessfulWalletSyncAt"] = attempted
            staged["lastSuccessfulWalletSync"] = attempted
            staged["lastSuccessfulReconciliationAt"] = attempted
            staged["lastSuccessfulReconciliation"] = attempted
            staged["ledgerUpdatedAt"] = attempted
            staged["walletSyncSource"] = "sidra_rpc_incremental_blocks"
            staged["walletSyncFailures"] = 0
            staged["lastSyncError"] = None
            staged["syncStatus"] = "FRESH" if cursor_complete else "DEGRADED"
            staged["walletSyncHealth"] = staged["syncStatus"]
            staged["buyDataHealthy"] = cursor_complete
            staged["walletSyncHealthy"] = cursor_complete
            staged["reconciliationHealthy"] = cursor_complete
            staged["sellDataHealthy"] = True
            staged["processedBlockchainEvents"] = sorted(seen)[-50000:]
            staged["updated_at"] = attempted
            real_trader.save_state(staged)
            return {"ok": cursor_complete, "syncStatus": staged["syncStatus"], "lastSyncAttemptAt": attempted,
                    "lastSuccessfulWalletSyncAt": attempted,
                    "lastSuccessfulReconciliationAt": attempted,
                    "lastBlockchainDataAt": staged["lastBlockchainDataAt"],
                    "imported": discovered, "processed_blocks": max(0, end - start + 1),
                    "syncError": None if cursor_complete else
                        f"incremental blockchain catch-up: cursor {end} of {latest}",
                    "walletBalances": staged["walletBalances"]}
        except Exception as exc:
            try:
                failed = real_trader.load_state()
                failures = int(failed.get("walletSyncFailures") or 0) + 1
                failed["walletSyncFailures"] = failures
                failed["lastSyncAttemptAt"] = attempted
                failed["lastSyncError"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                failed["syncStatus"] = "FAILED"
                failed["walletSyncHealth"] = "FAILED"
                failed["buyDataHealthy"] = False
                failed["reconciliationHealthy"] = False
                failed["walletSyncRetryAfterSeconds"] = _backoff(failed)
                real_trader.save_state(failed)
            except Exception:
                pass
            return {"ok": False, "syncStatus": "FAILED",
                    "syncError": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "lastSyncAttemptAt": attempted}
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()


def due(state, now=None):
    age = _age(state.get("lastSuccessfulWalletSyncAt"), now)
    if age is None:
        return True
    return age >= SYNC_INTERVAL_SECONDS


def retry_due(state):
    last = _age(state.get("lastSyncAttemptAt"))
    wait = int(state.get("walletSyncRetryAfterSeconds") or 0)
    return last is None or last >= wait
