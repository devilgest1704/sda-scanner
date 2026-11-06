"""Telegram GUI for the separate automated real-trading bot.

The existing REAL menu remains the user's manual real-wallet dashboard.
This module only reports the dedicated automated bot wallet/state; it does not
execute orders.
"""
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
import real_trading_config as cfg

BOT_WALLET_ADDRESS = "0x9e3643f2ac15c91ee83aa2814015f15b18d573f2"
BOT_WALLET_FALLBACK = BOT_WALLET_ADDRESS
EXPLORER_API = "https://ledger.sidrachain.com/api/v2"
SUPABASE_TRANSACTIONS_URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
SUPABASE_HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public",
}
MARKET_PRICE_STALE_SECONDS = 900
EXTERNAL_REFERENCE_ENV = "REAL_WALLET_REFERENCE_TOTAL_SDA"


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _wallet_address():
    value = os.environ.get(cfg.WALLET_ADDRESS_ENV, "").strip()
    if value:
        return value
    return BOT_WALLET_FALLBACK


def _state(dashboard):
    value = dashboard.load(cfg.STATE_FILE, {})
    return value if isinstance(value, dict) else {}


def _status():
    if cfg.REAL_TRADING_ENABLED and not cfg.REAL_TRADING_DRY_RUN:
        return "🟢 LIVE"
    if cfg.REAL_TRADING_DRY_RUN:
        return "🟡 DRY RUN"
    return "🔴 OFF"


def _wallet_balance_sda():
    address = _wallet_address().strip().lower()
    if address != BOT_WALLET_ADDRESS:
        return None, "configured wallet does not match the REAL bot wallet"
    try:
        response = requests.post(
            cfg.RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]},
            timeout=10,
            headers={"Content-Type": "application/json", "User-Agent": "sda-real-bot-gui/1.1"},
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            return None, str(data["error"])
        result = data.get("result")
        if not isinstance(result, str):
            return None, "RPC returned no balance"
        return int(result, 16) / 10**18, None
    except Exception as exc:
        return None, str(exc)


def _wallet_short(wallet):
    return wallet[:10] + "..." + wallet[-6:] if len(wallet) > 20 else (wallet or "NOT CONFIGURED")


def _token_label(token, pos):
    """Prefer saved position metadata, then the scanner's token catalog."""
    symbol = str(pos.get("symbol") or "").strip()
    name = str(pos.get("name") or pos.get("token_name") or "").strip()
    if not symbol or symbol.lower().startswith("0x"):
        try:
            with open("token_metadata.json", "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            item = metadata.get(str(token).lower(), {}) if isinstance(metadata, dict) else {}
            if isinstance(item, dict):
                if not symbol or symbol.lower().startswith("0x"):
                    symbol = str(item.get("symbol") or "").strip()
                name = name or str(item.get("name") or "").strip()
        except Exception:
            pass
    if symbol and name and symbol.casefold() != name.casefold():
        return f"{symbol} — {name}"
    return symbol or name or str(token)[:10]


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _quote_age(quoted_at):
    if not quoted_at:
        return "quote age unknown"
    try:
        stamp = datetime.fromisoformat(str(quoted_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age_seconds < 0:
            return "quote timestamp is in the future"
        if age_seconds < 3600:
            return f"quote age {int(age_seconds // 60)}m"
        hours, remainder = divmod(int(age_seconds), 3600)
        if hours < 48:
            return f"quote age {hours}h {remainder // 60}m"
        return f"quote age {hours // 24}d {hours % 24}h"
    except (TypeError, ValueError, OverflowError):
        return "quote age unknown"


def _parse_utc(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _newest_supabase_price(token):
    token = str(token).strip().lower()
    try:
        response = requests.get(
            SUPABASE_TRANSACTIONS_URL,
            params={
                "select": "token_address,price_in_sda,tx_timestamp,tx_type",
                "token_address": f"eq.{token}",
                "order": "tx_timestamp.desc",
                "limit": "100",
            },
            headers=SUPABASE_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception as exc:
        return {"price_sda": None, "timestamp": None, "error": f"Supabase: {type(exc).__name__}"}
    if not isinstance(rows, list):
        return {"price_sda": None, "timestamp": None, "error": "Supabase: invalid response"}

    now = datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("token_address") or "").lower() != token:
            continue
        if str(row.get("tx_type") or "").lower() not in {"buy", "sell"}:
            continue
        price = _finite_number(row.get("price_in_sda"))
        stamp = _parse_utc(row.get("tx_timestamp"))
        if price is None or price <= 0 or stamp is None or stamp > now:
            continue
        age_seconds = max(0.0, (now - stamp).total_seconds())
        return {
            "price_sda": price,
            "timestamp": stamp.isoformat(),
            "age_seconds": age_seconds,
            "stale": age_seconds > MARKET_PRICE_STALE_SECONDS,
            "source": "Supabase token_transactions.price_in_sda",
            "error": None,
        }
    return {"price_sda": None, "timestamp": None, "error": "no valid Supabase price row"}


def _unwrap_items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "balances", "result", "data", "tokens", "token_balances"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            items = _unwrap_items(value)
            if items:
                return items
    return []


def _address_from(value):
    if isinstance(value, str):
        return value.lower() if value.startswith("0x") else ""
    if isinstance(value, dict):
        for key in ("address_hash", "hash", "address", "token_address"):
            address = value.get(key)
            if isinstance(address, str) and address.startswith("0x"):
                return address.lower()
    return ""


def _raw_integer(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("raw", value.get("balance")))
    try:
        number = int(str(value), 0) if str(value).lower().startswith("0x") else int(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _human_token_amount(raw, decimals):
    raw_integer = _raw_integer(raw)
    try:
        decimals = int(decimals)
    except (TypeError, ValueError, OverflowError):
        return None
    if raw_integer is None or not 0 <= decimals <= 36:
        return None
    return Decimal(raw_integer) / (Decimal(10) ** decimals)


def _fetch_wallet_snapshot():
    wallet = _wallet_address().strip().lower()
    snapshot = {
        "wallet_address": wallet,
        "native_sda": None,
        "holdings": [],
        "token_balances_loaded": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "errors": [],
    }
    if wallet != BOT_WALLET_ADDRESS:
        snapshot["errors"].append("Configured wallet address does not match the REAL bot wallet")
        return snapshot

    try:
        response = requests.get(f"{EXPLORER_API}/addresses/{wallet}", timeout=15)
        response.raise_for_status()
        address_data = response.json()
        native_raw = _raw_integer(address_data.get("coin_balance", address_data.get("balance")))
        if native_raw is None:
            raise ValueError("Blockscout address response has no native balance")
        snapshot["native_sda"] = float(Decimal(native_raw) / Decimal(10**18))
    except Exception as exc:
        snapshot["errors"].append(f"Native balance unavailable: {type(exc).__name__}")

    try:
        response = requests.get(f"{EXPLORER_API}/addresses/{wallet}/token-balances", timeout=20)
        response.raise_for_status()
        items = _unwrap_items(response.json())
        snapshot["token_balances_loaded"] = True
    except Exception as exc:
        snapshot["errors"].append(f"Token balances unavailable: {type(exc).__name__}")
        return snapshot

    holdings = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token_data = item.get("token") if isinstance(item.get("token"), dict) else item
        address = _address_from(token_data) or _address_from(item)
        raw = _raw_integer(item.get("value", item.get("balance", item.get("token_balance"))))
        try:
            decimals = int(token_data.get("decimals", item.get("decimals")))
        except (TypeError, ValueError, OverflowError):
            decimals = -1
        if not address or raw is None or raw <= 0 or not 0 <= decimals <= 36:
            continue
        quantity = _human_token_amount(raw, decimals)
        if quantity is None:
            continue
        holdings.append({
            "address": address,
            "symbol": str(token_data.get("symbol") or address[:10]).strip(),
            "amount_raw": raw,
            "decimals": decimals,
            "amount": quantity,
            "price_sda": None,
            "price_timestamp": None,
            "price_age_seconds": None,
            "price_stale": False,
            "market_value_sda": None,
            "market_error": "price unavailable",
        })

    if holdings:
        with ThreadPoolExecutor(max_workers=min(8, len(holdings))) as pool:
            prices = list(pool.map(lambda holding: _newest_supabase_price(holding["address"]), holdings))
        for holding, price in zip(holdings, prices):
            holding["price_sda"] = price.get("price_sda")
            holding["price_timestamp"] = price.get("timestamp")
            holding["price_age_seconds"] = price.get("age_seconds")
            holding["price_stale"] = bool(price.get("stale"))
            holding["market_error"] = price.get("error")
            if price.get("price_sda") is not None:
                holding["market_value_sda"] = holding["amount"] * Decimal(str(price["price_sda"]))
        def quote_holding(holding):
            started = datetime.now(timezone.utc)
            try:
                import real_trader
                quote = real_trader.prepare_sell(holding["address"], holding["symbol"], holding["amount_raw"])
                proceeds_raw = _raw_integer(quote.get("quoted_wsda_raw"))
                if proceeds_raw is None or proceeds_raw <= 0:
                    return {"sell_value_sda": None, "sell_error": "empty quote"}
                return {
                    "sell_value_sda": Decimal(proceeds_raw) / (Decimal(10) ** 18),
                    "sell_error": None,
                    "sell_quote_timestamp": datetime.now(timezone.utc).isoformat(),
                    "sell_quote_age_seconds": (datetime.now(timezone.utc) - started).total_seconds(),
                }
            except Exception as exc:
                return {"sell_value_sda": None, "sell_error": _short_sell_quote_error(exc)}

        with ThreadPoolExecutor(max_workers=min(8, len(holdings))) as pool:
            sell_quotes = list(pool.map(quote_holding, holdings))
        for holding, quote in zip(holdings, sell_quotes):
            holding.update(quote)
            holding["sellable"] = holding.get("sell_value_sda") is not None
    snapshot["holdings"] = holdings
    snapshot["market_valued_count"] = sum(h["market_value_sda"] is not None for h in holdings)
    snapshot["sell_quoted_count"] = sum(h.get("sell_value_sda") is not None for h in holdings)
    return snapshot


def _market_price(dashboard, token):
    price = _newest_supabase_price(token)
    return price.get("price_sda"), price.get("timestamp"), price.get("age_seconds"), price.get("error")


def _short_sell_quote_error(exc):
    text = str(exc).upper()
    if "STF" in text:
        return "STF"
    if "INSUFFICIENT_OUTPUT_AMOUNT" in text:
        return "slippage"
    if "EXPIRED" in text:
        return "expired"
    if "TIMEOUT" in text:
        return "timeout"
    if "REVERT" in text:
        return "reverted"
    if "NO LIVE PINETSWAP QUOTE" in text:
        return "no route"
    return "quote error"


def _position_mark_to_market(dashboard, token, pos, holding=None):
    """Return a Supabase market mark and independent full-position sell quote."""
    token = str(token).lower()
    cost_sda = _finite_number(pos.get("amount_sda", pos.get("cost_sda")))
    mark = {
        "amount_raw": None,
        "cost_sda": cost_sda,
        "amount": None,
        "price_sda": None,
        "price_timestamp": None,
        "price_age_seconds": None,
        "price_stale": False,
        "market_value_sda": None,
        "market_pnl_sda": None,
        "market_pnl_pct": None,
        "sell_value_sda": None,
        "sell_pnl_sda": None,
        "sell_pnl_pct": None,
        "sell_quote_timestamp": None,
        "sell_quote_age_seconds": None,
        "sell_quote_vs_market_pct": None,
        "sellable": False,
        "sell_error": "wallet balance unavailable",
        "market_error": "market price unavailable",
    }
    if not isinstance(holding, dict):
        return mark
    amount_raw = holding.get("amount_raw")
    if amount_raw is None or holding.get("amount") is None:
        return mark
    mark["amount_raw"] = int(amount_raw)
    mark["amount"] = holding["amount"]
    mark["price_sda"] = holding.get("price_sda")
    mark["price_timestamp"] = holding.get("price_timestamp")
    mark["price_age_seconds"] = holding.get("price_age_seconds")
    mark["price_stale"] = bool(holding.get("price_stale"))
    mark["market_value_sda"] = (_finite_number(holding.get("market_value_sda"))
                                if holding.get("market_value_sda") is not None else None)
    mark["market_error"] = holding.get("market_error")
    mark["sell_value_sda"] = (_finite_number(holding.get("sell_value_sda"))
                              if holding.get("sell_value_sda") is not None else None)
    mark["sell_quote_timestamp"] = holding.get("sell_quote_timestamp")
    mark["sell_quote_age_seconds"] = holding.get("sell_quote_age_seconds")
    mark["sell_error"] = holding.get("sell_error")
    mark["sellable"] = bool(holding.get("sellable"))
    if mark["market_value_sda"] is not None and cost_sda is not None:
        mark["market_pnl_sda"] = float(mark["market_value_sda"]) - cost_sda
        mark["market_pnl_pct"] = mark["market_pnl_sda"] / cost_sda * 100 if cost_sda > 0 else None
    if mark["sell_value_sda"] is not None and cost_sda is not None:
        mark["sell_pnl_sda"] = float(mark["sell_value_sda"]) - cost_sda
        mark["sell_pnl_pct"] = mark["sell_pnl_sda"] / cost_sda * 100 if cost_sda > 0 else None
        if mark["market_value_sda"] is not None and mark["market_value_sda"] > 0:
            mark["sell_quote_vs_market_pct"] = (float(mark["sell_value_sda"]) / float(mark["market_value_sda"]) - 1) * 100
    amount_raw = int(amount_raw)
    if amount_raw <= 0:
        mark["sell_error"] = "zero wallet balance"
        return mark
    return mark


def _position_marks(dashboard, positions):
    snapshot = _fetch_wallet_snapshot()
    holdings = {item["address"]: item for item in snapshot.get("holdings", [])}
    marks = []
    for token, pos in positions.items():
        if isinstance(pos, dict):
            address = str(token).lower()
            holding = holdings.get(address)
            if holding is None and snapshot.get("token_balances_loaded"):
                holding = {"address": address, "amount_raw": 0, "amount": Decimal(0),
                           "sell_error": "zero wallet balance", "market_error": "zero wallet balance"}
            marks.append((_token_label(token, pos),
                          _position_mark_to_market(dashboard, address, pos, holding)))
    return marks, snapshot


def _nested_address(value):
    return _address_from(value)


def _explorer_get(path):
    response = requests.get(f"{EXPLORER_API}/{path.lstrip('/')}", timeout=15)
    response.raise_for_status()
    return response.json()


def _transfer_amount(transfer):
    total = transfer.get("total", transfer.get("value"))
    raw = _raw_integer(total)
    token = transfer.get("token") if isinstance(transfer.get("token"), dict) else {}
    try:
        decimals = int(token.get("decimals", 18))
    except (TypeError, ValueError, OverflowError):
        return None
    if raw is None or not 0 <= decimals <= 36:
        return None
    return Decimal(raw) / (Decimal(10) ** decimals)


def _verify_trade(trade, wallet):
    action = str(trade.get("action") or "").upper()
    token = str(trade.get("token") or "").lower()
    tx_hash = str(trade.get("tx_hash") or trade.get("tx") or "").strip()
    result = {"verified": False, "status": "UNVERIFIED", "tx": tx_hash or None,
              "proceeds_sda": None, "reason": "missing transaction hash"}
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
        if str(trade.get("source") or "").lower() == "manual_user_confirmed" or not tx_hash:
            result["status"] = "UNVERIFIED MANUAL"
        else:
            result["reason"] = "invalid transaction hash"
        return result
    if action not in {"BUY", "SELL"} or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
        result["reason"] = "missing action or token address"
        return result

    try:
        tx = _explorer_get(f"transactions/{tx_hash}")
        if not isinstance(tx, dict) or str(tx.get("status") or "").lower() != "ok":
            result["reason"] = "transaction not confirmed successfully"
            return result
        returned_hash = str(tx.get("hash") or tx.get("transaction_hash") or "").lower()
        if returned_hash != tx_hash.lower():
            result["reason"] = "explorer transaction hash does not match ledger hash"
            return result
        if _nested_address(tx.get("from")) != wallet:
            result["reason"] = "transaction sender does not match bot wallet"
            return result
        transfer_data = _explorer_get(f"transactions/{tx_hash}/token-transfers")
        transfers = _unwrap_items(transfer_data)
    except Exception as exc:
        result["reason"] = f"explorer verification unavailable: {type(exc).__name__}"
        return result

    token_amount = Decimal(0)
    wsda_amount = Decimal(0)
    native_sda_value = Decimal(0)
    native_raw = _raw_integer(tx.get("value"))
    if native_raw is not None:
        native_sda_value = Decimal(native_raw) / (Decimal(10) ** 18)
    wsda = str(cfg.WSDA_ADDRESS).lower()
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        transfer_token = _nested_address(transfer.get("token"))
        sender = _nested_address(transfer.get("from"))
        recipient = _nested_address(transfer.get("to"))
        amount = _transfer_amount(transfer)
        if amount is None or amount <= 0:
            continue
        if action == "BUY":
            if transfer_token == token and recipient == wallet:
                token_amount += amount
            if transfer_token == wsda and sender == wallet:
                wsda_amount += amount
        else:
            if transfer_token == token and sender == wallet:
                token_amount += amount
            if transfer_token == wsda and recipient == wallet:
                wsda_amount += amount

    if action == "BUY" and wsda_amount <= 0:
        wsda_amount = native_sda_value
    if token_amount <= 0 or wsda_amount <= 0:
        result["reason"] = "matching bot-wallet token/SDA transfers not found"
        return result
    result.update({"verified": True, "status": "VERIFIED", "reason": None,
                   "token_amount": token_amount, "sda_transfer": wsda_amount})
    if action == "SELL":
        result["proceeds_sda"] = float(wsda_amount)
    return result


def _realized_summary(state, wallet):
    trades = state.get("trades", []) if isinstance(state, dict) else []
    verified = []
    unverified = []
    verified_buys = []
    records = []
    for trade in trades:
        if not isinstance(trade, dict) or str(trade.get("action", "")).upper() not in {"BUY", "SELL"}:
            continue
        result = _verify_trade(trade, wallet)
        record = {"trade": trade, **result}
        records.append(record)
        if str(trade.get("action", "")).upper() == "BUY":
            (verified_buys if result["verified"] else unverified).append(record)
            continue
        if result["verified"]:
            cost = _finite_number(trade.get("cost_sda", trade.get("amount_sda")))
            if cost is None:
                record.update({"verified": False, "status": "UNVERIFIED",
                               "reason": "verified transfer but missing cost basis"})
                unverified.append(record)
            else:
                record["realized_pnl_sda"] = result["proceeds_sda"] - cost
                verified.append(record)
        else:
            saved_pnl = _finite_number(trade.get("realized_pnl_sda")
                                       if trade.get("realized_pnl_sda") is not None
                                       else trade.get("pnl_sda"))
            record["realized_pnl_sda"] = saved_pnl
            unverified.append(record)
    verified_total = sum(record["realized_pnl_sda"] for record in verified)
    manual_values = [record.get("realized_pnl_sda") for record in unverified
                     if record.get("realized_pnl_sda") is not None]
    return {
        "verified_pnl_sda": verified_total,
        "unverified_pnl_sda": sum(manual_values),
        "verified_trades": verified,
        "unverified_trades": unverified,
        "verified_count": len(verified),
        "unverified_count": sum(1 for item in unverified
                                 if str(item["trade"].get("action", "")).upper() == "SELL"),
        "verified_buys": verified_buys,
        "records": records,
    }


def _portfolio_summary_lines(snapshot, marks, realized):
    holdings = snapshot.get("holdings", [])
    token_count = len(holdings)
    market_count = sum(item.get("market_value_sda") is not None for item in holdings)
    liquidation_count = sum(item.get("sell_value_sda") is not None for item in holdings)
    token_market = sum((Decimal(str(item["market_value_sda"])) for item in holdings
                        if item.get("market_value_sda") is not None), Decimal(0))
    token_liquidation = sum((Decimal(str(item["sell_value_sda"])) for item in holdings
                             if item.get("sell_value_sda") is not None), Decimal(0))
    native = snapshot.get("native_sda")
    wallet_market = native + float(token_market) if native is not None else None
    wallet_liquidation = native + float(token_liquidation) if native is not None else None
    position_count = len(marks)
    market_count_open = sum(mark.get("market_value_sda") is not None for _, mark in marks)
    sell_count_open = sum(mark.get("sell_value_sda") is not None for _, mark in marks)
    open_cost = sum(mark["cost_sda"] for _, mark in marks if mark.get("cost_sda") is not None)
    open_market = sum(float(mark["market_value_sda"]) for _, mark in marks
                      if mark.get("market_value_sda") is not None)
    open_sell = sum(float(mark["sell_value_sda"]) for _, mark in marks
                    if mark.get("sell_value_sda") is not None)
    market_pnl = sum(mark["market_pnl_sda"] for _, mark in marks
                     if mark.get("market_pnl_sda") is not None)
    executable_pnl = sum(mark["sell_pnl_sda"] for _, mark in marks
                         if mark.get("sell_pnl_sda") is not None)
    market_complete = market_count_open == position_count
    sell_complete = sell_count_open == position_count
    market_label = "Total wallet market value" if market_count == token_count else "Known wallet market value subtotal"
    liquidation_label = ("Total wallet estimated liquidation value" if liquidation_count == token_count
                         else "Known wallet liquidation value subtotal")
    reference = _finite_number(os.getenv(EXTERNAL_REFERENCE_ENV))
    lines = [
        f"👛 Wallet address: {snapshot.get('wallet_address') or 'UNAVAILABLE'}",
        f"💰 Native SDA balance: {native:.4f} SDA" if native is not None else "💰 Native SDA balance: N/A",
        f"💼 Open-position market value: {open_market:.4f} SDA (priced {market_count_open}/{position_count})",
        f"📦 Open-position estimated sell value: {open_sell:.4f} SDA (quoted {sell_count_open}/{position_count})",
        f"💵 Open cost basis: {open_cost:.4f} SDA",
        (f"📊 Total unrealized market P/L: {market_pnl:+.4f} SDA" if market_complete
         else f"📊 Known unrealized market P/L: {market_pnl:+.4f} SDA (priced {market_count_open}/{position_count})"),
        (f"📉 Total executable P/L: {executable_pnl:+.4f} SDA" if sell_complete
         else f"📉 Known executable P/L: {executable_pnl:+.4f} SDA (quoted {sell_count_open}/{position_count})"),
        f"🧾 Verified realized P/L: {realized['verified_pnl_sda']:+.4f} SDA (gas excluded; {realized['verified_count']} verified sells)",
        f"📝 Unverified/manual realized P/L: {realized['unverified_pnl_sda']:+.4f} SDA ({realized['unverified_count']} unverified sells)",
        (f"🌐 {market_label}: {wallet_market:.4f} SDA (priced {market_count}/{token_count} token balances)"
         if wallet_market is not None else f"🌐 {market_label}: N/A (native balance unavailable)"),
        (f"🧮 {liquidation_label}: {wallet_liquidation:.4f} SDA (quoted {liquidation_count}/{token_count} token balances)"
         if wallet_liquidation is not None else f"🧮 {liquidation_label}: N/A (native balance unavailable)"),
    ]
    if reference is not None and wallet_market is not None:
        lines += [f"📌 External reference total: {reference:.4f} SDA",
                  f"↔ Difference (project market − reference): {wallet_market - reference:+.4f} SDA"]
    if snapshot.get("errors"):
        lines.append("⚠ Wallet data: " + "; ".join(snapshot["errors"]))
    lines += [
        "Sources: Blockscout bot-wallet balances; Supabase newest valid price_in_sda; PinetSwap read-only full-balance quotes.",
        "Market-price age warning threshold: 15 minutes. Wallet liquidation total is partial when any token balance is unquoted.",
    ]
    return lines


def _position_lines(symbol, mark):
    amount = f"{mark['amount']:.8f}" if mark.get("amount") is not None else "UNAVAILABLE"
    cost = f"{mark['cost_sda']:.4f} SDA" if mark["cost_sda"] is not None else "UNKNOWN"
    market_value = (f"{float(mark['market_value_sda']):.4f} SDA"
                    if mark.get("market_value_sda") is not None else "UNPRICED")
    market_pnl = (f"{mark['market_pnl_sda']:+.4f} SDA ({mark['market_pnl_pct']:+.2f}%)"
                  if mark.get("market_pnl_sda") is not None else "N/A")
    sell_value = f"{float(mark['sell_value_sda']):.4f} SDA" if mark.get("sell_value_sda") is not None else "UNAVAILABLE"
    sell_pnl = (f"{mark['sell_pnl_sda']:+.4f} SDA ({mark['sell_pnl_pct']:+.2f}%)"
                if mark.get("sell_pnl_sda") is not None else "N/A")
    if mark["price_sda"] is not None:
        age_seconds = mark.get("price_age_seconds")
        age = _quote_age(mark.get("price_timestamp"))
        stale = " ⚠ STALE PRICE" if mark.get("price_stale") else ""
        price_line = f"Market price: {mark['price_sda']:.10g} SDA at {mark['price_timestamp']} ({age}){stale}"
    else:
        price_line = f"Market price: unavailable ({mark.get('market_error') or 'no valid price'})"
    sell_line = ("Sell: ✅ executable quote" if mark["sellable"]
                 else f"Sell: ⚠ quote unavailable ({mark['sell_error'] or 'unknown'})")
    lines = [f"• {symbol}", f"  Quantity: {amount}", f"  Original invested: {cost}",
             f"  Market value: {market_value}", f"  Market unrealized P/L: {market_pnl}",
             f"  {price_line}", f"  Estimated sell value: {sell_value}",
             f"  Executable P/L: {sell_pnl}", f"  {sell_line}"]
    if mark.get("sell_quote_timestamp"):
        lines.append(f"  Sell quote: {mark['sell_quote_timestamp']} (age {mark['sell_quote_age_seconds']:.2f}s)")
    if mark.get("sell_quote_vs_market_pct") is not None:
        lines.append(f"  Sell quote vs market mark: {mark['sell_quote_vs_market_pct']:+.2f}% (not a standalone price-impact estimate)")
    return lines


def bot_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        positions = {}
    wallet = _wallet_address()
    state_updated = str(state.get("updated_at") or "—")[:19].replace("T", " ")
    marks, snapshot = _position_marks(dashboard, positions)
    realized = _realized_summary(state, BOT_WALLET_ADDRESS)
    lines = [
        "🤖 REAL TRADING BOT", "", f"Status: {_status()}",
        f"👛 Bot wallet: {_wallet_short(wallet)}",
        f"🎯 Position size: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📦 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.0f} SDA",
        f"📊 Open positions: {len(positions)}/{cfg.MAX_OPEN_POSITIONS}",
        f"🕒 Position ledger updated: {state_updated} UTC",
    ]
    lines += _portfolio_summary_lines(snapshot, marks, realized)
    if not marks:
        lines += ["────────────────────────",
                  "⚪ No open bot positions"]
    else:
        lines += ["🪙 OPEN BOT POSITIONS", "────────────────────────"]
        for symbol, mark in marks:
            lines.extend(_position_lines(symbol, mark))
    if realized["unverified_trades"]:
        lines += ["", "📎 UNVERIFIED REALIZED LEDGER ENTRIES"]
        for record in realized["unverified_trades"]:
            trade = record["trade"]
            symbol = trade.get("symbol") or str(trade.get("token") or "UNKNOWN")[:10]
            pnl = record.get("realized_pnl_sda")
            pnl_text = f" • recorded P/L {pnl:+.4f} SDA" if pnl is not None else " • P/L unavailable"
            label = ("UNVERIFIED MANUAL" if record["status"] == "UNVERIFIED MANUAL"
                     else f"UNVERIFIED {str(trade.get('action') or 'TRADE').upper()}")
            lines.append(f"{label} • {symbol}{pnl_text} • {record['reason']}")
    lines += ["", "Market value is based on the newest valid Supabase transaction price; estimated sell value is a separate read-only PinetSwap quote."]
    return "\n".join(lines)


def statistics_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    trades = state.get("trades", [])
    positions = positions if isinstance(positions, dict) else {}
    trades = [x for x in trades if isinstance(x, dict)] if isinstance(trades, list) else []
    buys = [x for x in trades if str(x.get("action", "")).upper() == "BUY"]
    sells = [x for x in trades if str(x.get("action", "")).upper() == "SELL"]
    wallet = _wallet_address()
    realized = _realized_summary(state, BOT_WALLET_ADDRESS)
    verified_values = [item["realized_pnl_sda"] for item in realized["verified_trades"]]
    wins = [value for value in verified_values if value > 0]
    losses = [value for value in verified_values if value < 0]
    wallet = _wallet_address()
    updated = str(state.get("updated_at") or "—")[:16].replace("T", " ")
    marks, snapshot = _position_marks(dashboard, positions)
    gross_wins, gross_losses = sum(wins), abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses else (float("inf") if gross_wins else None)
    win_rate = len(wins) / len(verified_values) * 100 if verified_values else None
    closed_summary = (f"📊 SELL trades: {len(sells)} • Verified: {realized['verified_count']} • Unverified: {realized['unverified_count']}"
                      f" • Verified win rate: {win_rate:.1f}%" if win_rate is not None else
                      f"📊 SELL trades: {len(sells)} • Verified: {realized['verified_count']} • Unverified: {realized['unverified_count']} • Verified win rate: N/A")
    lines = [
        "📈 REAL TRADING BOT • STATISTICS", "",
        f"🟢 Open positions: {len(positions)} / {cfg.MAX_OPEN_POSITIONS}",
        f"📥 BUY entries: {len(buys)} • Verified: {len(realized['verified_buys'])}",
        closed_summary,
        f"💹 Verified realized P/L: {realized['verified_pnl_sda']:+.4f} SDA (gas excluded)",
        f"📝 Unverified/manual realized P/L: {realized['unverified_pnl_sda']:+.4f} SDA",
        f"🏆 Avg win: {sum(wins)/len(wins):+.2f} SDA" if wins else "🏆 Avg win: N/A",
        f"🧯 Avg loss: {sum(losses)/len(losses):+.2f} SDA" if losses else "🧯 Avg loss: N/A",
        f"⚖️ Profit factor: {pf:.2f}" if pf is not None and pf != float("inf") else ("⚖️ Profit factor: ∞" if pf == float("inf") else "⚖️ Profit factor: N/A"),
        f"📊 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.2f} SDA",
        f"🎯 Position range: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"🕒 State updated: {updated}",
    ]
    lines += _portfolio_summary_lines(snapshot, marks, realized)
    lines += [
        "", "🪙 OPEN POSITION P/L", "────────────────────────",
    ]
    for symbol, mark in marks:
        lines.extend(_position_lines(symbol, mark))
    if not marks:
        lines.append("⚪ No open positions")
    lines += ["", "📜 RECENT BOT TRADES", "────────────────────────"]
    if not trades:
        lines.append("⚪ No bot trades yet")
    else:
        verification_by_trade = {id(record["trade"]): record for record in realized["records"]}
        for trade in trades[-15:][::-1]:
            action = str(trade.get("action") or "?").upper()
            symbol = trade.get("symbol") or str(trade.get("token") or "UNKNOWN")[:10]
            amount = trade.get("amount_sda")
            tx = str(trade.get("tx") or "")
            amount_text = f" • {float(amount):.2f} SDA" if amount is not None else ""
            record = verification_by_trade.get(id(trade))
            pnl = record.get("realized_pnl_sda") if record else None
            pnl_text = f" • verified P/L {float(pnl):+.4f} SDA" if action == "SELL" and record and record.get("verified") else ""
            verification = f" • {record['status']}" if record else ""
            if action == "SELL" and record and not record.get("verified"):
                saved_pnl = record.get("realized_pnl_sda")
                pnl_text = f" • unverified P/L {float(saved_pnl):+.4f} SDA" if saved_pnl is not None else ""
            lines.append(f"{'🟢' if action == 'BUY' else '🔴' if action == 'SELL' else '⚪'} {action} {symbol}{amount_text}{verification}{pnl_text}")
            if tx:
                lines.append(f"   tx {tx[:10]}...")
    lines += ["", f"👛 Bot wallet: {_wallet_short(wallet)}",
              "Verified SELL P/L is based on confirmed WSDA/token transfer logs and excludes gas."]
    return "\n".join(lines)
def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_real_bot_menu_patched", False):
        return dashboard

    original_menu = dashboard.menu_keyboard
    original_handle = dashboard.handle_update

    def menu_keyboard():
        keyboard = original_menu()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REAL_BOT" for row in rows):
            real_index = next((i for i, row in enumerate(rows) if row and row[0].get("callback_data") == "REAL"), None)
            bot_row = [{"text": "🤖 Real Trading Bot", "callback_data": "REAL_BOT"}]
            if real_index is None:
                rows.insert(2, bot_row)
            else:
                rows.insert(real_index + 1, bot_row)
        return keyboard

    def handle_update(update, state=None):
        cb = update.get("callback_query") or {}
        data = cb.get("data")
        if data not in ("REAL_BOT", "REAL_BOT_STATS", "REAL_BOT_REFRESH", "REAL_BOT_STATS_REFRESH"):
            return original_handle(update, state)
        state = state if state is not None else {"offset": 0}
        state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        configured_chat = os.environ.get("CHAT_ID")
        if configured_chat and str(chat_id) != str(configured_chat):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state
        dashboard.answer_callback(cb.get("id"))
        if data in ("REAL_BOT", "REAL_BOT_REFRESH"):
            keyboard = {"inline_keyboard": [
                [{"text": "🔄 Refresh live P/L", "callback_data": "REAL_BOT_REFRESH"}],
                [{"text": "📈 Statistics", "callback_data": "REAL_BOT_STATS"}],
                [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
            ]}
            dashboard.edit(chat_id, message_id, bot_report(dashboard), keyboard)
        else:
            keyboard = {"inline_keyboard": [
                [{"text": "🔄 Refresh statistics", "callback_data": "REAL_BOT_STATS_REFRESH"}],
                [{"text": "⬅️ Real Trading Bot", "callback_data": "REAL_BOT"}],
                [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
            ]}
            dashboard.edit(chat_id, message_id, statistics_report(dashboard), keyboard)
        return state

    dashboard.menu_keyboard = menu_keyboard
    dashboard.handle_update = handle_update
    dashboard._sda_real_bot_menu_patched = True

    # This is deliberately the last callback wrapper. It restores reliable
    # routing for MAIN and the manual Real Wallet & Positions button after all
    # legacy compatibility patches have been installed by api/index.py.
    import telegram_callback_router
    telegram_callback_router.patch_dashboard(dashboard)
    return dashboard
