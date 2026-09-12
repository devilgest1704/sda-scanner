import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20
import real_bot_menu
import paper_statistics_fix
import dashboard_consistency
import dashboard_wallet_summary


_original_dashboard_load = dashboard.load


def _dashboard_load_synced(path, default):
    data = _original_dashboard_load(path, default)
    if path == "portfolio_data.json" and isinstance(data, dict):
        wallet = _original_dashboard_load("wallet_data.json", {})
        return compact._sync_current_to_wallet(wallet, data)
    return data


def _install_dashboard_compat():
    """Provide the callback API expected by the dashboard patch modules.

    Telegram callbacks are handled by the Vercel webhook. The hourly GitHub
    Actions job only needs the dashboard callback API so the patch modules can
    wrap it; it must send one report and then exit.
    """
    if not hasattr(dashboard, "handle_update"):
        def handle_update(update, state=None):
            state = state if state is not None else {"offset": 0}
            cb = update.get("callback_query") or {}
            if cb:
                dashboard._handle_callback(cb)
                return state

            message = update.get("message") or {}
            text = str(message.get("text") or "").strip().lower()
            chat_id = (message.get("chat") or {}).get("id")
            configured_chat = dashboard.os.environ.get("CHAT_ID")
            if configured_chat and str(chat_id) != str(configured_chat):
                return state
            if text in ("/start", "/menu", "menu"):
                dashboard.send(dashboard.main_dashboard(), dashboard.menu_keyboard(), chat_id)
            return state

        dashboard.handle_update = handle_update


def _menu_without_unused_items():
    keyboard = dashboard.menu_keyboard()
    rows = []
    for row in keyboard.get("inline_keyboard", []):
        if not row:
            continue
        callback = row[0].get("callback_data")
        if callback in ("TECH", "MOMENTUM"):
            continue
        rows.append(row)
    keyboard["inline_keyboard"] = rows
    return keyboard


def main():
    _install_dashboard_compat()
    dashboard.load = _dashboard_load_synced
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    position_action_v20.patch_dashboard(dashboard)
    real_bot_menu.patch_dashboard(dashboard)
    paper_statistics_fix.patch_dashboard(dashboard)
    dashboard_consistency.patch_dashboard(dashboard)
    dashboard_wallet_summary.patch_dashboard(dashboard)

    _patched_menu_keyboard = dashboard.menu_keyboard

    def _menu_keyboard_with_refresh():
        keyboard = _patched_menu_keyboard()
        rows = []
        for row in keyboard.get("inline_keyboard", []):
            if not row:
                continue
            callback = row[0].get("callback_data")
            if callback in ("TECH", "MOMENTUM"):
                continue
            rows.append(row)
        if not any(row and row[0].get("callback_data") == "REFRESH" for row in rows):
            rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
        keyboard["inline_keyboard"] = rows
        return keyboard

    dashboard.menu_keyboard = _menu_keyboard_with_refresh

    _original_handle_update = dashboard.handle_update

    def _handle_update_with_refresh(update, state=None):
        cb = update.get("callback_query") or {}
        if cb.get("data") != "REFRESH":
            return _original_handle_update(update, state)

        state = state if state is not None else {"offset": 0}
        state["offset"] = max(
            int(state.get("offset", 0)),
            int(update.get("update_id", 0)) + 1,
        )
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        configured_chat = dashboard.os.environ.get("CHAT_ID")

        if configured_chat and str(chat_id) != str(configured_chat):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state

        dashboard.answer_callback(cb.get("id"), "Refreshing…")
        dashboard.edit(
            chat_id,
            message_id,
            dashboard.main_dashboard(),
            dashboard.menu_keyboard(),
        )
        return state

    dashboard.handle_update = _handle_update_with_refresh

    dashboard.send(dashboard.main_dashboard(), dashboard.menu_keyboard())


if __name__ == "__main__": main()
