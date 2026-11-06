"""Telegram GUI for the separate automated real-trading bot.

The existing REAL menu remains the user's manual real-wallet dashboard.
This module only reports the dedicated automated bot wallet/state; it does not
execute orders.
"""
import json
import math
import os
from datetime import datetime, timezone

import real_trading_config as cfg

BOT_WALLET_FALLBACK = "0x9e3643f2ac15c91ee83aa2814015f15b18d573f2"


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
    address = _wallet_address()
    if not address:
        return None, "wallet address missing"
    try:
        import requests
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


def _market_price(dashboard, token):
    market = dashboard.load("market_data.json", {"tokens": {}})
    tokens = market.get("tokens", {}) if isinstance(market, dict) else {}
    row = tokens.get(str(token).lower(), {}) if isinstance(tokens, dict) else {}
    if not isinstance(row, dict) and isinstance(tokens, dict):
        row = next((value for key, value in tokens.items()
                    if str(key).lower() == str(token).lower()), {})
    if not isinstance(row, dict):
        return None, "quote age unknown"
    analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else row
    price = _finite_number(analysis.get("price_in_sda"))
    if price is None or price <= 0:
        return None, "quote age unknown"
    return price, _quote_age(analysis.get("last_transaction"))


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


def _position_mark_to_market(dashboard, token, pos):
    """Return a market valuation and an independent executable-sell status."""
    token = str(token).lower()
    cost_sda = _finite_number(pos.get("amount_sda", pos.get("cost_sda")))
    mark = {
        "amount_raw": None,
        "cost_sda": cost_sda,
        "value_sda": None,
        "pnl_sda": None,
        "pnl_pct": None,
        "price_sda": None,
        "quote_age": "quote age unknown",
        "sellable": False,
        "sell_error": "wallet balance unavailable",
    }
    try:
        import real_trader
        amount_raw = int(real_trader.token_balance(token, _wallet_address()))
    except Exception:
        return mark
    mark["amount_raw"] = amount_raw
    if amount_raw <= 0:
        mark["sell_error"] = "zero wallet balance"
        return mark

    try:
        decimals = int(real_trader.token_decimals(token))
        if not 0 <= decimals <= 36:
            raise ValueError("unsupported token decimals")
        price, quote_age = _market_price(dashboard, token)
        mark["quote_age"] = quote_age
        if price is not None:
            token_amount = amount_raw / (10 ** decimals)
            mark["price_sda"] = price
            mark["value_sda"] = token_amount * price
            if cost_sda is not None and cost_sda > 0:
                mark["pnl_sda"] = mark["value_sda"] - cost_sda
                mark["pnl_pct"] = mark["pnl_sda"] / cost_sda * 100
    except Exception:
        pass

    try:
        quote = real_trader.prepare_sell(token, _token_label(token, pos), amount_raw)
        proceeds_raw = _finite_number(quote.get("quoted_wsda_raw"))
        mark["sellable"] = proceeds_raw is not None and proceeds_raw > 0
        if not mark["sellable"]:
            mark["sell_error"] = "empty quote"
        else:
            mark["sell_error"] = None
    except Exception as exc:
        mark["sell_error"] = _short_sell_quote_error(exc)
    return mark


def _position_marks(dashboard, positions):
    marks = []
    for token, pos in positions.items():
        if isinstance(pos, dict):
            marks.append((_token_label(token, pos), _position_mark_to_market(dashboard, token, pos)))
    return marks


def _realized_total(state):
    trades = state.get("trades", []) if isinstance(state, dict) else []
    sells = [trade for trade in trades if isinstance(trade, dict)
             and str(trade.get("action", "")).upper() == "SELL"]
    if not sells:
        return 0.0
    values = [_finite_number(trade.get("realized_pnl_sda")
                             if trade.get("realized_pnl_sda") is not None
                             else trade.get("pnl_sda")) for trade in sells]
    return sum(values) if all(value is not None for value in values) else None


def _portfolio_summary_lines(balance, balance_error, marks, realized, daily):
    total = len(marks)
    valued = sum(mark["value_sda"] is not None for _, mark in marks)
    cost_known = sum(mark["cost_sda"] is not None and mark["cost_sda"] >= 0
                     for _, mark in marks)
    pnl_known = sum(mark["pnl_sda"] is not None for _, mark in marks)
    sellable = sum(mark["sellable"] for _, mark in marks)
    open_cost = sum(mark["cost_sda"] for _, mark in marks
                    if mark["cost_sda"] is not None and mark["cost_sda"] >= 0)
    open_value = sum(mark["value_sda"] for _, mark in marks if mark["value_sda"] is not None)
    known_pnl = sum(mark["pnl_sda"] for _, mark in marks if mark["pnl_sda"] is not None)
    complete = valued == total and cost_known == total and pnl_known == total
    balance_text = f"{balance:.3f} SDA" if balance is not None else f"N/A ({balance_error})"
    cost_text = (f"{open_cost:.2f} SDA" if cost_known == total
                 else f"{open_cost:.2f} SDA (known {cost_known}/{total})")
    value_label = "Estimated open value" if valued == total else "Known open value subtotal"
    pnl_label = "Unrealized P/L" if complete else "Known unrealized P/L subtotal"
    lines = [
        f"💰 SDA wallet balance: {balance_text}",
        f"💼 Open cost basis: {cost_text}",
        f"📊 {value_label}: {open_value:.2f} SDA (valued {valued}/{total})",
        f"📈 {pnl_label}: {known_pnl:+.2f} SDA (valued {pnl_known}/{total})",
        f"💹 Realized P/L: {realized:+.2f} SDA" if realized is not None else "💹 Realized P/L: N/A",
        f"📉 Daily realized P/L: {daily:+.2f} SDA",
        f"✅ Executable SELL quotes: {sellable}/{total}",
    ]
    if complete and realized is not None:
        lines.append(f"💰 Combined portfolio P/L: {realized + known_pnl:+.2f} SDA")
    else:
        lines.append("💰 Combined portfolio P/L: N/A (open valuation or cost basis incomplete)")
    return lines


def _position_lines(symbol, mark):
    value = f"{mark['value_sda']:.2f} SDA" if mark["value_sda"] is not None else "UNPRICED"
    cost = f"{mark['cost_sda']:.2f} SDA" if mark["cost_sda"] is not None else "UNKNOWN"
    pnl = (f"{mark['pnl_sda']:+.2f} SDA ({mark['pnl_pct']:+.2f}%)"
           if mark["pnl_sda"] is not None else "N/A")
    if mark["price_sda"] is not None:
        price_line = f"Price: market valuation ({mark['price_sda']:.8g} SDA; {mark['quote_age']})"
    else:
        price_line = "Price: unpriced (no valid market mark)"
    sell_line = ("Sell: ✅ executable quote" if mark["sellable"]
                 else f"Sell: ⚠ quote unavailable ({mark['sell_error'] or 'unknown'})")
    return [f"• {symbol}", f"  Value: {value}", f"  Cost: {cost}",
            f"  P/L: {pnl}", f"  {price_line}", f"  {sell_line}"]


def bot_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        positions = {}
    wallet = _wallet_address()
    balance, balance_error = _wallet_balance_sda()
    daily = _num(state.get("daily_realized_pnl_sda"))
    state_updated = str(state.get("updated_at") or "—")[:19].replace("T", " ")
    marks = _position_marks(dashboard, positions)
    lines = [
        "🤖 REAL TRADING BOT", "", f"Status: {_status()}",
        f"👛 Bot wallet: {_wallet_short(wallet)}",
        f"🎯 Position size: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📦 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.0f} SDA",
        f"📊 Open positions: {len(positions)}/{cfg.MAX_OPEN_POSITIONS}",
        f"🕒 Position ledger updated: {state_updated} UTC",
    ]
    lines += _portfolio_summary_lines(balance, balance_error, marks, _realized_total(state), daily)
    if not marks:
        lines += ["────────────────────────",
                  "⚪ No open bot positions"]
    else:
        lines += ["🪙 OPEN BOT POSITIONS", "────────────────────────"]
        for symbol, mark in marks:
            lines.extend(_position_lines(symbol, mark))
    lines += ["", "Market values use last observed market prices; SELL quotes are separate and may change before execution.",
              "Bot wallet is separate from the existing manual REAL wallet."]
    return "\n".join(lines)


def statistics_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    trades = state.get("trades", [])
    positions = positions if isinstance(positions, dict) else {}
    trades = [x for x in trades if isinstance(x, dict)] if isinstance(trades, list) else []
    buys = [x for x in trades if str(x.get("action", "")).upper() == "BUY"]
    sells = [x for x in trades if str(x.get("action", "")).upper() == "SELL"]
    realized_values = [_finite_number(x.get("realized_pnl_sda")
                                      if x.get("realized_pnl_sda") is not None
                                      else x.get("pnl_sda")) for x in sells]
    known_realized_values = [value for value in realized_values if value is not None]
    realized_total = _realized_total(state)
    wins = [x for x in realized_values if x is not None and x > 0]
    losses = [x for x in realized_values if x is not None and x < 0]
    daily = _num(state.get("daily_realized_pnl_sda"))
    balance, balance_error = _wallet_balance_sda()
    wallet = _wallet_address()
    updated = str(state.get("updated_at") or "—")[:16].replace("T", " ")
    marks = _position_marks(dashboard, positions)
    gross_wins, gross_losses = sum(wins), abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses else (float("inf") if gross_wins else None)
    win_rate = len(wins) / len(known_realized_values) * 100 if known_realized_values else None
    closed_summary = (f"📊 Closed trades: {len(sells)} • Win rate: {win_rate:.1f}% ({len(known_realized_values)}/{len(sells)} valued)"
                      if win_rate is not None else f"📊 Closed trades: {len(sells)} • Win rate: N/A")
    lines = [
        "📈 REAL TRADING BOT • STATISTICS", "",
        f"🟢 Open positions: {len(positions)} / {cfg.MAX_OPEN_POSITIONS}",
        f"📥 Confirmed BUYs: {len(buys)}", f"📤 Confirmed SELLs: {len(sells)}", closed_summary,
        f"💰 Realized P/L total: {realized_total:+.2f} SDA" if realized_total is not None else "💰 Realized P/L total: N/A",
        f"🏆 Avg win: {sum(wins)/len(wins):+.2f} SDA" if wins else "🏆 Avg win: N/A",
        f"🧯 Avg loss: {sum(losses)/len(losses):+.2f} SDA" if losses else "🧯 Avg loss: N/A",
        f"⚖️ Profit factor: {pf:.2f}" if pf is not None and pf != float("inf") else ("⚖️ Profit factor: ∞" if pf == float("inf") else "⚖️ Profit factor: N/A"),
        f"📊 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.2f} SDA",
        f"🎯 Position range: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"🕒 State updated: {updated}",
    ]
    lines += _portfolio_summary_lines(balance, balance_error, marks, realized_total, daily)
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
        for trade in trades[-15:][::-1]:
            action = str(trade.get("action") or "?").upper()
            symbol = trade.get("symbol") or str(trade.get("token") or "UNKNOWN")[:10]
            amount = trade.get("amount_sda")
            tx = str(trade.get("tx") or "")
            amount_text = f" • {float(amount):.2f} SDA" if amount is not None else ""
            pnl = trade.get("realized_pnl_sda")
            pnl_text = f" • P/L {float(pnl):+.2f} SDA" if pnl is not None else ""
            lines.append(f"{'🟢' if action == 'BUY' else '🔴' if action == 'SELL' else '⚪'} {action} {symbol}{amount_text}{pnl_text}")
            if tx:
                lines.append(f"   tx {tx[:10]}...")
    lines += ["", f"👛 Bot wallet: {_wallet_short(wallet)}", "👛 Separate automated-trading wallet",
              "P/L estimates exclude gas and may differ from final realized results."]
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
