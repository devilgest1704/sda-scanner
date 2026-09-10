import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20


_original_menu_keyboard = dashboard.menu_keyboard


def _menu_keyboard_with_refresh():
    keyboard = _original_menu_keyboard()
    rows = keyboard.get("inline_keyboard", [])
    if not any(row and row[0].get("callback_data") == "REAL" for row in rows):
        rows.insert(2, [{"text": "💰 Real Trading", "callback_data": "REAL"}])
    if not any(row and row[0].get("callback_data") == "REFRESH" for row in rows):
        rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
    return keyboard


def main():
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    position_action_v20.patch_dashboard(dashboard)
    dashboard.menu_keyboard = _menu_keyboard_with_refresh
    dashboard.run()


if __name__ == "__main__":
    main()
