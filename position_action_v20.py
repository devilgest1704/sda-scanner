"""Position Action UI compatibility layer.

V26 is the single paper/decision policy. This module only wires the existing
Telegram Real Wallet menu to the V26 read-only reports.
"""
from copy import deepcopy
import v26_dashboard_patch
import real_wallet_market_fix


def _real_statistics_report(dashboard):
    """Legacy REAL statistics alias; all figures come from real_accounting_state.json."""
    import real_bot_views
    text,_keyboard=real_bot_views.render_view(dashboard,"stats",refresh=True)
    return text

def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_position_action_ui_patched", False): return dashboard
    v26_dashboard_patch.patch_dashboard(dashboard)
    original_handle_update = dashboard.handle_update
    original_menu_keyboard = dashboard.menu_keyboard

    def menu_keyboard_v26():
        keyboard = original_menu_keyboard()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REAL" for row in rows):
            rows.insert(2, [{"text": "👛 Real Wallet & Positions", "callback_data": "REAL"}])
        return keyboard

    def handle_update_v26(update, state=None):
        data = update if isinstance(update, dict) else {}
        callback = data.get("callback_query") or {}
        if callback:
            cb_data = str(callback.get("data") or "")
            callback_id = callback.get("id")
            msg = callback.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            message_id = msg.get("message_id")
            if cb_data == "REAL":
                dashboard.answer_callback(callback_id)
                import real_bot_views
                text,keyboard=real_bot_views.render_view(dashboard,"main",refresh=True)
                dashboard.edit(chat_id,message_id,text,keyboard)
                return state if state is not None else {"offset": 0}
            if cb_data == "REAL_STATS":
                dashboard.answer_callback(callback_id)
                import real_bot_views
                text,keyboard=real_bot_views.render_view(dashboard,"stats",refresh=True)
                dashboard.edit(chat_id,message_id,text,keyboard)
                return state if state is not None else {"offset": 0}
        return original_handle_update(update, state)

    dashboard.menu_keyboard = menu_keyboard_v26
    dashboard.handle_update = handle_update_v26
    dashboard.real_statistics_report = lambda: _real_statistics_report(dashboard)
    real_wallet_market_fix.patch_dashboard(dashboard)
    dashboard._sda_position_action_ui_patched = True
    return dashboard
