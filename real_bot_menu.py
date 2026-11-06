"""Telegram GUI for the separate automated real-trading bot.

The existing REAL menu remains the user's manual real-wallet dashboard.
This module only reports the dedicated automated bot wallet/state; it does not
execute orders.
"""
import json
import os

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


def _position_mark_to_market(token, pos):
    """Use newest PinetSwap trade price for this exact token and chain balance."""
    from datetime import datetime, timezone
    token = str(token).lower()
    wallet = _wallet_address()
    decimals = 18
    try:
        with open("token_metadata.json", "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        item = metadata.get(token, {}) if isinstance(metadata, dict) else {}
        if isinstance(item, dict) and item.get("decimals") is not None:
            decimals = int(item["decimals"])
    except Exception:
        pass
    try:
        import real_trader
        amount_raw = int(real_trader.token_balance(token, wallet))
    except Exception as exc:
        return None, None, f"wallet balance unavailable: {str(exc)[:55]}"
    if amount_raw <= 0:
        cost = _num(pos.get("amount_sda", pos.get("cost_sda")))
        return 0.0, -cost, "wallet balance is zero"
    try:
        import requests
        url = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
        headers = {"apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z", "accept-profile": "public"}
        response = requests.get(url, params={"select": "price_in_sda,tx_timestamp,block_number", "token_address": f"eq.{token}", "price_in_sda": "gt.0", "order": "block_number.desc", "limit": "1"}, headers=headers, timeout=8)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return None, None, "no recent PinetSwap trade price"
        row = rows[0]
        price = _num(row.get("price_in_sda"))
        if price <= 0:
            return None, None, "invalid PinetSwap price"
        stamp = str(row.get("tx_timestamp") or "")
        age_seconds = None
        if stamp:
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                age_seconds = max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
            except Exception:
                pass
        if age_seconds is not None and age_seconds > 600:
            return None, None, f"last PinetSwap trade is {age_seconds // 60}m old"
        token_amount = amount_raw / (10 ** decimals)
        value_sda = token_amount * price
        cost_sda = _num(pos.get("amount_sda", pos.get("cost_sda")))
        age_text = f"{age_seconds}s" if age_seconds is not None else "fresh"
        return value_sda, value_sda - cost_sda, f"live PinetSwap • {age_text}"
    except Exception as exc:
        return None, None, f"PinetSwap price unavailable: {str(exc)[:60]}"


def _pnl_text(value):
    return f"{value:+.2f} SDA" if value is not None else "N/A"


def bot_report(dashboard):
    state = _state(dashboard)
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        positions = {}
    wallet = _wallet_address()
    balance, balance_error = _wallet_balance_sda()
    daily = _num(state.get("daily_realized_pnl_sda"))
    balance_text = f"{balance:.3f} SDA" if balance is not None else f"RPC ERROR: {balance_error}"
    lines = [
        "🤖 REAL TRADING BOT", "", f"Status: {_status()}",
        f"👛 Bot wallet: {_wallet_short(wallet)}", f"💰 SDA balance: {balance_text}",
        f"🎯 Position size: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📦 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.0f} SDA",
        f"📊 Open positions: {len(positions)}/{cfg.MAX_OPEN_POSITIONS}",
        f"📉 Daily realized P/L: {daily:+.2f} SDA",
    ]
    if not positions:
        lines += ["📈 Unrealized P/L: +0.00 SDA", "💼 Open cost: 0.00 SDA",
                  "💹 Total P/L: N/A (no closed trades)", "────────────────────────",
                  "⚪ No open bot positions"]
    else:
        lines += ["🪙 OPEN BOT POSITIONS", "────────────────────────"]
        marks = []
        open_cost = 0.0
        valid_count = 0
        for token, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            symbol = _token_label(token, pos)
            cost = _num(pos.get("amount_sda", pos.get("cost_sda")))
            open_cost += cost
            value, pnl, error = _position_mark_to_market(token, pos)
            if pnl is None:
                lines.append(f"• {symbol} • cost {cost:.2f} SDA • P/L N/A")
                lines.append(f"  Quote unavailable: {error}")
            else:
                valid_count += 1
                marks.append(pnl)
                pct = pnl / cost * 100 if cost else 0.0
                marker = f" • {error}" if str(error or "").startswith("live PinetSwap") else (" ⚠️ snapshot" if str(error or "").startswith("snapshot") else (" ⚠️ ledger amount" if error == "ledger amount" else ""))
                lines.append(f"• {symbol} • {pnl:+.2f} SDA ({pct:+.2f}%){marker}")
                lines.append(f"  Value {value:.2f} / cost {cost:.2f} SDA")
        unrealized = sum(marks) if valid_count == len([p for p in positions.values() if isinstance(p, dict)]) else None
        lines += ["────────────────────────", f"📈 Unrealized P/L: {_pnl_text(unrealized)}",
                  f"💼 Open cost: {open_cost:.2f} SDA", f"💹 Realized today: {daily:+.2f} SDA"]
    lines += ["", "P/L is quote-based estimate; excludes gas and may change before execution.",
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
    realized_values = [_num(x.get("realized_pnl_sda")) for x in sells if x.get("realized_pnl_sda") is not None]
    realized_total = sum(realized_values)
    wins = [x for x in realized_values if x > 0]
    losses = [x for x in realized_values if x < 0]
    daily = _num(state.get("daily_realized_pnl_sda"))
    balance, balance_error = _wallet_balance_sda()
    wallet = _wallet_address()
    updated = str(state.get("updated_at") or "—")[:16].replace("T", " ")
    balance_text = f"{balance:.3f}" if balance is not None else f"RPC ERROR: {balance_error}"
    marks, position_rows, open_cost = [], [], 0.0
    for token, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        symbol = _token_label(token, pos)
        cost = _num(pos.get("amount_sda", pos.get("cost_sda")))
        open_cost += cost
        value, pnl, error = _position_mark_to_market(token, pos)
        if pnl is None:
            position_rows.append(f"• {symbol}: P/L N/A ({error})")
        else:
            marks.append(pnl)
            pct = pnl / cost * 100 if cost else 0.0
            marker = f" • {error}" if str(error or "").startswith("live PinetSwap") else (" ⚠️ snapshot" if str(error or "").startswith("snapshot") else "")
            position_rows.append(f"• {symbol}: {pnl:+.2f} SDA ({pct:+.2f}%){marker}")
    valid_positions = len(position_rows)
    unrealized = sum(marks) if len(marks) == valid_positions else None
    combined = realized_total + unrealized if unrealized is not None else None
    gross_wins, gross_losses = sum(wins), abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses else (float("inf") if gross_wins else None)
    closed_summary = (f"📊 Closed trades: {len(realized_values)} • Win rate: {len(wins)/len(realized_values)*100:.1f}%"
                      if realized_values else "📊 Closed trades: 0 • Win rate: N/A")
    lines = [
        "📈 REAL TRADING BOT • STATISTICS", "",
        f"🟢 Open positions: {len(positions)} / {cfg.MAX_OPEN_POSITIONS}",
        f"📥 Confirmed BUYs: {len(buys)}", f"📤 Confirmed SELLs: {len(sells)}", closed_summary,
        f"💰 Realized P/L total: {realized_total:+.2f} SDA",
        f"📈 Unrealized P/L: {_pnl_text(unrealized)}", f"💹 Combined P/L: {_pnl_text(combined)}",
        f"🏆 Avg win: {sum(wins)/len(wins):+.2f} SDA" if wins else "🏆 Avg win: N/A",
        f"🧯 Avg loss: {sum(losses)/len(losses):+.2f} SDA" if losses else "🧯 Avg loss: N/A",
        f"⚖️ Profit factor: {pf:.2f}" if pf is not None and pf != float("inf") else ("⚖️ Profit factor: ∞" if pf == float("inf") else "⚖️ Profit factor: N/A"),
        f"💼 Open cost: {open_cost:.2f} SDA", "────────────────────────",
        f"💰 Wallet native SDA: {balance_text}", f"📊 Capital cap: {cfg.TRADING_WALLET_CAPITAL_SDA:.2f} SDA",
        f"🎯 Position range: {cfg.POSITION_MIN_SDA:.0f}–{cfg.POSITION_MAX_SDA:.0f} SDA",
        f"📉 Daily realized P/L: {daily:+.2f} SDA", f"🕒 State updated: {updated}",
        "", "🪙 OPEN POSITION P/L", "────────────────────────",
    ]
    lines.extend(position_rows or ["⚪ No open positions"])
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
