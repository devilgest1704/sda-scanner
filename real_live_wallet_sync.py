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
MAX_INDEX_LAG_BLOCKS = int(os.getenv("REAL_WALLET_MAX_INDEX_LAG_BLOCKS", "20"))
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
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            return None
        return max(0.0, ((now or _now()) - stamp.astimezone(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def freshness(state, now=None):
    age = _age(state.get("lastSuccessfulWalletSyncAt"), now)
    status = str(state.get("syncStatus") or "").upper()
    if (age is None or age > DEGRADED_SECONDS
            or status in {"FAILED", "STALE", "IN_PROGRESS"}
            or state.get("walletSyncHealthy") is not True
            or state.get("reconciliationHealthy") is not True):
        return "STALE", age
    if age > FRESH_SECONDS or status == "DEGRADED":
        return "DEGRADED", age
    return "FRESH", age


def due(state, now=None):
    status = str(state.get("syncStatus") or "").upper()
    if status in {"FAILED", "STALE", "DEGRADED", "IN_PROGRESS"}:
        return True
    if state.get("walletSyncHealthy") is not True or state.get("reconciliationHealthy") is not True:
        return True
    age = _age(state.get("lastSuccessfulWalletSyncAt"), now)
    return age is None or age >= SYNC_INTERVAL_SECONDS


def retry_due(state, now=None):
    last = _age(state.get("lastSyncAttemptAt"), now)
    return last is None or last >= int(state.get("walletSyncRetryAfterSeconds") or 0)


def _retry_delay(failures):
    base = BACKOFF_SECONDS[min(max(int(failures) - 1, 0), len(BACKOFF_SECONDS) - 1)]
    return min(60, max(1, int(base * random.uniform(0.8, 1.2))))


def _get_json(path, params=None):
    response = HTTP.get(f"{API}{path}", params=params or {}, timeout=(3.0, 12.0),
                        headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise TypeError(f"Blockscout returned invalid JSON for {path}")
    return payload


def _fetch_pages(path, params, cursor_block, cutoff, max_pages=MAX_PAGES_PER_ENDPOINT):
    """Fetch newest-first pages until the committed block/time boundary is crossed."""
    rows = []
    page_params = dict(params or {})
    boundary_reached = False
    for _ in range(max_pages):
        payload = _get_json(path, page_params)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise TypeError(f"Blockscout pagination contract invalid for {path}")
        items = payload["items"]
        if not items:
            boundary_reached = True
            break
        for item in items:
            if not isinstance(item, dict):
                raise TypeError(f"Blockscout returned malformed row for {path}")
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
            raise TypeError(f"Blockscout next_page_params invalid for {path}")
        page_params = dict(params or {})
        page_params.update(next_params)
    if not boundary_reached:
        raise RuntimeError(f"Blockscout pagination limit reached for {path}; cursor retained")
    return rows


def _parse_timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value))
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
        raise TypeError("Blockscout token-balances response is not a list")
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        token = item.get("token") if isinstance(item.get("token"), dict) else item
        address = _address(token) or _address(item)
        value = item.get("value", item.get("balance", item.get("token_balance")))
        decimals = token.get("decimals", item.get("decimals"))
        if not address or value is None or decimals is None:
            raise TypeError("Blockscout token balance missing address/value/decimals")
        decimals = int(decimals)
        if not 0 <= decimals <= 36:
            raise TypeError(f"Invalid token decimals for {address}")
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


def _reconcile_unresolved_manual_sell(state, tx_hash, token, raw_sold, raw_proceeds,
                                       network_fee, fee_tier, timestamp, resulting_balance_raw,
                                       decimals, identity, seen):
    """Attach on-chain proceeds to a prior wallet-balance-only disposal.

    The older runtime may already have removed the sold quantity from positions.
    In that case update the unresolved MANUAL_SELL ledger row in place; never
    reduce the position or add a second PnL event.
    """
    disposals = state.get("manual_disposals") or []
    candidates = []
    tx_time = _parse_timestamp(timestamp)
    for disposal in disposals:
        if not isinstance(disposal, dict):
            continue
        if str(disposal.get("token") or "").lower() != token:
            continue
        if str(disposal.get("status") or "").upper() != "PROCEEDS_UNKNOWN":
            continue
        if _int(disposal.get("quantity_raw")) != raw_sold:
            continue
        disposal_time = _parse_timestamp(disposal.get("at"))
        delta = abs((tx_time - disposal_time).total_seconds()) if tx_time and disposal_time else 10**12
        if delta <= 24 * 60 * 60:
            candidates.append((delta, disposal))
    candidates.sort(key=lambda item: item[0])
    disposal = candidates[0][1] if candidates else None

    trades = state.get("trades") or []
    trade_candidates = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if str(trade.get("action") or "").upper() != "MANUAL_SELL":
            continue
        if str(trade.get("token") or "").lower() != token:
            continue
        if _int(trade.get("sold_quantity_raw")) != raw_sold:
            continue
        if str(trade.get("accounting_status") or "").upper() != "PROCEEDS_UNKNOWN":
            continue
        if trade.get("tx"):
            continue
        trade_time = _parse_timestamp(trade.get("at"))
        delta = abs((tx_time - trade_time).total_seconds()) if tx_time and trade_time else 10**12
        if delta <= 24 * 60 * 60:
            trade_candidates.append((delta, trade))
    if not trade_candidates:
        if disposal is None:
            return None
        raise RuntimeError(
            f"unresolved manual disposal for {token} has no matching cost-basis ledger row"
        )
    trade_candidates.sort(key=lambda item: item[0])
    trade = trade_candidates[0][1]
    cost_basis = Decimal(str(trade.get("cost_basis_sda") or disposal.get("cost_basis_sda") or 0))
    if cost_basis < 0:
        raise RuntimeError(f"manual disposal cost basis is invalid for {token}")
    quantity = Decimal(raw_sold) / (Decimal(10) ** decimals)
    proceeds = Decimal(raw_proceeds) / Decimal(10**18)
    entry_fee = Decimal(str(trade.get("entry_network_fee_sda") or 0))
    fees = network_fee + entry_fee
    net_pnl = proceeds - cost_basis - fees
    execution_price = proceeds / quantity if quantity else Decimal("0")
    buy_trade = None
    for candidate in trades:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("action") or "").upper() not in {"BUY", "BUY_RECONCILED", "MANUAL_BUY"}:
            continue
        if str(candidate.get("token") or "").lower() != token:
            continue
        if candidate.get("tx"):
            candidate_time = _parse_timestamp(candidate.get("at"))
            if candidate_time and tx_time and candidate_time <= tx_time:
                if buy_trade is None or candidate_time > _parse_timestamp(buy_trade.get("at")):
                    buy_trade = candidate
    trade.update({
        "tx": tx_hash, "source": "MANUAL", "status": "CONFIRMED",
        "buy_tx": trade.get("buy_tx") or (buy_trade or {}).get("tx"),
        "proceeds_sda": str(proceeds), "network_fee_sda": str(network_fee),
        "entry_network_fee_sda": str(entry_fee), "fees_sda": str(fees),
        "execution_price_sda": str(execution_price), "net_pnl_sda": str(net_pnl),
        "realized_pnl_sda": str(net_pnl), "accounting_status": "CONFIRMED",
        "resulting_token_balance_raw": resulting_balance_raw, "at": timestamp,
        "swap_fee_tier": fee_tier,
    })
    if disposal is not None:
        disposal.update({
            "tx": tx_hash, "status": "RECONCILED", "proceeds_sda": str(proceeds),
            "network_fee_sda": str(network_fee), "fees_sda": str(fees),
            "realized_pnl_sda": str(net_pnl), "execution_price_sda": str(execution_price),
            "accounting_status": "CONFIRMED", "resulting_token_balance_raw": resulting_balance_raw,
            "at": timestamp, "reconciled_at": _iso(),
        })
    seen.add(identity)
    # Only a disposal executed today contributes to the daily risk counter.
    if tx_time and tx_time.date() == _now().date():
        state["daily_realized_pnl_sda"] = float(
            Decimal(str(state.get("daily_realized_pnl_sda") or 0)) + net_pnl
        )
    state.setdefault("manual_sell_reconciliations", []).append({
        "tx": tx_hash, "token": token, "quantity_raw": raw_sold,
        "proceeds_sda": str(proceeds), "realized_pnl_sda": str(net_pnl),
        "event_id": identity, "at": timestamp,
    })
    return {"tx": tx_hash, "token": token, "source": "MANUAL_SELL_RECONCILED",
            "sold_quantity_raw": raw_sold, "cost_basis_sda": str(cost_basis),
            "proceeds_sda": str(proceeds), "fees_sda": str(fees),
            "realized_pnl_sda": str(net_pnl)}


def _apply_swap(state, tx_hash, tx, receipt, timestamp, wallet, seen, real_trader):
    incoming, outgoing, identities = _receipt_deltas(receipt, wallet)
    if _address(tx.get("to")) != str(cfg.ROUTER_ADDRESS).lower():
        wsda = str(cfg.WSDA_ADDRESS).lower()
        non_wsda_in = any(token != wsda and amount > 0 for token, amount in incoming.items())
        non_wsda_out = any(token != wsda and amount > 0 for token, amount in outgoing.items())
        if ((outgoing.get(wsda, 0) > 0 and non_wsda_in)
                or (incoming.get(wsda, 0) > 0 and non_wsda_out)):
            raise RuntimeError(
                f"wallet swap used an unconfigured router {tx.get('to')}; "
                "manual accounting is blocked until router is verified"
            )
        return []
    decoded = _calldata_tokens(tx.get("input") or tx.get("data"))
    if not decoded:
        tracked = set(state["positions"])
        touched = (set(incoming) | set(outgoing)) - {str(cfg.WSDA_ADDRESS).lower()}
        if tracked.intersection(touched) or touched:
            raise RuntimeError(f"unsupported router swap encoding in confirmed transaction {tx_hash}")
        return []
    token_in, token_out, fee_tier, amount_in_calldata = decoded
    if token_in != str(cfg.WSDA_ADDRESS).lower() and token_out != str(cfg.WSDA_ADDRESS).lower():
        raise RuntimeError(f"token-to-token swap requires SDA valuation before cursor commit: {tx_hash}")
    network_fee = _network_fee(receipt, tx)
    imported = []
    if token_in == str(cfg.WSDA_ADDRESS).lower() and token_out != token_in:
        raw_out = incoming.get(token_out, 0)
        transfer_spend = max(0, outgoing.get(token_in, 0) - incoming.get(token_in, 0))
        native_spend = _int(tx.get("value"))
        if transfer_spend and native_spend:
            if transfer_spend == amount_in_calldata:
                raw_in = transfer_spend
            elif native_spend == amount_in_calldata:
                raw_in = native_spend
            elif transfer_spend + native_spend == amount_in_calldata:
                raw_in = transfer_spend + native_spend
            else:
                raise RuntimeError(f"confirmed BUY swap {tx_hash} has ambiguous native/token input")
        else:
            raw_in = transfer_spend or native_spend
        if (raw_out <= 0 or raw_in <= 0
                or (amount_in_calldata > 0 and raw_in != amount_in_calldata)):
            raise RuntimeError(f"confirmed BUY swap {tx_hash} lacks exact wallet transfer evidence")
        decimals = _token_rows_for_decimals(state, token_out, real_trader)
        quantity = Decimal(raw_out) / (Decimal(10) ** decimals)
        spent = Decimal(raw_in) / Decimal(10**18)
        resulting_balance_raw = _int(
            ((state.get("walletBalances") or {}).get("tokens") or {}).get(token_out, {}).get("raw")
        )
        resulting_balance = Decimal(resulting_balance_raw) / (Decimal(10) ** decimals)
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
            # Legacy positions may predate transaction-level idempotence. Treat
            # swaps at/before their recorded opening as already represented;
            # only later confirmed buys may add a new lot.
            opened_at = _parse_timestamp(position.get("opened_at"))
            transaction_at = _parse_timestamp(timestamp)
            if opened_at is None or transaction_at is None:
                raise RuntimeError(f"cannot establish lot chronology for existing {token_out} position")
            if transaction_at <= opened_at:
                seen.add(identity)
                return []
            old_remaining = int(position.get("remaining_quantity",
                                             position.get("token_amount_raw", 0)) or 0)
            old_cost = Decimal(str(position.get("remaining_cost_basis",
                                                position.get("amount_sda", 0)) or 0))
            old_entry_fee = Decimal(str(position.get("entry_gas_remaining_sda",
                                                      position.get("network_fee_sda", 0)) or 0))
            lots = position.get("cost_lots")
            if not isinstance(lots, list):
                lots = ([{"tx": position.get("buy_tx"), "at": position.get("opened_at"),
                          "quantity_raw": old_remaining, "remaining_quantity_raw": old_remaining,
                          "cost_basis_sda": str(old_cost), "remaining_cost_basis_sda": str(old_cost),
                          "entry_fee_sda": str(old_entry_fee),
                          "remaining_entry_fee_sda": str(old_entry_fee)}] if old_remaining > 0 else [])
            lots.append({"tx": tx_hash, "at": timestamp, "quantity_raw": raw_out,
                         "remaining_quantity_raw": raw_out, "cost_basis_sda": str(spent),
                         "remaining_cost_basis_sda": str(spent), "entry_fee_sda": str(network_fee),
                         "remaining_entry_fee_sda": str(network_fee)})
            position["cost_lots"] = lots
            position["remaining_quantity"] = old_remaining + raw_out
            position["remaining_cost_basis"] = str(sum(
                (Decimal(str(lot.get("remaining_cost_basis_sda") or 0)) for lot in lots), Decimal("0")))
            position["entry_gas_remaining_sda"] = str(sum(
                (Decimal(str(lot.get("remaining_entry_fee_sda") or 0)) for lot in lots), Decimal("0")))
            position["token_amount_raw"] = int(position.get("token_amount_raw") or 0) + raw_out
            position["initial_quantity"] = int(position.get("initial_quantity") or 0) + raw_out
            position["token_amount"] = str(
                Decimal(str(position.get("token_amount") or 0)) + quantity)
            position["spent_sda"] = str(Decimal(str(position.get("spent_sda") or 0)) + spent)
            position["initial_cost_basis"] = str(
                Decimal(str(position.get("initial_cost_basis", position.get("amount_sda", 0)) or 0)) + spent)
            position["amount_sda"] = float(Decimal(str(position["initial_cost_basis"])))
            position["network_fee_sda"] = str(Decimal(str(position.get("network_fee_sda") or 0)) + network_fee)
            position["entry_gas_sda"] = str(Decimal(str(position.get("entry_gas_sda") or 0)) + network_fee)
            total_quantity = Decimal(int(position["initial_quantity"])) / (Decimal(10) ** decimals)
            position["execution_price_sda"] = str(Decimal(str(position["initial_cost_basis"])) / total_quantity)
            position["all_in_cost_price_sda"] = str(
                (Decimal(str(position["initial_cost_basis"])) + Decimal(str(position["entry_gas_sda"]))) / total_quantity)
            position.setdefault("additional_buy_txs", []).append(tx_hash)
            position["resulting_token_balance_raw"] = resulting_balance_raw
            position["resulting_token_balance"] = str(resulting_balance)
            if source == "MANUAL":
                position["sellManagementEnabled"] = True
        else:
            position = {
                "symbol": token_out[:10], "token": token_out, "status": "OPEN",
                "source": source, "manual_imported": source == "MANUAL",
                "sellManagementEnabled": True, "amount_sda": float(spent),
                "initial_cost_basis": str(spent), "remaining_cost_basis": str(spent),
                "sold_cost_basis": "0", "realized_proceeds": "0", "realized_pnl": "0",
                "token_amount_raw": raw_out, "token_amount": str(quantity),
                "initial_quantity": raw_out, "remaining_quantity": raw_out,
                "resulting_token_balance_raw": resulting_balance_raw,
                "resulting_token_balance": str(resulting_balance),
                "spent_sda": str(spent), "confirmed_sold_quantity": 0,
                "cost_lots": [{"tx": tx_hash, "at": timestamp, "quantity_raw": raw_out,
                               "remaining_quantity_raw": raw_out, "cost_basis_sda": str(spent),
                               "remaining_cost_basis_sda": str(spent), "entry_fee_sda": str(network_fee),
                               "remaining_entry_fee_sda": str(network_fee)}],
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
            "token_amount": str(quantity), "quantity_raw": raw_out, "decimals": decimals,
            "invested_sda": str(spent), "spent_sda": str(spent), "cost_basis_sda": str(spent),
            "resulting_token_balance_raw": resulting_balance_raw,
            "resulting_token_balance": str(resulting_balance),
            "execution_price_sda": str(spent / quantity),
            "network_fee_sda": str(network_fee), "swap_fee_tier": fee_tier,
            "accounting_identity": identity, "sellManagementEnabled": True, "at": timestamp,
        })
        if source == "MANUAL":
            imports = state.setdefault("manual_imports", [])
            if not any(isinstance(item, dict) and item.get("event_id") == identity for item in imports):
                imports.append({"tx": tx_hash, "token": token_out, "event_id": identity, "at": timestamp,
                                "token_amount": str(quantity), "spent_sda": str(spent),
                                "fees_sda": str(network_fee), "execution_price_sda": str(spent / quantity),
                                "resulting_token_balance": str(resulting_balance)})
        if isinstance(pending, dict):
            state["pending_buys"].pop(token_out, None)
        seen.add(identity)
        imported.append({"tx": tx_hash, "token": token_out, "source": source,
                         "token_amount_raw": raw_out, "token_amount": str(quantity),
                         "spent_sda": str(spent), "fees_sda": str(network_fee),
                         "execution_price_sda": str(spent / quantity),
                         "resulting_token_balance_raw": resulting_balance_raw,
                         "resulting_token_balance": str(resulting_balance)})
    elif token_out == str(cfg.WSDA_ADDRESS).lower() and token_in != token_out:
        raw_sold = outgoing.get(token_in, 0)
        raw_proceeds = max(0, incoming.get(token_out, 0) - outgoing.get(token_out, 0))
        if (raw_sold <= 0 or raw_proceeds <= 0
                or (amount_in_calldata > 0 and raw_sold != amount_in_calldata)):
            raise RuntimeError(f"confirmed SELL swap {tx_hash} lacks exact wallet transfer evidence")
        resulting_balance_raw = _int(
            ((state.get("walletBalances") or {}).get("tokens") or {}).get(token_in, {}).get("raw")
        )
        decimals = _token_rows_for_decimals(state, token_in, real_trader)
        reconciled = _reconcile_unresolved_manual_sell(
            state, tx_hash, token_in, raw_sold, raw_proceeds, _gas_fee(receipt, tx),
            fee_tier, timestamp, resulting_balance_raw, decimals, identity, seen,
        )
        if reconciled is not None:
            imported.append(reconciled)
            return imported
        position = state["positions"].get(token_in)
        if not isinstance(position, dict):
            raise RuntimeError(f"confirmed SELL swap {tx_hash} has no tracked cost basis for {token_in}")
        event = next((x for x in identities if x[0] == token_in and x[2] == "OUT"), None)
        log_index = event[1] if event else 0
        identity = _processed_id(tx_hash, log_index, token_in, "SELL")
        pending = (state.get("pending_sells") or {}).get(token_in)
        if isinstance(pending, dict):
            pending_hash = str(pending.get("tx_hash") or pending.get("swap_tx") or "").lower()
            if pending_hash == tx_hash.lower():
                seen.add(identity)
                return []
            if pending.get("stage"):
                raise RuntimeError(
                    f"manual SELL discovery for {token_in} deferred while automatic SELL intent is active"
                )
        if identity in seen:
            return []
        recorded = int(position.get("remaining_quantity", position.get("token_amount_raw", 0)) or 0)
        sold = min(recorded, raw_sold)
        if recorded <= 0 or sold <= 0:
            raise RuntimeError(f"confirmed SELL swap {tx_hash} has no remaining tracked quantity")
        basis = Decimal(str(position.get("remaining_cost_basis", position.get("amount_sda", 0)) or 0))
        lots = position.get("cost_lots")
        if not isinstance(lots, list):
            legacy_fee = Decimal(str(position.get("entry_gas_remaining_sda",
                                                  position.get("network_fee_sda", 0)) or 0))
            lots = [{"tx": position.get("buy_tx"), "at": position.get("opened_at"),
                     "quantity_raw": recorded, "remaining_quantity_raw": recorded,
                     "cost_basis_sda": str(basis), "remaining_cost_basis_sda": str(basis),
                     "entry_fee_sda": str(legacy_fee), "remaining_entry_fee_sda": str(legacy_fee)}]
        remaining_to_allocate = sold
        sold_cost = Decimal("0")
        entry_fee_sold = Decimal("0")
        for lot in lots:
            lot_remaining = int(lot.get("remaining_quantity_raw") or 0)
            if lot_remaining <= 0 or remaining_to_allocate <= 0:
                continue
            take = min(lot_remaining, remaining_to_allocate)
            fraction = Decimal(take) / Decimal(lot_remaining)
            lot_cost = Decimal(str(lot.get("remaining_cost_basis_sda") or 0))
            lot_fee = Decimal(str(lot.get("remaining_entry_fee_sda") or 0))
            cost_part = lot_cost * fraction
            fee_part = lot_fee * fraction
            lot["remaining_quantity_raw"] = lot_remaining - take
            lot["remaining_cost_basis_sda"] = str(max(Decimal("0"), lot_cost - cost_part))
            lot["remaining_entry_fee_sda"] = str(max(Decimal("0"), lot_fee - fee_part))
            sold_cost += cost_part
            entry_fee_sold += fee_part
            remaining_to_allocate -= take
        if remaining_to_allocate:
            raise RuntimeError(f"SELL {tx_hash} exceeds tracked FIFO lot quantities")
        position["cost_lots"] = [lot for lot in lots if int(lot.get("remaining_quantity_raw") or 0) > 0]
        position["remaining_cost_basis"] = str(sum(
            (Decimal(str(lot.get("remaining_cost_basis_sda") or 0)) for lot in position["cost_lots"]),
            Decimal("0")))
        position["entry_gas_remaining_sda"] = str(sum(
            (Decimal(str(lot.get("remaining_entry_fee_sda") or 0)) for lot in position["cost_lots"]),
            Decimal("0")))
        proceeds_total = Decimal(raw_proceeds) / Decimal(10**18)
        sold_fraction = Decimal(sold) / Decimal(raw_sold)
        proceeds = proceeds_total * sold_fraction
        network_fee_allocated = network_fee * sold_fraction
        net_pnl = proceeds - sold_cost - network_fee_allocated - entry_fee_sold
        remaining = recorded - sold
        position["remaining_quantity"] = remaining
        position["confirmed_sold_quantity"] = int(position.get("confirmed_sold_quantity") or 0) + sold
        position["sold_cost_basis"] = str(Decimal(str(position.get("sold_cost_basis") or 0)) + sold_cost)
        position["realized_proceeds"] = str(Decimal(str(position.get("realized_proceeds") or 0)) + proceeds)
        position["realized_pnl"] = str(Decimal(str(position.get("realized_pnl") or 0)) + net_pnl)
        position["wallet_available_quantity"] = remaining
        position["resulting_token_balance_raw"] = resulting_balance_raw
        position["resulting_token_balance"] = str(
            Decimal(resulting_balance_raw) / (Decimal(10) ** int(position.get("decimals") or 18)))
        if remaining == 0:
            state["positions"].pop(token_in, None)
        state["daily_realized_pnl_sda"] = float(state.get("daily_realized_pnl_sda") or 0) + float(net_pnl)
        state["trades"].append({
            "action": "MANUAL_SELL", "source": "MANUAL", "status": "CONFIRMED",
            "token": token_in, "symbol": position.get("symbol"), "tx": tx_hash,
            "buy_tx": position.get("buy_tx"),
            "instruction_index": log_index, "sold_quantity_raw": sold,
            "token_amount": str(Decimal(sold) / (Decimal(10) ** int(position.get("decimals") or 18))),
            "resulting_token_balance_raw": resulting_balance_raw,
            "cost_basis_sda": str(sold_cost), "proceeds_sda": str(proceeds),
            "execution_price_sda": str(proceeds / (Decimal(sold) / (Decimal(10) ** int(position.get("decimals") or 18)))),
            "network_fee_sda": str(network_fee_allocated), "entry_network_fee_sda": str(entry_fee_sold),
            "fees_sda": str(network_fee_allocated + entry_fee_sold),
            "net_pnl_sda": str(net_pnl), "swap_fee_tier": fee_tier,
            "accounting_identity": identity, "at": timestamp,
        })
        seen.add(identity)
        imported.append({"tx": tx_hash, "token": token_in, "source": "MANUAL_SELL",
                         "sold_quantity_raw": sold, "proceeds_sda": str(proceeds),
                         "network_fee_sda": str(network_fee_allocated),
                         "entry_network_fee_sda": str(entry_fee_sold),
                         "realized_pnl_sda": str(net_pnl)})
    return imported


def _token_rows_for_decimals(state, token, real_trader):
    balances = (state.get("walletBalances") or {}).get("tokens") or {}
    if token in balances:
        return int(balances[token]["decimals"])
    metadata = state.get("_liveTokenMetadata") or {}
    if token in metadata:
        return int(metadata[token]["decimals"])
    return int(real_trader.token_decimals(token))


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
            latest_rpc_block = _int(real_trader.rpc("eth_blockNumber", []))
            block_payload = _get_json("/blocks")
            block_items = block_payload.get("items") if isinstance(block_payload, dict) else None
            if not isinstance(block_items, list) or not block_items:
                raise TypeError("Blockscout latest-block index unavailable")
            latest_explorer_block = _int(
                block_items[0].get("height", block_items[0].get("number",
                    block_items[0].get("block_number")))
            )
            index_lag = latest_rpc_block - latest_explorer_block
            if index_lag < 0 or index_lag > MAX_INDEX_LAG_BLOCKS:
                raise RuntimeError(
                    f"Blockscout index lag {index_lag} blocks "
                    f"(RPC={latest_rpc_block}, explorer={latest_explorer_block})"
                )
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
            tx_order = {}
            for row in transactions + transfers:
                block_number = _int(row.get("block_number"))
                latest_block = max(latest_block, block_number)
                stamp = _parse_timestamp(row.get("timestamp") or row.get("block_timestamp"))
                if stamp and (latest_timestamp is None or stamp > latest_timestamp):
                    latest_timestamp = stamp
                tx_hash = row.get("hash") or row.get("transaction_hash")
                if tx_hash:
                    tx_hash = str(tx_hash).lower()
                    tx_hashes.add(tx_hash)
            # The address-transactions endpoint supplies transaction_index;
            # token-transfer index is a log index and must not reorder swaps.
            for row in transactions:
                tx_hash = str(row.get("hash") or row.get("transaction_hash") or "").lower()
                if tx_hash:
                    tx_order[tx_hash] = (
                        _int(row.get("block_number")),
                        _int(row.get("transaction_index", row.get("index", 0))),
                        str(row.get("timestamp") or row.get("block_timestamp") or ""),
                    )
            for tx_hash in tx_hashes:
                tx_order.setdefault(tx_hash, (0, 0, ""))

            staged = dict(state)
            staged["positions"] = {str(k).lower(): dict(v) for k, v in (state.get("positions") or {}).items()}
            staged["trades"] = list(state.get("trades") or [])
            staged["manual_imports"] = list(state.get("manual_imports") or [])
            staged["pending_buys"] = dict(state.get("pending_buys") or {})
            staged["pending_sells"] = dict(state.get("pending_sells") or {})
            staged["walletBalances"] = {
                "native_sda_wei": str(native_balance),
                "tokens": token_balances, "wallet": wallet, "chain_id": cfg.CHAIN_ID,
                "rpc_latest_block": latest_rpc_block,
                "explorer_latest_block": latest_explorer_block,
                "explorer_index_lag_blocks": index_lag,
                "checked_at": attempted,
            }
            staged["_liveTokenMetadata"] = {k: {"decimals": v["decimals"]} for k, v in token_balances.items()}
            seen = set(state.get("processedBlockchainEvents") or [])
            processed_signatures = set(state.get("processedBlockchainSignatures") or [])
            known_trade_signatures = set()
            for trade in staged["trades"]:
                if not isinstance(trade, dict):
                    continue
                action = str(trade.get("action") or "").upper()
                tx = str(trade.get("tx") or trade.get("tx_hash") or "").lower()
                if tx and action in {"BUY", "BUY_RECONCILED", "SELL", "MANUAL_BUY", "MANUAL_SELL"}:
                    known_trade_signatures.add(tx)
            for position in staged["positions"].values():
                if not isinstance(position, dict):
                    continue
                buy_tx = str(position.get("buy_tx") or "").lower()
                if buy_tx:
                    known_trade_signatures.add(buy_tx)
                known_trade_signatures.update(str(tx).lower() for tx in position.get("additional_buy_txs", []))
            for item in staged["manual_imports"]:
                if isinstance(item, dict) and item.get("tx"):
                    known_trade_signatures.add(str(item["tx"]).lower())
            for intent in (state.get("sell_intents") or {}).values():
                if isinstance(intent, dict) and (intent.get("swap_tx") or intent.get("tx_hash")):
                    known_trade_signatures.add(str(intent.get("swap_tx") or intent.get("tx_hash")).lower())
            for intent in staged["pending_sells"].values():
                if isinstance(intent, dict) and (intent.get("swap_tx") or intent.get("tx_hash")):
                    known_trade_signatures.add(str(intent.get("swap_tx") or intent.get("tx_hash")).lower())
            imported = []
            transaction_map = {}
            for row in transactions:
                tx_hash = str(row.get("hash") or row.get("transaction_hash") or "").lower()
                if tx_hash:
                    transaction_map[tx_hash] = row
            for tx_hash in sorted(tx_hashes, key=lambda value: (tx_order.get(value, (0, 0, "")), value)):
                if tx_hash in processed_signatures:
                    continue
                if tx_hash in known_trade_signatures:
                    processed_signatures.add(tx_hash)
                    continue
                tx = real_trader.rpc("eth_getTransactionByHash", [tx_hash])
                receipt = real_trader.rpc("eth_getTransactionReceipt", [tx_hash])
                if not isinstance(tx, dict) or not isinstance(receipt, dict):
                    raise TypeError(f"transaction or receipt unavailable for {tx_hash}")
                # Only wallet-originated transactions can represent the user's
                # own manual BUY/SELL. Incoming gifts/transfers are not purchases.
                if _address(tx.get("from")) != wallet:
                    processed_signatures.add(tx_hash)
                    continue
                if _int(receipt.get("status")) != 1:
                    processed_signatures.add(tx_hash)
                    continue
                row = transaction_map.get(tx_hash, {})
                timestamp = row.get("timestamp") or row.get("block_timestamp")
                if not timestamp:
                    # A token-transfer row supplies the same confirmed block timestamp.
                    timestamp = next((x.get("timestamp") for x in transfers
                                      if str(x.get("transaction_hash") or "").lower() == tx_hash), None)
                parsed = _parse_timestamp(timestamp)
                if parsed is None:
                    raise TypeError(f"transaction timestamp unavailable for {tx_hash}")
                imported.extend(_apply_swap(staged, tx_hash, tx, receipt, parsed.isoformat(),
                                            wallet, seen, real_trader))
                processed_signatures.add(tx_hash)
            # Preserve decimals for all known open tokens even if an explorer omits zero balances.
            for token in staged["positions"]:
                if token not in token_balances:
                    decimals = real_trader.token_decimals(token)
                    token_balances[token] = {"raw": "0", "decimals": decimals, "symbol": token[:10]}
            # Confirm that the explorer token snapshot and RPC native balance are usable.
            if any(not isinstance(v, dict) or int(v.get("raw", -1)) < 0 for v in token_balances.values()):
                raise TypeError("wallet token-balance snapshot failed validation")
            synced_at = _iso()
            staged["walletBalances"]["checked_at"] = synced_at
            staged.pop("_liveTokenMetadata", None)
            latest_block = max(latest_block, max(0, latest_explorer_block - 5))
            staged["lastProcessedBlock"] = latest_block
            newest_row = max(
                (row for row in transactions if row.get("hash") or row.get("transaction_hash")),
                key=lambda row: (_int(row.get("block_number")), _int(row.get("transaction_index", row.get("index", 0)))),
                default=None,
            )
            staged["lastProcessedSignature"] = (
                str(newest_row.get("hash") or newest_row.get("transaction_hash")).lower()
                if newest_row else state.get("lastProcessedSignature")
            )
            staged["lastBlockchainDataAt"] = (
                _iso(latest_timestamp) if latest_timestamp else state.get("lastBlockchainDataAt")
            )
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
            staged["processedBlockchainEvents"] = sorted(seen)[-50000:]
            staged["processedBlockchainSignatures"] = sorted(processed_signatures)[-50000:]
            staged["updated_at"] = synced_at
            real_trader.save_state(staged)
            return {"ok": True, "syncStatus": "FRESH", "lastSyncAttemptAt": attempted,
                    "lastSuccessfulWalletSyncAt": synced_at,
                    "lastSuccessfulReconciliationAt": synced_at,
                    "lastBlockchainDataAt": staged["lastBlockchainDataAt"],
                    "imported": imported, "processed_transactions": len(tx_hashes),
                    "walletBalances": staged["walletBalances"]}
        except Exception as exc:  # noqa: BLE001 - fail closed for any provider or reconciliation error
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
            except Exception as save_exc:  # noqa: BLE001 - preserve the original sync failure
                print(
                    f"REAL wallet sync failure status could not be persisted: "
                    f"{type(save_exc).__name__}: {str(save_exc)[:200]}",
                    file=sys.stderr, flush=True,
                )
            return {"ok": False, "syncStatus": "FAILED",
                    "syncError": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "lastSyncAttemptAt": attempted}
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
