"""Telegram GUI for the separate automated real-trading bot.

This is intentionally separate from the existing REAL wallet dashboard, which
continues to represent the user's manual real-wallet trading. The bot uses
real_trade_state.json and the dedicated REAL_TRADING_* wallet configuration.
No order is executed by this module.
"""
import os
from datetime import datetime, timezone

import real_trading_config as cfg


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _wallet_address():
    value = os.environ.get(cfg.WALLET_ADDRESS_ENV, "").strip()
    if value:
        return value
    try:
        from real_trader import wallet_address
        return wallet_address()
    except Exception:
        return ""


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
    """Read-only native SDA balance using the public Sidra RPC."""
    address = _wallet_address()
    if not address:
        return None
    try:
        import requests
        response = requests.post(
            cfg.RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]},
            timeout=10,
            headers={"Content-Type": "application/json", "User-Agent": "sda-real-bot-gui/1.0"},
        )
        response.raise_for_status()
        data = response.json()
        return int(data["result"], 16) / 10**18
    except Exception:
        return None


def bot_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        positions = {}
    wallet = _wallet_address()
    balance = _wallet_balance_sda()
    daily = _num(state.get("daily_realized_pnl_sda"))

    lines = [
        "🤖 REAL TRADING BOT",
        "",
        f"Status: {_status()}",
        f"👛 Bot wallet: {wallet[:10] + '...' + wallet[-6:] if len(wallet) > 20 else (wallet or 'NOT CONFIGURED')}",
        f"💰 SDA balance: {'%.3f SDA' % balance if balance is not None else 'UNKNOWN'}",
        f"🎯 Position size: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📦 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.0f} SDA",
        f"📊 Open positions: {len(positions)}/{cfg.MAX_OPEN_POSITIONS}",
        f"📉 Daily realized P/L: {daily:+.2f} SDA",
        "────────────────────────",
    ]

    if not positions:
        lines.append("⚪ No open bot positions")
    else:
        lines.append("🪙 OPEN BOT POSITIONS")
        for token, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            symbol = pos.get("symbol") or token[:10]
            amount = _num(pos.get("amount_sda"))
            lines.append(f"• {symbol} • {amount:.2f} SDA")

    lines += ["", "Bot wallet is separate from the existing manual REAL wallet."]
    return "\n".join(lines)


def statistics_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    trades = state.get("trades", [])
    positions = positions if isinstance(positions, dict) else {}
    trades = trades if isinstance(trades, list) else []
    buys = [x for x in trades if isinstance(x, dict) and str(x.get("action", "")).upper() == "BUY"]
    sells = [x for x in trades if isinstance(x, dict) and str(x.get("action", "")).upper() == "SELL"]
    daily = _num(state.get("daily_realized_pnl_sda"))
    balance = _wallet_balance_sda()
    wallet = _wallet_address()
    updated = state.get("updated_at")
    if updated:
        try:
            updated = str(updated)[:16].replace("T", " ")
        except Exception:
            pass
    else:
        updated = "—"

    lines = [
        "📈 REAL TRADING BOT • STATISTICS",
        "",
        f"🟢 Open positions: {len(positions)} / {cfg.MAX_OPEN_POSITIONS}",
        f"📥 Bot BUYs: {len(buys)}",
        f"📤 Bot SELLs: {len(sells)}",
        "────────────────────────",
        f"💰 Wallet SDA: {'%.3f' % balance if balance is not None else 'UNKNOWN'}",
        f"📊 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.2f} SDA",
        f"🎯 Position range: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📉 Daily realized P/L: {daily:+.2f} SDA",
        f"🕒 State updated: {updated}",
        "",
        "📜 RECENT BOT TRADES",
        "────────────────────────",
    ]
    if not trades:
        lines.append("⚪ No bot trades yet")
    else:
        for trade in trades[-15:][::-1]:
            action = str(trade.get("action") or "?").upper()
            symbol = trade.get("symbol") or str(trade.get("token") or "UNKNOWN")[:10]
            amount = trade.get("amount_sda")
            tx = str(trade.get("tx") or "")
            tx_short = tx[:10] + "..." if len(tx) > 10 else tx
            amount_text = f" • {float(amount):.2f} SDA" if amount is not None else ""
            lines.append(f"{'🟢' if action == 'BUY' else '🔴' if action == 'SELL' else '⚪'} {action} {symbol}{amount_text}")
            if tx_short:
                lines.append(f"   tx {tx_short}")
    lines += ["", "👛 Separate automated-trading wallet"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    original_menu = dashboard.menu_keyboard
    original_handle = dashboard.handle_update

    def menu_keyboard():
        keyboard = original_menu()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REAL_BOT" for row in rows):
            # Keep existing REAL (manual wallet) and add the bot immediately below it.
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
        if data not in ("REAL_BOT", "REAL_BOT_STATS"):
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
        if data == "REAL_BOT":
            keyboard = {"inline_keyboard": [
                [{"text": "📈 Statistics", "callback_data": "REAL_BOT_STATS"}],
                [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
            ]}
            dashboard.edit(chat_id, message_id, bot_report(dashboard), keyboard)
        else:
            keyboard = {"inline_keyboard": [[{"text": "⬅️ Real Trading Bot", "callback_data": "REAL_BOT"}], [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}
            dashboard.edit(chat_id, message_id, statistics_report(dashboard), keyboard)
        return state

    dashboard.menu_keyboard = menu_keyboard
    dashboard.handle_update = handle_update
    return dashboard
