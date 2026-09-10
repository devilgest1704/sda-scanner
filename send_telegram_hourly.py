import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20
import real_bot_menu


_original_menu_keyboard = dashboard.menu_keyboard
_original_dashboard_load = dashboard.load


def _dashboard_load_synced(path, default):
    data = _original_dashboard_load(path, default)
    if path == "portfolio_data.json" and isinstance(data, dict):
        wallet = _original_dashboard_load("wallet_data.json", {})
        return compact._sync_current_to_wallet(wallet, data)
    return data


def _menu_keyboard_with_refresh():
    keyboard = _original_menu_keyboard()
    rows = keyboard.get("inline_keyboard", [])
    if not any(row and row[0].get("callback_data") == "REAL" for row in rows):
        rows.insert(2, [{"text": "💰 Real Trading", "callback_data": "REAL"}])
    if not any(row and row[0].get("callback_data") == "REFRESH" for row in rows):
        rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
    return keyboard


def main():
    dashboard.load = _dashboard_load_synced
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    position_action_v20.patch_dashboard(dashboard)
    real_bot_menu.patch_dashboard(dashboard)
    dashboard.menu_keyboard = _menu_keyboard_with_refresh
    dashboard.run()


if __name__ == "__main__":
    main()
