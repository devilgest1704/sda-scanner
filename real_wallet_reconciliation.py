"""Idempotent import of blockchain-confirmed manual REAL BUYs.

The accounting snapshot is read-only evidence. This module never signs or sends
transactions; it verifies the wallet, chain, snapshot freshness, transaction
receipt and current token balance before adding a missing position.
"""
from __future__ import annotations

import fcntl
import json
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests

ACCOUNTING_FILE = Path("real_accounting_state.json")
ACCOUNTING_URL = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/real_accounting_state.json"
LOCK_FILE = Path("/tmp/sda-real-wallet-reconciliation.lock")
MAX_SNAPSHOT_AGE_SECONDS = 20 * 60
CHAIN_ID = 97453


def _stamp(value):
    try:
        result = datetime.fromisoformat(str(value))
        if result.tzinfo is None:
            return None
        return result.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def snapshot_health(snapshot, now=None, max_age=MAX_SNAPSHOT_AGE_SECONDS):
    now = now or datetime.now(UTC)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        return False, "accounting snapshot missing or unsupported"
    generated = _stamp(snapshot.get("generated_at"))
    if generated is None:
        return False, "accounting snapshot generated_at missing or invalid"
    age = (now - generated).total_seconds()
    if age < 0:
        return False, "accounting snapshot timestamp is in the future"
    if age > max_age:
        return False, f"stale blockchain snapshot ({int(age)}s old)"
    blockchain_data = _stamp((snapshot.get("blockchain_range") or {}).get("latest_timestamp"))
    if blockchain_data is None:
        return False, "latest blockchain data timestamp missing or invalid"
    blockchain_age = (now - blockchain_data).total_seconds()
    if blockchain_age < 0:
        return False, "latest blockchain data timestamp is in the future"
    if blockchain_age > max_age:
        return False, f"stale blockchain data ({int(blockchain_age)}s old)"
    if int(snapshot.get("chain_id") or 0) != CHAIN_ID:
        return False, "accounting snapshot chain mismatch"
    if not snapshot.get("wallet"):
        return False, "accounting snapshot wallet missing"
    if snapshot.get("pagination_exhausted") is not True:
        return False, "blockchain history pagination incomplete"
    if str(snapshot.get("bot_reconciliation_status") or "").upper() != "COMPLETE":
        return False, "blockchain reconciliation incomplete"
    if snapshot.get("bot_unresolved_transactions"):
        return False, "unresolved blockchain transactions remain"
    validation = snapshot.get("validation")
    if not isinstance(validation, dict):
        return False, "blockchain accounting validation missing"
    if validation.get("all_pagination_exhausted") is not True:
        return False, "blockchain history pagination incomplete"
    if validation.get("detail_fetch_errors"):
        return False, "blockchain transaction detail fetch errors"
    if validation.get("cash_reconciliation_zero") is not True:
        return False, "wallet cash reconciliation mismatch"
    if validation.get("ledger_loaded") is not True:
        return False, "REAL accounting ledger not loaded"
    if validation.get("open_positions_valued") is not True:
        return False, "one or more REAL open positions lack safe price data"
    if not isinstance(snapshot.get("open_positions"), list):
        return False, "blockchain open positions missing"
    if not isinstance(snapshot.get("buy_transactions"), list):
        return False, "confirmed BUY transaction list missing"
    return True, None


def _pool_fee_tier(transaction):
    """Read the Uniswap-V3 exactInputSingle fee tier from transaction calldata."""
    if not isinstance(transaction, dict):
        return None
    data = str(transaction.get("input") or transaction.get("data") or "").lower()
    selector = "0x414bf389"
    offset = data.find(selector)
    if offset < 0:
        return None
    start = offset + len(selector) + 64 * 2
    raw_fee = data[start:start + 64]
    if len(raw_fee) != 64:
        return None
    try:
        return int(raw_fee, 16)
    except ValueError:
        return None


def _quantity_raw(quantity, decimals):
    value = Decimal(str(quantity))
    if not value.is_finite() or value <= 0 or decimals < 0 or decimals > 36:
        raise ValueError("invalid token quantity or decimals")
    raw = value * (Decimal(10) ** decimals)
    integral = raw.to_integral_value(rounding=ROUND_DOWN)
    if raw != integral:
        raise ValueError("token quantity exceeds supported decimals")
    return int(integral)


def reconcile_manual_buys(real_trader, *, snapshot_path=None,
                          now=None, max_age=MAX_SNAPSHOT_AGE_SECONDS):
    """Import missing confirmed BUY lots; return a fail-closed health record."""
    now = now or datetime.now(UTC)
    attempted = now.astimezone(UTC).isoformat()
    try:
        lock_handle = LOCK_FILE.open("a+", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "syncStatus": "FAILED", "syncError": f"lock open failed: {exc}",
                "lastSyncAttemptAt": attempted, "imported": []}
    with lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            return {"ok": False, "syncStatus": "IN_PROGRESS",
                    "syncError": "wallet reconciliation already running",
                    "lastSyncAttemptAt": attempted, "imported": []}
        try:
            try:
                if snapshot_path is not None:
                    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
                    snapshot_source = str(snapshot_path)
                else:
                    response = requests.get(
                        ACCOUNTING_URL,
                        params={"_": str(int(time.time() * 1000))},
                        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                        timeout=15,
                    )
                    response.raise_for_status()
                    snapshot = response.json()
                    snapshot_source = ACCOUNTING_URL
            except Exception as exc:  # noqa: BLE001 - surface provider/network failures to the health state
                return {"ok": False, "syncStatus": "FAILED",
                        "syncError": f"blockchain snapshot fetch failed: {type(exc).__name__}: {exc}",
                        "lastSyncAttemptAt": attempted, "imported": []}
            healthy, reason = snapshot_health(snapshot, now=now, max_age=max_age)
            if not healthy:
                return {"ok": False, "syncStatus": "STALE" if reason and "stale" in reason else "FAILED",
                        "syncError": reason, "lastSyncAttemptAt": attempted, "imported": []}

            wallet = str(real_trader.wallet_address() or "").lower()
            snapshot_wallet = str(snapshot.get("wallet") or "").lower()
            if not wallet or wallet != snapshot_wallet:
                return {"ok": False, "syncStatus": "FAILED",
                        "syncError": "REAL wallet does not match blockchain snapshot",
                        "lastSyncAttemptAt": attempted, "imported": []}
            if int(real_trader.chain_id()) != CHAIN_ID:
                return {"ok": False, "syncStatus": "FAILED", "syncError": "REAL chain ID mismatch",
                        "lastSyncAttemptAt": attempted, "imported": []}

            buys = {}
            for row in snapshot.get("buy_transactions", []):
                if not isinstance(row, dict):
                    continue
                tx = str(row.get("tx") or "").lower()
                token = str(row.get("contract") or "").lower()
                if tx.startswith("0x") and token.startswith("0x"):
                    buys[(tx, token)] = row

            state = real_trader.load_state()
            positions = state.setdefault("positions", {})
            trades = state.setdefault("trades", [])
            imports = state.setdefault("manual_imports", [])
            pending_buys = state.setdefault("pending_buys", {})
            imported = []
            skipped = []
            for row in snapshot.get("open_positions", []):
                if not isinstance(row, dict):
                    continue
                token = str(row.get("token") or "").lower()
                tx = str(row.get("buy_tx") or "").lower()
                buy = buys.get((tx, token))
                if not buy:
                    continue
                if any(str(p.get("buy_tx") or "").lower() == tx
                       for p in positions.values() if isinstance(p, dict)):
                    continue
                already_imported = any(str(item.get("tx") or "").lower() == tx and
                                        str(item.get("token") or "").lower() == token
                                        for item in imports if isinstance(item, dict))
                if token in positions and isinstance(positions[token], dict):
                    # Do not merge distinct lots into an existing position with a
                    # different cost basis. Leave it for explicit multi-lot handling.
                    skipped.append({"token": token, "tx": tx,
                                    "reason": "token already has a different REAL position"})
                    continue
                try:
                    decimals = int(row.get("decimals") if row.get("decimals") is not None
                                   else real_trader.token_decimals(token))
                    raw_quantity = _quantity_raw(row.get("quantity"), decimals)
                    wallet_quantity = int(real_trader.token_balance(token, wallet))
                    if wallet_quantity < raw_quantity:
                        raise ValueError("wallet balance below blockchain open-lot quantity")
                    receipt = real_trader.rpc("eth_getTransactionReceipt", [tx])
                    if not isinstance(receipt, dict) or int(receipt.get("status", "0x0"), 16) != 1:
                        raise ValueError("BUY transaction receipt missing or unsuccessful")
                    try:
                        transaction = real_trader.rpc("eth_getTransactionByHash", [tx])
                        pool_fee_tier = _pool_fee_tier(transaction)
                    except Exception:  # noqa: BLE001 - fee tier is optional metadata; receipt remains authoritative
                        pool_fee_tier = None
                    cost = Decimal(str(row.get("cost_basis_sda") or buy.get("invested_sda")))
                    invested = Decimal(str(buy.get("invested_sda") or cost))
                    if not cost.is_finite() or cost <= 0 or invested <= 0:
                        raise ValueError("invalid blockchain cost basis")
                    opened = row.get("at") or buy.get("at")
                    price = invested / Decimal(str(row.get("quantity")))
                    existing_trade = next((trade for trade in trades
                                           if isinstance(trade, dict)
                                           and str(trade.get("tx") or trade.get("tx_hash") or "").lower() == tx
                                           and str(trade.get("token") or "").lower() == token
                                           and str(trade.get("action") or "").upper() in {"BUY", "MANUAL_BUY", "BUY_RECONCILED"}), None)
                    pending_intent = pending_buys.get(token) if isinstance(pending_buys, dict) else None
                    pending_match = (isinstance(pending_intent, dict)
                                     and str(pending_intent.get("tx_hash") or "").lower() == tx)
                    source = "MANUAL"
                    if existing_trade or pending_match:
                        existing_source = str((existing_trade or {}).get("source") or "BOT").upper()
                        existing_action = str((existing_trade or {}).get("action") or "").upper()
                        source = "MANUAL" if existing_source.startswith("MANUAL") or existing_action == "MANUAL_BUY" else "BOT"
                    position = {
                        "symbol": str(row.get("symbol") or buy.get("symbol") or token[:8]),
                        "token": token,
                        "amount_sda": float(cost),
                        "quoted_out_raw": raw_quantity,
                        "minimum_out_raw": raw_quantity,
                        "token_amount_raw": raw_quantity,
                        "opened_at": opened,
                        "buy_tx": tx,
                        "position_id": tx,
                        "initial_quantity": raw_quantity,
                        "remaining_quantity": raw_quantity,
                        "confirmed_sold_quantity": 0,
                        "initial_cost_basis": str(cost),
                        "remaining_cost_basis": str(cost),
                        "sold_cost_basis": "0",
                        "realized_proceeds": "0",
                        "realized_pnl": "0",
                        "wallet_available_quantity": wallet_quantity,
                        "source": source,
                        "sellManagementEnabled": True,
                        "manual_imported": source == "MANUAL",
                        "manual_imported_at": attempted if source == "MANUAL" else None,
                        "accounting_identity": f"{CHAIN_ID}:{wallet}:{tx}:{token}",
                        "execution_price_sda": str(price),
                        "last_market_price": str(Decimal(str(row.get("value_sda") or invested)) /
                                                 Decimal(str(row.get("quantity")))),
                        "profit_partial_done": False,
                        "trailing_armed": False,
                    }
                    positions[token] = position
                    receipt_fee = None
                    try:
                        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
                        gas_price = int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice") or "0x0", 16)
                        receipt_fee = str(Decimal(gas_used * gas_price) / Decimal(10 ** 18))
                    except (TypeError, ValueError):
                        pass
                    position["network_fee_sda"] = receipt_fee
                    position["swap_fee_sda"] = None
                    position["swap_fee_tier"] = pool_fee_tier
                    position["swap_fee_rate_pct"] = (str(Decimal(pool_fee_tier) / Decimal(10000))
                                                     if pool_fee_tier is not None else None)
                    position["swap_fee_status"] = ("pool fee tier recorded; exact fee amount not separately itemized"
                                                   if pool_fee_tier is not None else
                                                   "embedded in swap execution; not separately itemized")
                    position["all_in_cost_price_sda"] = str(cost / Decimal(str(row.get("quantity"))))
                    record = {
                        "action": "MANUAL_BUY" if source == "MANUAL" else "BUY_RECONCILED",
                        "source": source, "status": "OPEN",
                        "token": token, "symbol": position["symbol"], "tx": tx,
                        "at": opened, "quantity": str(row.get("quantity")),
                        "quantity_raw": raw_quantity, "decimals": decimals,
                        "invested_sda": str(invested), "cost_basis_sda": str(cost),
                        "execution_price_sda": str(price),
                        "network_fee_sda": receipt_fee,
                        "swap_fee_sda": None,
                        "swap_fee_tier": pool_fee_tier,
                        "swap_fee_rate_pct": (str(Decimal(pool_fee_tier) / Decimal(10000))
                                              if pool_fee_tier is not None else None),
                        "swap_fee_included_in_execution_price": True,
                        "all_in_cost_price_sda": str(cost / Decimal(str(row.get("quantity")))),
                        "accounting_identity": position["accounting_identity"],
                        "sellManagementEnabled": True,
                    }
                    if pending_match:
                        pending_buys.pop(token, None)
                    if not existing_trade:
                        trades.append(record)
                    if source == "MANUAL" and not already_imported:
                        imports.append({"tx": tx, "token": token, "at": opened,
                                        "accounting_identity": position["accounting_identity"]})
                    imported.append({"token": token, "symbol": position["symbol"], "tx": tx,
                                     "source": source, "quantity_raw": raw_quantity,
                                     "cost_basis_sda": str(cost)})
                except Exception as exc:  # noqa: BLE001 - record unexpected per-position RPC failures
                    skipped.append({"token": token, "tx": tx,
                                    "reason": f"{type(exc).__name__}: {str(exc)[:240]}"})

            if imported:
                state["updated_at"] = attempted
            state["lastSyncAttemptAt"] = attempted
            state["lastSuccessfulWalletSync"] = attempted
            state["lastBlockchainSnapshotGeneratedAt"] = snapshot.get("generated_at")
            state["lastBlockchainDataAt"] = (snapshot.get("blockchain_range") or {}).get("latest_timestamp")
            state["ledgerUpdatedAt"] = attempted if imported else state.get("ledgerUpdatedAt")
            # The outer REAL bridge records reconciliation success only after
            # its wallet-position reconciliation phase also completes.
            state["syncStatus"] = "FRESH" if not skipped else "DEGRADED"
            state["lastSyncError"] = "; ".join(item["reason"] for item in skipped) if skipped else None
            state["walletSyncSource"] = snapshot_source
            real_trader.save_state(state)
            return {"ok": not skipped, "syncStatus": state["syncStatus"],
                    "syncError": state["lastSyncError"], "lastSyncAttemptAt": attempted,
                    "lastSuccessfulWalletSync": state["lastSuccessfulWalletSync"],
                    "lastBlockchainSnapshotGeneratedAt": state["lastBlockchainSnapshotGeneratedAt"],
                    "lastSuccessfulReconciliation": state["lastSuccessfulReconciliation"],
                    "lastBlockchainDataAt": state["lastBlockchainDataAt"],
                    "ledgerUpdatedAt": state["ledgerUpdatedAt"],
                    "imported": imported, "skipped": skipped}
        finally:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
