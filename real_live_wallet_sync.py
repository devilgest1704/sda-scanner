"""Incremental read-only wallet reconciliation using Sidra's existing Blockscout API.

No signing/broadcasting occurs here. Explorer pagination + transaction receipts
are staged in memory; balances, imports and cursors are persisted in one atomic
REAL-state replacement. The cursor is never advanced on a partial API failure.
"""
from __future__ import annotations

import fcntl
import os
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

import real_trading_config as cfg

API = cfg.EXPLORER_URL.rstrip("/") + "/api/v2"
LOCK_FILE = Path("/tmp/sda-real-live-wallet-sync.lock")
SYNC_INTERVAL_SECONDS = int(os.getenv("REAL_WALLET_SYNC_INTERVAL_SECONDS", "45"))
FRESH_SECONDS = int(os.getenv("REAL_WALLET_FRESH_SECONDS", "120"))
DEGRADED_SECONDS = int(os.getenv("REAL_WALLET_DEGRADED_SECONDS", "300"))
INITIAL_LOOKBACK_DAYS = int(os.getenv("REAL_WALLET_INITIAL_LOOKBACK_DAYS", "30"))
MAX_PAGES_PER_ENDPOINT = int(os.getenv("REAL_WALLET_MAX_PAGES", "100"))
BACKOFF_SECONDS = (5, 15, 30, 60)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
HTTP = requests.Session()


def _now():
    return datetime.now(UTC)


def _iso(value=None):
    return (value or _now()).astimezone(UTC).isoformat()


def _int(value):
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    value = str(value)
    return int(value, 16) if value.startswith("0x") else int(value)


def _address(value):
    if isinstance(value, dict):
        value = value.get("hash") or value.get("address_hash") or value.get("address")
    return str(value or "").lower()


def _age(value, now=None):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return max(0.0, ((now or _now()) - stamp.astimezone(UTC)).total_seconds())
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


def due(state, now=None):
    age = _age(state.get("lastSuccessfulWalletSyncAt"), now)
    return age is None or age >= SYNC_INTERVAL_SECONDS


def retry_due(state, now=None):
    last = _age(state.get("lastSyncAttemptAt"), now)
    return last is None or last >= int(state.get("walletSyncRetryAfterSeconds") or 0)


def _retry_delay(failures):
    base = BACKOFF_SECONDS[min(max(int(failures) - 1, 0), len(BACKOFF_SECONDS) - 1)]
    return max(1, int(base * random.uniform(0.8, 1.2)))


def _get_json(path, params=None):
    response = HTTP.get(f"{API}{path}", params=params or {}, timeout=(3.0, 12.0),
                        headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise RuntimeError(f"Blockscout returned invalid JSON for {path}")
    return payload


def _fetch_pages(path, params, cursor_block, cutoff, max_pages=MAX_PAGES_PER_ENDPOINT):
    """Fetch newest-first pages until the committed block/time boundary is crossed."""
    rows = []
    page_params = dict(params or {})
    boundary_reached = False
    for _ in range(max_pages):
        payload = _get_json(path, page_params)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError(f"Blockscout pagination contract invalid for {path}")
        items = payload["items"]
        if not items:
            boundary_reached = True
            break
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(f"Blockscout returned malformed row for {path}")
            block = _int(item.get("block_number"))
            stamp = item.get("timestamp") or item.get("block_timestamp")
            if cursor_block is not None and block < int(cursor_block):
                boundary_reached = True
                continue
            if cursor_block is None and cutoff is not None:
                parsed = _parse_timestamp(stamp)
                if parsed is not None and parsed < cutoff:
                    boundary_reached = True
                    continue
            rows.append(item)
        next_params = payload.get("next_page_params")
        if not next_params:
            boundary_reached = True
            break
        if boundary_reached:
            break
        if not isinstance(next_params, dict):
            raise RuntimeError(f"Blockscout next_page_params invalid for {path}")
        page_params = dict(params or {})
        page_params.update(next_params)
    if not boundary_reached:
        raise RuntimeError(f"Blockscout pagination limit reached for {path}; cursor retained")
    return rows


def _parse_timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(UTC) if stamp.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _token_rows(payload):
    if isinstance(payload, dict):
        rows = payload.get("items")
        if rows is None:
            rows = payload.get("token_balances")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise RuntimeError("Blockscout token-balances response is not a list")
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        token = item.get("token") if isinstance(item.get("token"), dict) else item
        address = _address(token) or _address(item)
        value = item.get("value", item.get("balance", item.get("token_balance")))
        decimals = token.get("decimals", item.get("decimals"))
        if not address or value is None or decimals is None:
            raise RuntimeError("Blockscout token balance missing address/value/decimals")
        decimals = int(decimals)
        if not 0 <= decimals <= 36:
            raise RuntimeError(f"Invalid token decimals for {address}")
        result[address] = {
            "raw": str(_int(value)), "decimals": decimals,
            "symbol": str(token.get("symbol") or address[:10]),
        }
    return result


def _calldata_tokens(data):
    data = str(data or "").lower()
    selector = str(cfg.BUY_SELECTOR).lower()
    if not data.startswith(selector):
        return None
    body = data[len(selector):]
    if len(body) < 64 * 8:
        return None
    try:
        words = [body[i * 64:(i + 1) * 64] for i in range(8)]
        token_in = "0x" + words[0][-40:]
        token_out = "0x" + words[1][-40:]
        fee = int(words[2], 16)
        amount_in = int(words[5], 16)
        return token_in, token_out, fee, amount_in
    except (ValueError, IndexError):
        return None


def _receipt_deltas(receipt, wallet):
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    identities = []
    for fallback_index, log in enumerate((receipt or {}).get("logs") or []):
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        token = _address(log.get("address"))
        sender = "0x" + str(topics[1])[-40:].lower()
        recipient = "0x" + str(topics[2])[-40:].lower()
        amount = _int(log.get("data") or "0x0")
        log_index = _int(log.get("logIndex", fallback_index))
        if recipient == wallet and sender != wallet:
            incoming[token] += amount
            identities.append((token, log_index, "IN", amount))
        elif sender == wallet and recipient != wallet:
            outgoing[token] += amount
            identities.append((token, log_index, "OUT", amount))
    return incoming, outgoing, identities


def _network_fee(receipt, tx):
    gas_used = _int((receipt or {}).get("gasUsed"))
    gas_price = _int((receipt or {}).get("effectiveGasPrice") or (tx or {}).get("gasPrice"))
    return Decimal(gas_used * gas_price) / Decimal(10**18)


def _processed_id(tx_hash, log_index, token, direction):
    return f"{str(tx_hash).lower()}:{int(log_index)}:{str(token).lower()}:{direction}"


def _apply_swap(state, tx_hash, tx, receipt, timestamp, wallet, seen):
    if _address(tx.get("to")) != str(cfg.ROUTER_ADDRESS).lower():
        return []
    decoded = _calldata_tokens(tx.get("input") or tx.get("data"))
    if not decoded:
        return []
    token_in, token_out, fee_tier, amount_in_calldata = decoded
    incoming, outgoing, identities = _receipt_deltas(receipt, wallet)
    network_fee = _network_fee(receipt, tx)
    imported = []
    if token_in == str(cfg.WSDA_ADDRESS).lower() and token_out != token_in:
        raw_out = incoming.get(token_out, 0)
        raw_in = outgoing.get(token_in, 0)
        if raw_in <= 0:
            raw_in = _int(tx.get("value"))
        if raw_out <= 0 or raw_in <= 0:
            return []
        decimals = _token_rows_for_decimals(state, token_out)
        quantity = Decimal(raw_out) / (Decimal(10) ** decimals)
        spent = Decimal(raw_in) / Decimal(10**18)
        event = next((x for x in identities if x[0] == token_out and x[2] == "IN"), None)
        log_index = event[1] if event else 0
        identity = _processed_id(tx_hash, log_index, token_out, "BUY")
        if identity in seen:
            return []
        position = state["positions"].get(token_out)
        pending = (state.get("pending_buys") or {}).get(token_out)
        known_bot = any(isinstance(t, dict)
                        and str(t.get("tx") or t.get("tx_hash") or "").lower() == tx_hash.lower()
                        and str(t.get("action") or "").upper() in {"BUY", "BUY_RECONCILED"}
                        for t in state["trades"])
        source = "BOT" if known_bot or isinstance(pending, dict) else "MANUAL"
        if isinstance(position, dict):
            # A second costed lot for the same token is safely aggregated.
            position["remaining_quantity"] = int(position.get("remaining_quantity",
                                                              position.get("token_amount_raw", 0)) or 0) + raw_out
            position["token_amount_raw"] = int(position.get("token_amount_raw") or 0) + raw_out
            position["initial_quantity"] = int(position.get("initial_quantity") or 0) + raw_out
            position["remaining_cost_basis"] = str(
                Decimal(str(position.get("remaining_cost_basis", position.get("amount_sda", 0)) or 0)) + spent)
            position["initial_cost_basis"] = str(
                Decimal(str(position.get("initial_cost_basis", position.get("amount_sda", 0)) or 0)) + spent)
            position.setdefault("additional_buy_txs", []).append(tx_hash)
        else:
            position = {
                "symbol": token_out[:10], "token": token_out, "status": "OPEN",
                "source": source, "manual_imported": source == "MANUAL",
                "sellManagementEnabled": True, "amount_sda": float(spent),
                "initial_cost_basis": str(spent), "remaining_cost_basis": str(spent),
                "sold_cost_basis": "0", "realized_proceeds": "0", "realized_pnl": "0",
                "token_amount_raw": raw_out, "initial_quantity": raw_out,
                "remaining_quantity": raw_out, "confirmed_sold_quantity": 0,
                "decimals": decimals, "opened_at": timestamp, "buy_tx": tx_hash,
                "position_id": identity, "accounting_identity": identity,
                "network_fee_sda": str(network_fee), "entry_gas_sda": str(network_fee),
                "entry_gas_remaining_sda": str(network_fee),
                "execution_price_sda": str(spent / quantity),
                "all_in_cost_price_sda": str((spent + network_fee) / quantity),
                "profit_partial_done": False, "trailing_armed": False,
            }
            state["positions"][token_out] = position
        state["trades"].append({
            "action": "BUY_RECONCILED" if source == "BOT" else "MANUAL_BUY",
            "source": source, "status": "OPEN", "token": token_out,
            "symbol": position.get("symbol"), "tx": tx_hash,
            "instruction_index": log_index, "quantity": str(quantity),
            "quantity_raw": raw_out, "decimals": decimals,
            "invested_sda": str(spent), "cost_basis_sda": str(spent),
            "execution_price_sda": str(spent / quantity),
            "network_fee_sda": str(network_fee), "swap_fee_tier": fee_tier,
            "accounting_identity": identity, "sellManagementEnabled": True, "at": timestamp,
        })
        if source == "MANUAL":
            state.setdefault("manual_imports", []).append({
                "tx": tx_hash, "token": token_out, "event_id": identity, "at": timestamp,
            })
        if isinstance(pending, dict):
            state["pending_buys"].pop(token_out, None)
        seen.add(identity)
        imported.append({"tx": tx_hash, "token": token_out, "source": source,
                         "token_amount_raw": raw_out, "spent_sda": str(spent),
                         "fees_sda": str(network_fee), "execution_price_sda": str(spent / quantity)})
    elif token_out == str(cfg.WSDA_ADDRESS).lower() and token_in != token_out:
        raw_sold = outgoing.get(token_in, 0)
        raw_proceeds = incoming.get(token_out, 0)
        if raw_sold <= 0 or raw_proceeds <= 0:
            return []
        position = state["positions"].get(token_in)
        if not isinstance(position, dict):
            return []
        pending = (state.get("pending_sells") or {}).get(token_in)
        if isinstance(pending, dict):
            pending_hash = str(pending.get("tx_hash") or pending.get("swap_tx") or "").lower()
            if pending_hash == tx_hash.lower():
                return []
        event = next((x for x in identities if x[0] == token_in and x[2] == "OUT"), None)
        log_index = event[1] if event else 0
        identity = _processed_id(tx_hash, log_index, token_in, "SELL")
        if identity in seen:
            return []
        recorded = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
        sold = min(recorded, raw_sold)
        if recorded <= 0 or sold <= 0:
            return []
        basis = Decimal(str(position.get("remaining_cost_basis", position.get("amount_sda", 0)) or 0))
        sold_cost = basis * Decimal(sold) / Decimal(recorded)
        proceeds = Decimal(raw_proceeds) / Decimal(10**18)
        entry_fee_left = Decimal(str(position.get("entry_gas_remaining_sda",
                                                  position.get("network_fee_sda", 0)) or 0))
        entry_fee_sold = entry_fee_left * Decimal(sold) / Decimal(recorded)
        net_pnl = proceeds - sold_cost - network_fee - entry_fee_sold
        remaining = recorded - sold
        position["remaining_quantity"] = remaining
        position["confirmed_sold_quantity"] = int(position.get("confirmed_sold_quantity") or 0) + sold
        position["remaining_cost_basis"] = str(max(Decimal(0), basis - sold_cost))
        position["sold_cost_basis"] = str(Decimal(str(position.get("sold_cost_basis") or 0)) + sold_cost)
        position["realized_proceeds"] = str(Decimal(str(position.get("realized_proceeds") or 0)) + proceeds)
        position["realized_pnl"] = str(Decimal(str(position.get("realized_pnl") or 0)) + net_pnl)
        position["entry_gas_remaining_sda"] = str(max(Decimal(0), entry_fee_left - entry_fee_sold))
        position["wallet_available_quantity"] = remaining
        if remaining == 0:
            state["positions"].pop(token_in, None)
        state["daily_realized_pnl_sda"] = float(state.get("daily_realized_pnl_sda") or 0) + float(net_pnl)
        state["trades"].append({
            "action": "MANUAL_SELL", "source": "MANUAL", "status": "CONFIRMED",
            "token": token_in, "symbol": position.get("symbol"), "tx": tx_hash,
            "instruction_index": log_index, "sold_quantity_raw": sold,
            "cost_basis_sda": str(sold_cost), "proceeds_sda": str(proceeds),
            "network_fee_sda": str(network_fee), "entry_network_fee_sda": str(entry_fee_sold),
            "net_pnl_sda": str(net_pnl), "swap_fee_tier": fee_tier,
            "accounting_identity": identity, "at": timestamp,
        })
        seen.add(identity)
        imported.append({"tx": tx_hash, "token": token_in, "source": "MANUAL_SELL",
                         "sold_quantity_raw": sold, "proceeds_sda": str(proceeds),
                         "realized_pnl_sda": str(net_pnl)})
    return imported


def _token_rows_for_decimals(state, token):
    balances = (state.get("walletBalances") or {}).get("tokens") or {}
    if token in balances:
        return int(balances[token]["decimals"])
    metadata = state.get("_liveTokenMetadata") or {}
    if token in metadata:
        return int(metadata[token]["decimals"])
    raise RuntimeError(f"Token decimals unavailable for discovered token {token}")


def sync_once(real_trader, *, now=None):
    """One incremental Explorer+RPC sync. All state changes are committed together."""
    now = now or _now()
    attempted = _iso(now)
    lock_handle = LOCK_FILE.open("a+")
    with lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "syncStatus": "IN_PROGRESS",
                    "syncError": "live wallet sync already running", "lastSyncAttemptAt": attempted}
        try:
            state = real_trader.load_state()
            wallet = str(real_trader.wallet_address() or "").lower()
            if not wallet:
                raise RuntimeError("REAL wallet address unavailable")
            if int(real_trader.chain_id()) != cfg.CHAIN_ID:
                raise RuntimeError("RPC chain ID mismatch")
            cursor_block = state.get("lastProcessedBlock")
            cutoff = now - timedelta(days=max(1, INITIAL_LOOKBACK_DAYS)) if cursor_block is None else None
            tx_path = f"/addresses/{wallet}/transactions"
            transfer_path = f"/addresses/{wallet}/token-transfers"
            transactions = []
            for direction in ("from", "to"):
                transactions.extend(_fetch_pages(tx_path, {"filter": direction}, cursor_block, cutoff))
            transfers = _fetch_pages(transfer_path, {"type": "ERC-20"}, cursor_block, cutoff)

            # The token-balance response is a complete current snapshot, not a delta.
            token_payload = _get_json(f"/addresses/{wallet}/token-balances")
            token_balances = _token_rows(token_payload)
            native_balance = int(real_trader.native_balance(wallet))
            latest_block = int(cursor_block or 0)
            latest_timestamp = None
            tx_hashes = set()
            for row in transactions + transfers:
                latest_block = max(latest_block, _int(row.get("block_number")))
                stamp = _parse_timestamp(row.get("timestamp") or row.get("block_timestamp"))
                if stamp and (latest_timestamp is None or stamp > latest_timestamp):
                    latest_timestamp = stamp
                tx_hash = row.get("hash") or row.get("transaction_hash")
                if tx_hash:
                    tx_hashes.add(str(tx_hash).lower())

            staged = dict(state)
            staged["positions"] = {str(k).lower(): dict(v) for k, v in (state.get("positions") or {}).items()}
            staged["trades"] = list(state.get("trades") or [])
            staged["manual_imports"] = list(state.get("manual_imports") or [])
            staged["pending_buys"] = dict(state.get("pending_buys") or {})
            staged["pending_sells"] = dict(state.get("pending_sells") or {})
            staged["walletBalances"] = {
                "native_sda_wei": str(native_balance),
                "tokens": token_balances, "wallet": wallet, "chain_id": cfg.CHAIN_ID,
                "checked_at": attempted,
            }
            staged["_liveTokenMetadata"] = {k: {"decimals": v["decimals"]} for k, v in token_balances.items()}
            seen = set(state.get("processedBlockchainEvents") or [])
            imported = []
            transaction_map = {}
            for row in transactions:
                tx_hash = str(row.get("hash") or row.get("transaction_hash") or "").lower()
                if tx_hash:
                    transaction_map[tx_hash] = row
            for tx_hash in sorted(tx_hashes):
                if not any(item.startswith(tx_hash + ":") for item in seen):
                    tx = real_trader.rpc("eth_getTransactionByHash", [tx_hash])
                    receipt = real_trader.rpc("eth_getTransactionReceipt", [tx_hash])
                    if not isinstance(tx, dict) or not isinstance(receipt, dict):
                        raise RuntimeError(f"transaction or receipt unavailable for {tx_hash}")
                    if _int(receipt.get("status")) != 1:
                        continue
                    row = transaction_map.get(tx_hash, {})
                    timestamp = row.get("timestamp") or row.get("block_timestamp")
                    if not timestamp:
                        # A token-transfer row supplies the same confirmed block timestamp.
                        timestamp = next((x.get("timestamp") for x in transfers
                                          if str(x.get("transaction_hash") or "").lower() == tx_hash), None)
                    parsed = _parse_timestamp(timestamp)
                    if parsed is None:
                        raise RuntimeError(f"transaction timestamp unavailable for {tx_hash}")
                    imported.extend(_apply_swap(staged, tx_hash, tx, receipt, parsed.isoformat(),
                                                wallet, seen))
            # Preserve decimals for all known open tokens even if an explorer omits zero balances.
            for token in staged["positions"]:
                if token not in token_balances:
                    decimals = real_trader.token_decimals(token)
                    token_balances[token] = {"raw": "0", "decimals": decimals, "symbol": token[:10]}
            # Confirm that the explorer token snapshot and RPC native balance are usable.
            if any(not isinstance(v, dict) or int(v.get("raw", -1)) < 0 for v in token_balances.values()):
                raise RuntimeError("wallet token-balance snapshot failed validation")
            synced_at = _iso(now)
            staged.pop("_liveTokenMetadata", None)
            staged["lastProcessedBlock"] = latest_block
            staged["lastProcessedSignature"] = max(tx_hashes) if tx_hashes else state.get("lastProcessedSignature")
            staged["lastBlockchainDataAt"] = _iso(latest_timestamp) if latest_timestamp else synced_at
            staged["lastSyncAttemptAt"] = attempted
            staged["lastSuccessfulWalletSyncAt"] = synced_at
            staged["lastSuccessfulWalletSync"] = synced_at
            staged["lastSuccessfulReconciliationAt"] = synced_at
            staged["lastSuccessfulReconciliation"] = synced_at
            staged["ledgerUpdatedAt"] = synced_at
            staged["walletSyncSource"] = "sidra_blockscout_v2_plus_rpc"
            staged["walletSyncFailures"] = 0
            staged["walletSyncRetryAfterSeconds"] = 0
            staged["lastSyncError"] = None
            staged["syncStatus"] = "FRESH"
            staged["walletSyncHealthy"] = True
            staged["reconciliationHealthy"] = True
            staged["buyDataHealthy"] = True
            staged["sellDataHealthy"] = True
            staged["processedBlockchainEvents"] = sorted(seen)[-50000:]
            staged["updated_at"] = synced_at
            real_trader.save_state(staged)
            return {"ok": True, "syncStatus": "FRESH", "lastSyncAttemptAt": attempted,
                    "lastSuccessfulWalletSyncAt": synced_at,
                    "lastSuccessfulReconciliationAt": synced_at,
                    "lastBlockchainDataAt": staged["lastBlockchainDataAt"],
                    "imported": imported, "processed_transactions": len(tx_hashes),
                    "walletBalances": staged["walletBalances"]}
        except Exception as exc:
            try:
                failed = real_trader.load_state()
                failures = int(failed.get("walletSyncFailures") or 0) + 1
                failed["walletSyncFailures"] = failures
                failed["lastSyncAttemptAt"] = attempted
                failed["lastSyncError"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                failed["syncStatus"] = "FAILED"
                failed["walletSyncHealthy"] = False
                failed["reconciliationHealthy"] = False
                failed["buyDataHealthy"] = False
                failed["walletSyncRetryAfterSeconds"] = _retry_delay(failures)
                real_trader.save_state(failed)
            except Exception:
                pass
            return {"ok": False, "syncStatus": "FAILED",
                    "syncError": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "lastSyncAttemptAt": attempted}
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
