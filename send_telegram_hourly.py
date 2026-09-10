import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20
import real_bot_menu


_original_dashboard_load = dashboard.load


def _dashboard_load_synced(path, default):
    data = _original_dashboard_load(path, default)
    if path == "portfolio_data.json" and isinstance(data, dict):
        wallet = _original_dashboard_load("wallet_data.json", {})
        return compact._sync_current_to_wallet(wallet, data)
    return data


def main():
    dashboard.load = _dashboard_load_synced
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    position_action_v20.patch_dashboard(dashboard)
    real_bot_menu.patch_dashboard(dashboard)

    # IMPORTANT: keep the already-patched menu (including REAL and REAL_BOT)
    # and only add Refresh on top of it. The previous code rebuilt the menu
    # from dashboard.menu_keyboard's original implementation and therefore
    # silently discarded the Real Trading Bot button.
    _patched_menu_keyboard = dashboard.menu_keyboard

    def _menu_keyboard_with_refresh():
        keyboard = _patched_menu_keyboard()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REFRESH" for row in rows):
            rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
        return keyboard

    dashboard.menu_keyboard = _menu_keyboard_with_refresh
    dashboard.run()


if __name__ == "__main__":
    main()
